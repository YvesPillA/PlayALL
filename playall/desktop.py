from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
import sys
import tempfile
import threading
import time
from pathlib import Path

from PySide6.QtCore import QObject, QRunnable, QThreadPool, QTimer, Qt, Signal
from PySide6.QtGui import QColor, QFont, QIcon, QPainter, QPainterPath, QPen, QPixmap
from PySide6.QtNetwork import QLocalServer, QLocalSocket
from PySide6.QtWidgets import (QApplication, QComboBox, QDialog, QFileDialog, QFormLayout,
    QFrame, QHBoxLayout, QLabel, QLineEdit, QMainWindow, QMessageBox, QPlainTextEdit,
    QPushButton, QSizePolicy, QSplitter, QTextBrowser, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget)

from .config import Settings
from .games.registry import game_specs
from .llm import make_ai
from .runner import Runner
from .storage import atomic_json
from .processes import inspect_process, list_processes
from .focus_window import FocusWindow


STYLE = """
QWidget { font-family: 'Microsoft YaHei UI'; font-size: 13px; color: #213246; }
QMainWindow, QDialog { background: #f5f7fa; }
QLabel#Brand { font-family: 'Segoe UI'; font-size: 25px; font-weight: 700; letter-spacing: -0.5px; }
QLabel#Subtitle, QLabel#Hint, QLabel#Footer { color: #68798a; font-size: 12px; }
QLabel#Section { font-size: 13px; font-weight: 600; }
QFrame#Line { background: #dfe5ed; max-height: 1px; }
QPushButton { background: #ffffff; border: 1px solid #d6dfe9; border-radius: 7px; padding: 7px 13px; min-height: 20px; }
QPushButton:hover { background: #edf2f8; border-color: #b3c3d7; }
QPushButton:pressed { background: #e1e9f3; }
QPushButton:focus { border: 2px solid #648ee8; padding: 6px 12px; }
QPushButton:disabled { color: #9ca8b7; background: #edf0f4; border-color: #e0e5eb; }
QPushButton#Primary { background: #2c5ee8; color: white; border: 0; font-size: 15px; font-weight: 600; min-height: 31px; padding: 9px; }
QPushButton#Primary:hover { background: #234fcc; }
QPushButton#Primary:pressed { background: #1d42ae; }
QPushButton#Primary:disabled { background: #94a9d6; color: #f3f6fd; }
QPushButton#Primary[paused='true'] { background: #276d62; }
QPushButton#Primary[paused='true']:hover { background: #205b52; }
QPushButton#Primary[paused='true']:disabled { background: #d8e1ec; color: #68798a; }
QPushButton#TextButton { color: #456287; background: transparent; border: 1px solid transparent; padding: 5px 8px; }
QPushButton#TextButton:hover { background: #e7edf6; }
QComboBox { background: white; border: 1px solid #d6dfe9; border-radius: 7px; padding: 8px 11px; min-height: 20px; }
QComboBox:focus { border-color: #648ee8; }
QComboBox:disabled { color: #8290a1; background: #edf0f4; }
QComboBox::drop-down { border: 0; width: 27px; }
QComboBox QAbstractItemView { background: white; selection-background-color: #e6edff; selection-color: #213246; border: 1px solid #d6dfe9; padding: 4px; }
QPlainTextEdit, QTextBrowser { background: white; border: 1px solid #d6dfe9; border-radius: 8px; padding: 10px; selection-background-color: #dce6ff; }
QPlainTextEdit:focus { border-color: #648ee8; }
QLineEdit { background: white; border: 1px solid #d6dfe9; border-radius: 6px; padding: 8px; selection-background-color: #dce6ff; }
QLineEdit:focus { border-color: #648ee8; }
QTreeWidget { background: white; border: 1px solid #d6dfe9; border-radius: 7px; padding: 5px; }
QTreeWidget::item { padding: 6px; }
QTreeWidget::item:selected { background: #e6edff; color: #213246; }
QHeaderView::section { background: #edf2f8; border: 0; padding: 7px; color: #68798a; }
QScrollBar:vertical { background: transparent; width: 9px; margin: 3px; }
QScrollBar::handle:vertical { background: #becada; border-radius: 3px; min-height: 25px; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical { background: transparent; }
"""


def app_icon():
    pix = QPixmap(128, 128)
    pix.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pix)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setBrush(QColor("#2c5ee8"))
    painter.setPen(Qt.PenStyle.NoPen)
    painter.drawRoundedRect(4, 4, 120, 120, 28, 28)
    path = QPainterPath()
    path.moveTo(48, 34)
    path.lineTo(90, 64)
    path.lineTo(48, 94)
    path.closeSubpath()
    painter.setBrush(QColor("white"))
    painter.drawPath(path)
    painter.end()
    return QIcon(pix)


class WorkSignals(QObject):
    done = Signal(object)
    failed = Signal(str)


class Selector(QComboBox):
    def paintEvent(self, event):
        super().paintEvent(event)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(QPen(QColor("#68798a"), 1.6))
        x, y = self.width() - 18, self.height() // 2
        painter.drawLine(x - 4, y - 2, x, y + 2)
        painter.drawLine(x, y + 2, x + 4, y - 2)
        painter.end()


class Work(QRunnable):
    def __init__(self, function):
        super().__init__()
        self.function, self.signals = function, WorkSignals()

    def run(self):
        try:
            self.signals.done.emit(self.function())
        except Exception as error:
            self.signals.failed.emit(str(error)[:3000])


def label(text, name=""):
    item = QLabel(text)
    if name:
        item.setObjectName(name)
    return item


def line():
    item = QFrame()
    item.setObjectName("Line")
    item.setFixedHeight(1)
    return item


class ProcessDialog(QDialog):
    def __init__(self, owner):
        super().__init__(owner)
        self.setWindowTitle("选择游戏进程")
        self.resize(570, 520)
        self.selection = self.game_id = None
        layout = QVBoxLayout(self)
        layout.setContentsMargins(22, 22, 22, 20)
        layout.setSpacing(12)
        layout.addWidget(label("选择已经打开的游戏", "Section"))
        hint = label("只显示已核对版本、可以接入的游戏进程。进程列表不会发送给 AI。", "Hint")
        hint.setWordWrap(True)
        layout.addWidget(hint)
        search_row = QHBoxLayout()
        self.search = QLineEdit()
        self.search.setPlaceholderText("搜索进程名称或 PID")
        self.search.setAccessibleName("搜索进程")
        search_row.addWidget(self.search, 1)
        refresh = QPushButton("刷新")
        refresh.clicked.connect(self.reload)
        search_row.addWidget(refresh)
        layout.addLayout(search_row)
        self.table = QTreeWidget()
        self.table.setObjectName("processList")
        self.table.setRootIsDecorated(False)
        self.table.setHeaderLabels(["进程", "PID", "已适配游戏"])
        self.table.setColumnWidth(0, 235)
        self.table.setColumnWidth(1, 80)
        layout.addWidget(self.table, 1)
        self.message = label("", "Hint")
        self.message.setWordWrap(True)
        layout.addWidget(self.message)
        buttons = QHBoxLayout()
        cancel = QPushButton("取消")
        cancel.clicked.connect(self.reject)
        self.select = QPushButton("选择进程")
        self.select.setObjectName("Primary")
        self.select.setEnabled(False)
        self.select.clicked.connect(self.choose)
        buttons.addStretch()
        buttons.addWidget(cancel)
        buttons.addWidget(self.select)
        layout.addLayout(buttons)
        self.search.textChanged.connect(self.filter_rows)
        self.table.itemSelectionChanged.connect(self.selection_changed)
        self.table.itemDoubleClicked.connect(lambda *args: self.choose() if self.select.isEnabled() else None)
        self.reload()

    def reload(self):
        self.table.clear()
        supported = {spec.executable_name.casefold(): spec for spec in game_specs().values() if spec.process_attach and spec.process_verifier}
        try:
            processes = list_processes()
            processes.sort(key=lambda value: (value.name.casefold(), value.pid))
            for process in processes:
                spec = supported.get(process.name.casefold())
                if not spec:
                    continue
                current = inspect_process(process.pid)
                if not current or not spec.accepts_process(current):
                    continue
                item = QTreeWidgetItem([current.name, str(current.pid), spec.label])
                item.setData(0, Qt.ItemDataRole.UserRole, (current.pid, spec.id, current.started, current.path))
                self.table.addTopLevelItem(item)
            self.message.setText("选择进程后点击连接；连接不会启动游戏。" if self.table.topLevelItemCount() else
                                 "未检测到已适配的游戏进程。请先打开游戏，再刷新列表。")
            self.filter_rows()
        except Exception as error:
            self.message.setText(str(error))
            self.parent().runner.debug.exception("process_list", error)

    def filter_rows(self, *unused):
        query = self.search.text().strip().casefold()
        for index in range(self.table.topLevelItemCount()):
            item = self.table.topLevelItem(index)
            item.setHidden(query not in (item.text(0) + " " + item.text(1)).casefold())
        if self.table.currentItem() and self.table.currentItem().isHidden():
            self.table.setCurrentItem(None)
            self.select.setEnabled(False)

    def selection_changed(self):
        item = self.table.currentItem()
        value = item.data(0, Qt.ItemDataRole.UserRole) if item else None
        spec = game_specs().get(value[1]) if value else None
        self.select.setEnabled(bool(spec and spec.process_attach))

    def choose(self):
        if not self.select.isEnabled():
            return
        pid, game_id, started, path = self.table.currentItem().data(0, Qt.ItemDataRole.UserRole)
        process = inspect_process(pid)
        spec = game_specs()[game_id]
        if not process or process.started != started or process.path != path or not spec.accepts_process(process):
            self.message.setText("这个进程已退出或无法读取，请刷新后重新选择。")
            return
        self.selection, self.game_id = process.selection(), game_id
        self.accept()


class DebugDialog(QDialog):
    def __init__(self, owner):
        super().__init__(owner)
        self.owner = owner
        self.setWindowTitle("PlayALL 日志")
        self.resize(760, 540)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(22, 22, 22, 20)
        layout.setSpacing(12)
        hint = label("日志保存在本机，自动隐藏密钥；包含连接、暂停和错误信息。", "Hint")
        hint.setWordWrap(True)
        layout.addWidget(hint)
        self.text = QPlainTextEdit()
        self.text.setObjectName("debugLog")
        self.text.setReadOnly(True)
        layout.addWidget(self.text, 1)
        path = label(str(owner.runner.debug.path), "Hint")
        path.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(path)
        buttons = QHBoxLayout()
        for title, callback in (("刷新", self.refresh), ("复制", self.copy), ("导出…", self.export)):
            button = QPushButton(title)
            button.clicked.connect(callback)
            buttons.addWidget(button)
        buttons.addStretch()
        close = QPushButton("关闭")
        close.clicked.connect(self.accept)
        buttons.addWidget(close)
        layout.addLayout(buttons)
        self.refresh()

    def refresh(self):
        text = self.owner.runner.debug.read()
        target = self.owner.target_process
        if not target and self.owner.runner.config.get("game_process"):
            target = self.owner.runner.config["game_process"]
        if target and Path(target["path"]).name.casefold() == "aistalt.exe":
            text += "\n\n—— 游戏连接组件 ——\n" + self.owner.runner.debug.bridge_tail(target["path"])
        self.text.setPlainText(text)
        self.text.verticalScrollBar().setValue(self.text.verticalScrollBar().maximum())

    def copy(self):
        self.refresh()
        QApplication.clipboard().setText(self.text.toPlainText())

    def export(self):
        path, _ = QFileDialog.getSaveFileName(self, "导出调试日志", "PlayALL-debug.log", "日志 (*.log);;文本 (*.txt)")
        if path:
            try:
                self.refresh()
                Path(path).write_text(self.text.toPlainText(), encoding="utf-8")
            except OSError as error:
                self.owner.show_error("导出日志失败：" + str(error))


class SettingsDialog(QDialog):
    def __init__(self, owner):
        super().__init__(owner)
        self.owner = owner
        self.setWindowTitle("PlayALL 设置")
        self.setMinimumWidth(460)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 24, 24, 22)
        layout.setSpacing(14)
        layout.addWidget(label("连接 AI", "Section"))
        form = QFormLayout()
        form.setVerticalSpacing(12)
        self.profile = Selector()
        self.profile.setObjectName("settingsProvider")
        for p in owner.settings.public()["profiles"]:
            if p["id"] != "rehearsal":
                self.profile.addItem(p["label"], p["id"])
        self.key = QLineEdit()
        self.key.setObjectName("apiKey")
        self.key.setAccessibleName("API Key")
        self.key.setEchoMode(QLineEdit.EchoMode.Password)
        self.model = QLineEdit()
        self.model.setObjectName("modelName")
        self.endpoint = QLineEdit()
        self.endpoint.setObjectName("apiEndpoint")
        form.addRow("AI", self.profile)
        form.addRow("API Key", self.key)
        form.addRow("模型", self.model)
        form.addRow("接口地址", self.endpoint)
        layout.addLayout(form)
        self.connection = label("密钥加密保存在本机。留空会保留已有密钥。", "Hint")
        self.connection.setWordWrap(True)
        self.connection.setMinimumHeight(36)
        layout.addWidget(self.connection)
        probe_row = QHBoxLayout()
        self.probe = QPushButton("测试连接")
        self.probe.setObjectName("testConnection")
        self.probe.clicked.connect(self.test_connection)
        probe_row.addWidget(self.probe)
        self.clear_key = QPushButton("清除密钥")
        self.clear_key.setObjectName("TextButton")
        self.clear_key.clicked.connect(self.forget_key)
        probe_row.addWidget(self.clear_key)
        probe_row.addStretch()
        layout.addLayout(probe_row)
        layout.addWidget(line())
        layout.addWidget(label("游戏适配器", "Section"))
        self.adapter = Selector()
        for spec in game_specs().values():
            if spec.path_setting and spec.process_attach:
                self.adapter.addItem(spec.label, spec.id)
        game_index = self.adapter.findData(owner.current_game_id())
        self.adapter.setCurrentIndex(max(0, game_index))
        layout.addWidget(self.adapter)
        self.game_paths = {spec.path_setting: owner.settings.public().get(spec.path_setting, "")
                           for spec in game_specs().values() if spec.path_setting}
        self.path_key = game_specs()[self.adapter.currentData()].path_setting
        game_row = QHBoxLayout()
        self.path = QLineEdit(self.game_paths[self.path_key])
        self.path.setToolTip(self.path.text())
        self.path.setCursorPosition(0)
        self.path.textChanged.connect(self.path.setToolTip)
        self.path.setObjectName("gamePath")
        self.path.setPlaceholderText("选择 " + game_specs()[self.adapter.currentData()].executable_name)
        self.adapter.currentIndexChanged.connect(self.select_adapter)
        browse = QPushButton("选择文件")
        browse.clicked.connect(self.browse)
        game_row.addWidget(self.path, 1)
        game_row.addWidget(browse)
        layout.addLayout(game_row)
        self.preparation_hint = label(game_specs()[self.adapter.currentData()].connection_instructions or
            "游戏由你亲自打开，再从主界面选择进程。首次使用或组件更新时，先关闭游戏并准备连接组件。", "Hint")
        self.preparation_hint.setWordWrap(True)
        layout.addWidget(self.preparation_hint)
        self.prepare_button = QPushButton("准备连接组件")
        self.prepare_button.setVisible(bool(game_specs()[self.adapter.currentData()].connection_preparer))
        self.prepare_button.clicked.connect(self.prepare_bridge)
        layout.addWidget(self.prepare_button)
        self.message = label("", "Hint")
        self.message.setWordWrap(True)
        layout.addWidget(self.message)
        bottom = QHBoxLayout()
        cancel = QPushButton("关闭")
        cancel.clicked.connect(self.reject)
        save = QPushButton("保存设置")
        save.setObjectName("Primary")
        save.clicked.connect(self.save)
        bottom.addStretch()
        bottom.addWidget(cancel)
        bottom.addWidget(save)
        layout.addLayout(bottom)
        selected = owner.provider.currentData()
        index = self.profile.findData(selected)
        self.profile.setCurrentIndex(max(0, index))
        self.profile.currentIndexChanged.connect(self.load_profile)
        self.load_profile()

    def load_profile(self):
        p = next(p for p in self.owner.settings.public()["profiles"] if p["id"] == self.profile.currentData())
        self.key.clear()
        self.key.setPlaceholderText("已保存；留空保留" if p["has_key"] else "粘贴 API Key")
        self.model.setText(p["model"])
        self.endpoint.setText(p["base_url"])
        self.clear_key.setEnabled(p["has_key"])

    def persist(self):
        self.owner.runner.debug.remember_secret(self.key.text())
        self.game_paths[self.path_key] = self.path.text()
        self.owner.settings.update({**self.game_paths, "profiles": [{"id": self.profile.currentData(),
            "api_key": self.key.text(), "model": self.model.text(), "base_url": self.endpoint.text()}]})
        self.key.clear()
        self.key.setPlaceholderText("已保存；留空保留")
        self.owner.refresh_configuration()

    def save(self):
        try:
            self.persist()
            self.accept()
        except Exception as error:
            self.message.setText(str(error))

    def test_connection(self):
        try:
            self.persist()
        except Exception as error:
            self.connection.setText(str(error))
            return
        self.probe.setEnabled(False)
        self.connection.setText("正在检查模型连接…")
        profile_id = self.profile.currentData()
        self.owner.background(lambda: make_ai(self.owner.settings.profile(profile_id)).probe(), self.probed, self.probe_error)

    def probed(self, result):
        self.probe.setEnabled(True)
        self.connection.setText(result["message"])

    def probe_error(self, message):
        self.probe.setEnabled(True)
        self.connection.setText(message)

    def forget_key(self):
        self.owner.settings.update({"profiles": [{"id": self.profile.currentData(), "clear_key": True}]})
        self.load_profile()
        self.owner.refresh_configuration()
        self.connection.setText("本机保存的密钥已清除。")

    def browse(self):
        spec = game_specs()[self.adapter.currentData()]
        path, _ = QFileDialog.getOpenFileName(self, "选择 " + spec.label, self.path.text(),
                                            spec.label + " (" + spec.executable_name + ");;应用程序 (*.exe)")
        if path:
            self.path.setText(path)

    def select_adapter(self):
        self.game_paths[self.path_key] = self.path.text()
        spec = game_specs()[self.adapter.currentData()]
        self.path_key = spec.path_setting
        self.path.setText(self.game_paths[self.path_key])
        self.path.setCursorPosition(0)
        self.path.setPlaceholderText("选择 " + spec.executable_name)
        self.prepare_button.setVisible(bool(spec.connection_preparer))
        self.preparation_hint.setText(spec.connection_instructions or
            "游戏由你亲自打开，再从主界面选择进程。首次使用或组件更新时，先关闭游戏并准备连接组件。")

    def prepare_bridge(self):
        spec = game_specs()[self.adapter.currentData()]
        if not spec.connection_preparer:
            return
        self.prepare_button.setEnabled(False)
        self.message.setText("正在检查连接组件…")
        path = self.path.text()
        def prepare():
            return spec.prepare_connection(self.owner.settings.directory, path, self.owner.runner.emit)
        self.owner.background(prepare, self.prepared, self.prepare_error)

    def prepared(self, result):
        self.prepare_button.setEnabled(True)
        self.message.setText(result.get("message", "连接组件已准备好。请亲自打开游戏，再回主界面选择进程。"))

    def prepare_error(self, message):
        self.prepare_button.setEnabled(True)
        self.message.setText(message)


class MainWindow(QMainWindow):
    def __init__(self, settings, auto_open_game=True):
        super().__init__()
        self.settings, self.runner = settings, Runner(settings, connect_game_window=auto_open_game)
        self.auto_open_game = auto_open_game
        self.pool = QThreadPool(self)
        self.pool.setMaxThreadCount(3)
        self.jobs = set()
        self.cursor = 0
        self.pending = self.quitting = False
        self.pending_label = "正在保存…"
        self.last_status = "idle"
        self.chat_pending = False
        self.chat_rendered = None
        self.question_rendered = None
        self.target_process = None
        self.target_game_id = ""
        self.setWindowTitle("PlayALL")
        self.setWindowIcon(app_icon())
        self.resize(560, 840)
        self.setMinimumSize(480, 720)
        body = QWidget()
        self.setCentralWidget(body)
        root = QVBoxLayout(body)
        self.root_layout = root
        root.setContentsMargins(25, 23, 25, 18)
        root.setSpacing(10)
        header = QHBoxLayout()
        mark = QLabel()
        mark.setPixmap(app_icon().pixmap(38, 38))
        mark.setFixedSize(38, 38)
        header.addWidget(mark)
        brand = QVBoxLayout()
        brand.setSpacing(0)
        brand.addWidget(label("PlayALL", "Brand"))
        header.addLayout(brand)
        header.addStretch()
        self.debug_button = QPushButton("日志")
        self.debug_button.setObjectName("debugButton")
        self.debug_button.clicked.connect(self.open_debug)
        header.addWidget(self.debug_button)
        self.settings_button = QPushButton("设置")
        self.settings_button.setObjectName("settingsButton")
        self.settings_button.clicked.connect(self.open_settings)
        header.addWidget(self.settings_button)
        root.addLayout(header)
        root.addWidget(line())
        selectors = QHBoxLayout()
        selectors.setSpacing(12)
        self.provider = Selector()
        self.provider.setObjectName("providerSelect")
        self.provider.setAccessibleName("选择 AI")
        for item in settings.public()["profiles"]:
            self.provider.addItem(item["label"], item["id"])
        ai_column = QVBoxLayout()
        ai_column.setSpacing(7)
        ai_column.addWidget(label("AI", "Section"))
        ai_column.addWidget(self.provider)
        selectors.addLayout(ai_column, 1)
        process_column = QVBoxLayout()
        process_column.setSpacing(7)
        process_column.addWidget(label("进程", "Section"))
        self.process_panel = QWidget()
        process_row = QHBoxLayout(self.process_panel)
        process_row.setContentsMargins(0, 0, 0, 0)
        process_row.setSpacing(8)
        self.process_button = QPushButton("选择进程…")
        self.process_button.setObjectName("selectProcess")
        self.process_button.clicked.connect(self.select_process)
        self.process_button.setAccessibleName("选择已适配游戏进程")
        self.process_button.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        process_row.addWidget(self.process_button, 1)
        self.connect_button = QPushButton("连接")
        self.connect_button.setObjectName("connectProcess")
        self.connect_button.clicked.connect(self.connect_process)
        process_row.addWidget(self.connect_button)
        process_column.addWidget(self.process_panel)
        selectors.addLayout(process_column, 1)
        root.addLayout(selectors)
        self.privacy_hint = label("连接仅在本机进行。开始游玩后，游戏状态、可见内容和游玩对话会发送给所选 AI；进程列表不会发送。", "Hint")
        self.privacy_hint.setWordWrap(True)
        root.addWidget(self.privacy_hint)
        prompt_header = QHBoxLayout()
        prompt_header.addWidget(label("总提示词", "Section"))
        prompt_header.addStretch()
        self.prompt_hint = label("长期使用，随时可改", "Hint")
        prompt_header.addWidget(self.prompt_hint)
        root.addLayout(prompt_header)
        self.prompt = QPlainTextEdit()
        self.prompt.setObjectName("systemPrompt")
        self.prompt.setAccessibleName("总提示词")
        self.prompt.setFixedHeight(76)
        self.prompt.setPlaceholderText("告诉 AI 你希望它怎样玩，其他的让它自己决定。")
        self.prompt.setPlainText(settings.public()["system_prompt"])
        root.addWidget(self.prompt)
        self.primary = QPushButton("开始游玩")
        self.primary.setObjectName("Primary")
        self.primary.setAccessibleName("开始游玩")
        self.primary.clicked.connect(self.primary_action)
        root.addWidget(self.primary)
        status_row = QHBoxLayout()
        self.dot = label("●")
        self.dot.setStyleSheet("color: #8b9aab; font-size: 12px;")
        status_row.addWidget(self.dot, 0, Qt.AlignmentFlag.AlignTop)
        self.status = label("选择已经打开的游戏进程并连接。")
        self.status.setObjectName("playStatus")
        self.status.setWordWrap(True)
        self.status.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        self.status.setMinimumHeight(36)
        self.status.setMaximumHeight(64)
        status_row.addWidget(self.status, 1)
        self.watch_button = QPushButton("检查连接")
        self.watch_button.setObjectName("TextButton")
        self.watch_button.clicked.connect(self.watch_game)
        self.watch_button.setVisible(False)
        status_row.addWidget(self.watch_button, 0, Qt.AlignmentFlag.AlignTop)
        root.addLayout(status_row)
        self.records = QSplitter(Qt.Orientation.Vertical)
        self.records.setObjectName("recordsAndChat")
        self.records.setChildrenCollapsible(False)
        self.records.setHandleWidth(8)
        log_panel = QWidget()
        log_layout = QVBoxLayout(log_panel)
        log_layout.setContentsMargins(0, 0, 0, 0)
        log_layout.setSpacing(6)
        log_header = QHBoxLayout()
        log_header.addWidget(label("行动记录", "Section"))
        log_header.addStretch()
        self.focus_button = QPushButton("思路与交流")
        self.focus_button.setObjectName("openFocusWindow")
        self.focus_button.setToolTip("打开独立的大窗口，查看 AI 思路摘要并交流")
        self.focus_button.clicked.connect(self.open_focus)
        log_header.addWidget(self.focus_button)
        clear = QPushButton("清空显示")
        clear.setObjectName("TextButton")
        clear.clicked.connect(self.clear_log)
        log_header.addWidget(clear)
        log_layout.addLayout(log_header)
        self.log = QTextBrowser()
        self.log.setObjectName("actionLog")
        self.log.setAccessibleName("行动记录")
        self.log.setOpenExternalLinks(False)
        self.log.setMinimumHeight(90)
        self.log.document().setMaximumBlockCount(600)
        log_layout.addWidget(self.log, 1)
        self.records.addWidget(log_panel)
        chat_panel = QWidget()
        chat_layout = QVBoxLayout(chat_panel)
        chat_layout.setContentsMargins(0, 0, 0, 0)
        chat_layout.setSpacing(6)
        chat_header = QHBoxLayout()
        chat_header.addWidget(label("与 AI 交流", "Section"))
        self.chat_hint = label("暂停时也能交流", "Hint")
        chat_header.addStretch()
        chat_header.addWidget(self.chat_hint)
        chat_layout.addLayout(chat_header)
        self.chat = QTextBrowser()
        self.chat.setObjectName("aiConversation")
        self.chat.setAccessibleName("与 AI 的对话")
        self.chat.setOpenExternalLinks(False)
        self.chat.setMinimumHeight(96)
        self.chat.setPlaceholderText("发送指令或问题。AI 需要你做决定时，会在这里询问。")
        chat_layout.addWidget(self.chat, 1)
        self.confirm_panel = QWidget()
        confirm_row = QHBoxLayout(self.confirm_panel)
        confirm_row.setContentsMargins(0, 0, 0, 0)
        self.approve_button = QPushButton("同意本次操作")
        self.approve_button.setObjectName("approveOperation")
        self.approve_button.clicked.connect(lambda: self.answer_operation(True))
        self.deny_button = QPushButton("取消本次操作")
        self.deny_button.setObjectName("denyOperation")
        self.deny_button.clicked.connect(lambda: self.answer_operation(False))
        confirm_row.addWidget(self.approve_button)
        confirm_row.addWidget(self.deny_button)
        self.confirm_panel.setVisible(False)
        chat_layout.addWidget(self.confirm_panel)
        input_row = QHBoxLayout()
        self.chat_input = QLineEdit()
        self.chat_input.setObjectName("chatInput")
        self.chat_input.setAccessibleName("发送给 AI 的消息")
        self.chat_input.setMaxLength(8000)
        self.chat_input.setPlaceholderText("给 AI 下指令，或问一个问题…")
        self.chat_input.returnPressed.connect(self.send_chat)
        input_row.addWidget(self.chat_input, 1)
        self.send_button = QPushButton("发送")
        self.send_button.setObjectName("sendChat")
        self.send_button.clicked.connect(self.send_chat)
        input_row.addWidget(self.send_button)
        chat_layout.addLayout(input_row)
        self.records.addWidget(chat_panel)
        self.records.setSizes([155, 240])
        root.addWidget(self.records, 1)
        self.footer = label("进度和对话会自动保存在本机", "Footer")
        self.footer.setContentsMargins(23, 0, 23, 6)
        self.statusBar().setSizeGripEnabled(False)
        self.statusBar().setStyleSheet("QStatusBar { background: #f5f7fa; } QStatusBar::item { border: 0; }")
        self.statusBar().addWidget(self.footer, 1)
        self.focus_window = FocusWindow(self)
        self.focus_window.play_requested.connect(self.primary_action)
        self.focus_window.message_requested.connect(lambda text: self.send_chat_text(text, self.focus_window.chat_input))
        self.focus_window.confirmation_requested.connect(self.answer_operation)
        self.provider.setCurrentIndex(max(0, self.provider.findData(settings.public()["provider"])))
        self.provider.currentIndexChanged.connect(self.refresh_configuration)
        self.prompt.textChanged.connect(self.prompt_changed)
        self.prompt_timer = QTimer(self)
        self.prompt_timer.setSingleShot(True)
        self.prompt_timer.setInterval(800)
        self.prompt_timer.timeout.connect(self.save_choices)
        self.timer = QTimer(self)
        self.timer.setInterval(350)
        self.timer.timeout.connect(self.refresh)
        self.timer.start()
        self.add_log("system", "亲自打开游戏，选择进程并连接后，再点开始游玩。暂停后可随时继续。", time.time())
        self.refresh_configuration()

    def background(self, function, done=None, failed=None):
        def checked():
            try:
                return function()
            except Exception as error:
                self.runner.debug.exception("background_error", error)
                raise
        work = Work(checked)
        self.jobs.add(work)
        def success(value):
            self.jobs.discard(work)
            if done:
                done(value)
        def failure(message):
            self.jobs.discard(work)
            (failed or self.show_error)(message)
        work.signals.done.connect(success)
        work.signals.failed.connect(failure)
        self.pool.start(work)

    def choices(self):
        return {"provider": self.provider.currentData(), "game": self.current_game_id(), "system_prompt": self.prompt.toPlainText(),
                "game_process": self.target_process}

    def current_game_id(self):
        return self.target_game_id or next(spec.id for spec in game_specs().values() if spec.process_attach)

    def save_choices(self):
        try:
            if self.prompt.toPlainText().strip():
                self.settings.update(self.choices())
        except Exception as error:
            self.show_error(str(error))

    def prompt_changed(self):
        self.prompt_timer.start()
        self.prompt_hint.setText("暂停后继续时生效" if self.runner.snapshot()["active"] else "自动保存")

    def refresh_configuration(self, *unused):
        self.refresh()

    def refresh(self):
        state = self.runner.snapshot(self.cursor)
        self.cursor = state["event_cursor"]
        for event in state["events"]:
            if event["kind"] not in ("action", "chat", "question"):
                self.add_log(event["kind"], event["message"], event["time"])
        status, active = state["status"], state["active"]
        paused = status == "paused"
        editable = not active or paused
        self.provider.setEnabled(editable and not self.pending and not self.quitting)
        self.prompt.setReadOnly(not editable)
        self.primary.setProperty("paused", paused)
        self.primary.style().unpolish(self.primary)
        self.primary.style().polish(self.primary)
        selected = next(p for p in self.settings.public()["profiles"] if p["id"] == self.provider.currentData())
        needs_key = selected["id"] != "rehearsal" and not selected["has_key"] and not selected["base_url"].startswith("http://")
        spec = game_specs()[self.current_game_id()]
        target = self.target_process if self.target_game_id == spec.id else None
        same_connection = active and state.get("game") == spec.id and self.runner.config.get("game_process") == target
        self.process_button.setText(f"{target['name']} · {target['pid']}" if target else "选择进程…")
        self.process_button.setToolTip(f"{spec.label}\n{target['name']} · {target['pid']}" if target else "选择已打开、版本已核对的游戏进程")
        self.process_button.setEnabled(editable and not self.pending and not self.quitting and not state.get("chat_busy"))
        self.connect_button.setText("取消连接" if same_connection and status == "preparing" else "断开" if same_connection else "连接")
        self.connect_button.setEnabled(not self.pending and not self.quitting and (bool(target and spec.process_attach) or bool(same_connection)))
        self.privacy_hint.setText("免费演练使用本机固定脚本，不连接模型服务。" if selected["id"] == "rehearsal" else
                                 f"连接仅在本机进行。开始游玩或发送消息时，游戏状态、可见内容、提示词和对话会发送给 {selected['label']}；进程列表不会发送。")
        text = "继续游玩" if paused and state.get("ai_started") else "开始游玩" if paused else "暂停" if active else "接着玩" if self.has_saved_context() else "开始游玩"
        if state.get("save_error"):
            text = "重试保存"
        if status == "preparing":
            text = "正在连接游戏…"
        question = state.get("pending_question")
        if question:
            text = "等待你的回复"
        elif state.get("chat_busy"):
            text = "AI 正在回复…"
        if self.pending:
            text = self.pending_label if not self.quitting else "正在保存并退出…"
        self.primary.setText(text)
        self.primary.setAccessibleName(text)
        can_play = not spec.has_game_window or bool(same_connection and state.get("connected"))
        self.primary.setEnabled(not self.pending and not self.quitting and not question and not state.get("chat_busy")
                                and status not in ("preparing", "stopping") and (can_play or active and not paused))
        message = "先在设置中填写 API Key，然后开始游玩。" if needs_key and not active else state["message"]
        if spec.has_game_window and not active:
            message = state["message"] if status == "error" else "已选择进程，点连接后再开始游玩。" if target else "请先打开游戏，再选择游戏进程。"
            if not spec.process_attach:
                message = "此游戏的进程接入适配已暂停，请选择幼神 Demo。"
        self.status.setText(message)
        self.status.setToolTip(message)
        self.dot.setStyleSheet("color: " + ("#ba8227" if paused else "#278271" if active else "#8b9aab") + "; font-size: 12px;")
        ready = bool(same_connection and state.get("connected") and spec.process_attach)
        self.watch_button.setVisible(ready)
        self.watch_button.setEnabled(not self.pending and not self.quitting)
        self.refresh_chat(state)
        self.focus_window.sync(state, play_text=self.primary.text(), play_enabled=self.primary.isEnabled(),
                               pending=self.pending, chat_pending=self.chat_pending, quitting=self.quitting)
        if state.get("saved_at"):
            title = "对话已保存  " + time.strftime("%H:%M:%S", time.localtime(state["saved_at"]))
            if state.get("game_saved_at"):
                title += "    最近游戏存档  " + time.strftime("%H:%M:%S", time.localtime(state["game_saved_at"]))
            self.footer.setText(title)
        else:
            self.footer.setText("进度和对话会自动保存在本机")
        self.update_minimum_height()
        if self.quitting and not active and not self.pending:
            if status == "error":
                self.quitting = False
                QMessageBox.warning(self, "保存遇到问题", state["message"] + "\n窗口已保留，请检查存档目录。")
            else:
                self.close()
        elif self.quitting and state.get("save_error"):
            self.quitting = False
            QMessageBox.warning(self, "保存遇到问题", state["message"])
        self.last_status = status

    def update_minimum_height(self):
        width = max(480, self.width())
        content_height = max(self.root_layout.minimumSize().height(), self.root_layout.minimumHeightForWidth(width))
        self.setMinimumHeight(max(720, content_height + self.statusBar().sizeHint().height()))

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if hasattr(self, "footer"):
            self.update_minimum_height()

    def has_saved_context(self):
        game_id = self.current_game_id()
        return (self.settings.directory / "sessions" / f"{game_id}.json").exists()

    def primary_action(self):
        state = self.runner.snapshot()
        if state.get("save_error"):
            self.runner.stop()
            self.refresh()
            return
        if state["active"] and state["status"] != "paused":
            self.pending_label = "正在暂停 AI…"
            self.pending = True
            self.refresh()
            self.background(self.runner.pause, self.operation_done, self.operation_error)
            return
        spec = game_specs()[self.current_game_id()]
        if spec.has_game_window and (not state["active"] or not state.get("connected")
                                    or state.get("game") != spec.id or self.runner.config.get("game_process") != self.target_process):
            self.show_error("请先选择已经打开的游戏进程并连接，再开始游玩。")
            return
        selected = next(p for p in self.settings.public()["profiles"] if p["id"] == self.provider.currentData())
        if selected["id"] != "rehearsal" and not selected["has_key"] and not selected["base_url"].startswith("http://"):
            self.open_settings()
            return
        try:
            options = self.choices()
            self.settings.update(options)
            self.open_focus()
            if state["active"] and options["game"] != state["game"]:
                self.pending_label = "正在保存…"
                self.pending = True
                self.refresh()
                def switch():
                    self.runner.stop()
                    self.runner.thread.join(timeout=60)
                    if self.runner.thread.is_alive():
                        raise RuntimeError("还在保存上一个游戏，请稍后再切换。")
                    if self.runner.snapshot()["status"] == "error":
                        raise RuntimeError(self.runner.snapshot()["message"])
                    return self.runner.start(options)
                self.background(switch, self.started, self.operation_error)
            elif state["active"]:
                self.pending_label = "正在连接游戏…"
                self.pending = True
                self.refresh()
                self.background(lambda: self.runner.resume(options), self.operation_done, self.operation_error)
            else:
                self.started(self.runner.start(options))
        except Exception as error:
            self.show_error(str(error))

    def started(self, result):
        self.pending = False
        self.cursor = 0
        self.log.clear()
        self.refresh()

    def operation_done(self, result):
        self.pending = False
        self.refresh()

    def operation_error(self, message):
        self.pending = False
        self.show_error(message)
        self.refresh()

    def add_log(self, kind, message, timestamp):
        color = {"error": "#ad493d", "success": "#276d62", "thought": "#213246", "notice": "#725f35"}.get(kind, "#68798a")
        stamp = time.strftime("%H:%M:%S", time.localtime(timestamp))
        scroll = self.log.verticalScrollBar()
        at_bottom = scroll.value() >= scroll.maximum() - 15
        self.log.append(f'<p style="margin: 0 0 10px 0; line-height: 145%;"><span style="font-size: 11px; color: #8b99a9;">{stamp}</span><br><span style="color: {color};">{html.escape(str(message)).replace(chr(10), "<br>")}</span></p>')
        if at_bottom:
            scroll.setValue(scroll.maximum())

    def clear_log(self):
        self.log.clear()

    def refresh_chat(self, state):
        question = state.get("pending_question")
        confirmation = bool(question and question["kind"] == "confirmation")
        self.confirm_panel.setVisible(confirmation)
        question_id = question["id"] if question else None
        new_question = question_id is not None and question_id != self.question_rendered
        self.question_rendered = question_id
        conversation = state.get("conversation", [])
        identity = tuple(entry["id"] for entry in conversation)
        if identity != self.chat_rendered:
            scroll = self.chat.verticalScrollBar()
            position, at_bottom = scroll.value(), scroll.value() >= scroll.maximum() - 15
            paragraphs = []
            for entry in conversation:
                role = {"user": "你", "assistant": "AI", "system": "提示"}[entry["role"]]
                color = "#2c5ee8" if entry["role"] == "user" else "#725f35" if entry["role"] == "system" or entry.get("question_id") else "#213246"
                stamp = time.strftime("%H:%M:%S", time.localtime(entry["time"]))
                content = html.escape(entry["message"]).replace("\n", "<br>")
                paragraphs.append(f'<p style="margin: 0 0 10px 0; line-height: 145%;"><span style="font-size: 11px; color: #8b99a9;">{role}　{stamp}</span><br><span style="color: {color};">{content}</span></p>')
            self.chat.setHtml("".join(paragraphs))
            scroll.setValue(scroll.maximum() if at_bottom else position)
            if at_bottom or new_question:
                # Showing confirmation controls changes viewport height. Scroll
                # after that layout so the question itself remains visible.
                QTimer.singleShot(0, self.scroll_chat_bottom)
            self.chat_rendered = identity
        self.chat_hint.setText("等待你确认" if confirmation else "等待你回答" if question else "正在回复…" if state.get("chat_busy") else "暂停时也能交流")
        enabled = not self.quitting and not self.chat_pending and state["status"] != "stopping"
        self.approve_button.setEnabled(enabled and not state.get("chat_busy"))
        self.deny_button.setEnabled(enabled and not state.get("chat_busy"))
        self.send_button.setEnabled(enabled)
        self.chat_input.setEnabled(not self.quitting and state["status"] != "stopping")
        self.chat_input.setPlaceholderText("回复‘同意’或‘取消’，也可以先提问…" if confirmation else
                                           "在这里回答 AI 的问题…" if question else "给 AI 下指令，或问一个问题…")

    def scroll_chat_bottom(self):
        scroll = self.chat.verticalScrollBar()
        scroll.setValue(scroll.maximum())

    def send_chat(self):
        self.send_chat_text(self.chat_input.text(), self.chat_input)

    def send_chat_text(self, text, source):
        text = text.strip()
        if not text or self.chat_pending or self.quitting:
            return
        options = self.choices()
        self.chat_pending = True
        self.refresh()
        def done(result):
            self.chat_pending = False
            current = source.text() if isinstance(source, QLineEdit) else source.toPlainText()
            if current.strip() == text:
                source.clear()
            self.refresh()
        def send_request():
            if self.quitting:
                return {"ok": False, "cancelled": True}
            return self.runner.send_message(text, options)
        self.background(send_request, done, self.chat_error)

    def answer_operation(self, approved):
        question = self.runner.snapshot().get("pending_question")
        if not question or question["kind"] != "confirmation" or self.chat_pending:
            return
        self.chat_pending = True
        self.refresh()
        reply = "同意本次操作" if approved else "取消本次操作"
        self.background(lambda: self.runner.answer_question(question["id"], reply, approved=approved),
                        self.chat_done, self.chat_error)

    def chat_done(self, result):
        self.chat_pending = False
        self.refresh()

    def chat_error(self, message):
        self.chat_pending = False
        self.runner._chat_entry("system", message)
        self.refresh()

    def show_error(self, message):
        self.runner.debug.write("ui_error", message)
        self.add_log("error", message, time.time())
        self.status.setText(message)
        self.focus_window.add_events([{"id": "ui-" + str(time.time_ns()), "kind": "error", "message": message, "time": time.time()}])

    def open_focus(self):
        self.focus_window.show()
        if self.focus_window.isMinimized():
            self.focus_window.showNormal()
        self.focus_window.raise_()
        self.focus_window.activateWindow()

    def watch_game(self):
        if self.runner.game:
            self.background(self.runner.game.watch, lambda result: self.show_error(result["message"]) if not result.get("ok") else None)

    def select_process(self):
        dialog = ProcessDialog(self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.target_process, self.target_game_id = dialog.selection, dialog.game_id
            self.runner.debug.write("process_selected", "已选择本机游戏进程。", game=dialog.game_id, pid=dialog.selection["pid"])
            self.refresh()

    def connect_process(self):
        state = self.runner.snapshot()
        options = self.choices()
        if state["active"] and self.runner.config.get("game_process") == options["game_process"]:
            self.runner.stop()
            self.refresh()
            return
        if not options["game_process"] or not game_specs()[options["game"]].process_attach:
            self.show_error("请先选择已经打开且已有适配器的游戏进程。")
            return
        options["connect_only"] = True
        if state["active"]:
            self.pending_label, self.pending = "正在切换连接…", True
            self.refresh()
            def switch():
                self.runner.stop()
                self.runner.thread.join(timeout=60)
                if self.runner.thread.is_alive():
                    raise RuntimeError("还在保存上一个游戏，请稍后再切换。")
                if self.runner.snapshot()["status"] == "error":
                    raise RuntimeError(self.runner.snapshot()["message"])
                return self.runner.start(options)
            self.background(switch, self.started, self.operation_error)
        else:
            try:
                self.started(self.runner.start(options))
            except Exception as error:
                self.show_error(str(error))

    def open_debug(self):
        DebugDialog(self).exec()

    def open_settings(self):
        SettingsDialog(self).exec()
        self.refresh_configuration()

    def closeEvent(self, event):
        self.prompt_timer.stop()
        if self.runner.snapshot()["active"]:
            event.ignore()
            if not self.quitting:
                self.save_choices()
                self.quitting = True
                self.runner.stop()
                self.refresh()
            return
        self.save_choices()
        self.quitting = True
        self.runner.stop()
        self.focus_window.close()
        event.accept()


def main(argv=None):
    parser = argparse.ArgumentParser(description="PlayALL 桌面软件")
    parser.add_argument("--data-dir", type=Path)
    parser.add_argument("--no-auto-game", action="store_true", help="调试时不自动打开游戏客户端")
    parser.add_argument("--verify-installation", type=Path, help="后台检查安装并将结果写入指定 JSON 文件")
    args = parser.parse_args(argv)
    if args.verify_installation:
        os.environ["QT_QPA_PLATFORM"] = "offscreen"
    app = QApplication(sys.argv[:1])
    app.setApplicationName("PlayALL")
    app.setOrganizationName("PlayALL")
    app.setStyle("Fusion")
    app.setFont(QFont("Microsoft YaHei UI", 10))
    app.setStyleSheet(STYLE)
    if args.verify_installation:
        result = {"surface": "native Qt Widgets", "frozen": bool(getattr(sys, "frozen", False)),
                  "platform": app.platformName(), "game_started": False, "ai_started": False}
        try:
            from .config import ROOT
            from .games.registry import resolve
            with tempfile.TemporaryDirectory(prefix="playall-install-check-") as directory:
                fixture = Settings(Path(directory))
                window = MainWindow(fixture, auto_open_game=False)
                dialog = SettingsDialog(window)
                specs = game_specs()
                for spec in specs.values():
                    assert callable(resolve(spec.factory))
                    assert spec.tools()
                    if spec.process_attach:
                        assert callable(resolve(spec.process_verifier))
                        assert callable(resolve(spec.connection_preparer))
                        selected_index = dialog.adapter.findData(spec.id)
                        assert selected_index >= 0
                        dialog.adapter.setCurrentIndex(selected_index)
                        assert dialog.path_key == spec.path_setting
                index = dialog.adapter.findData("aistalt")
                assert index >= 0 and specs[window.current_game_id()].process_attach
                assert window.findChild(QComboBox, "gameSelect") is None
                assert window.chat and window.chat_input and window.approve_button
                assert window.focus_window.isWindow() and window.focus_window.feed and window.focus_window.chat_input
                assert not window.focus_window.isVisible()
                dialog.adapter.setCurrentIndex(index)
                assert dialog.path_key == "aistalt_path"
                # A source-only Factorio build can omit the optional Unity
                # bridge. It must be compiled against the user's own game API.
                unity_bridge = ROOT / "mods/aistalt-bridge/PlayALL.Aistalt.dll"
                result["optional_components"] = {"aistalt_bridge": "built" if unity_bridge.is_file() else "not built"}
                if unity_bridge.is_file():
                    assert (unity_bridge.parent / "build.json").is_file()
                from .games.factorio_attach import BUNDLE
                assert (BUNDLE / "control.lua").is_file()
                assert not window.runner.snapshot()["active"]
                assert not window.isVisible() and not dialog.isVisible()
                result["games"] = list(specs)
                dialog.close()
                window.close()
                app.processEvents()
            result["status"] = "passed"
        except Exception as error:
            result.update(status="failed", error=type(error).__name__ + ": " + str(error))
        atomic_json(args.verify_installation, result)
        return 0 if result["status"] == "passed" else 1
    settings = Settings(args.data_dir)
    name = "PlayALL-" + hashlib.sha256(str(settings.directory.resolve()).lower().encode()).hexdigest()[:20]
    client = QLocalSocket()
    client.connectToServer(name)
    if client.waitForConnected(250):
        client.write(b"show")
        client.waitForBytesWritten(250)
        return 0
    server = QLocalServer()
    server.setSocketOptions(QLocalServer.SocketOption.UserAccessOption)
    QLocalServer.removeServer(name)
    if not server.listen(name):
        QMessageBox.critical(None, "PlayALL", "无法创建本机软件窗口，请稍后重试。")
        return 1
    window = MainWindow(settings, auto_open_game=not args.no_auto_game)
    def report_exception(error_type, error, trace):
        window.runner.debug.exception("unhandled_error", error)
        window.show_error("软件遇到错误，详情已记录到日志：" + str(error)[:1000])
    sys.excepthook = report_exception
    def activate():
        socket = server.nextPendingConnection()
        socket.disconnectFromServer()
        socket.deleteLater()
        window.showNormal()
        window.raise_()
        window.activateWindow()
    server.newConnection.connect(activate)
    window.show()
    result = app.exec()
    server.close()
    return result


if __name__ == "__main__":
    raise SystemExit(main())
