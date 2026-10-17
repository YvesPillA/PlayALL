"""A separate reading surface for public AI summaries and player conversation.

This view owns no game connection, model client or approval state. All controls
are signals handled by the main window's existing Runner.
"""
from __future__ import annotations

import html
import time

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QTextCursor
from PySide6.QtWidgets import (QFrame, QHBoxLayout, QLabel, QMainWindow, QPlainTextEdit,
    QPushButton, QSplitter, QTextBrowser, QVBoxLayout, QWidget)


def escaped(value):
    return html.escape(str(value)).replace("\n", "<br>")


class MessageEditor(QPlainTextEdit):
    submitted = Signal()

    def __init__(self):
        super().__init__()
        self.composing = False

    def inputMethodEvent(self, event):
        self.composing = bool(event.preeditString())
        super().inputMethodEvent(event)

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter) and not self.composing and not event.modifiers() & Qt.KeyboardModifier.ShiftModifier:
            self.submitted.emit()
            event.accept()
        else:
            super().keyPressEvent(event)


class FocusWindow(QMainWindow):
    play_requested = Signal()
    message_requested = Signal(str)
    confirmation_requested = Signal(bool)

    def __init__(self, owner):
        super().__init__(owner, Qt.WindowType.Window)
        self.setObjectName("aiFocusWindow")
        self.setWindowTitle("PlayALL · AI 思路与交流")
        self.setWindowIcon(owner.windowIcon())
        self.resize(1180, 820)
        self.setMinimumSize(860, 620)
        self.reading_size = 16
        self.run_id = None
        self.events = []
        self.event_ids = set()
        self.conversation = []
        self.chat_identity = None
        self.question_id = None
        self.waiting_for_reply = False
        body = QWidget()
        self.setCentralWidget(body)
        root = QVBoxLayout(body)
        root.setContentsMargins(26, 22, 26, 22)
        root.setSpacing(14)
        header = QHBoxLayout()
        titles = QVBoxLayout()
        titles.setSpacing(4)
        title = QLabel("AI 思路与交流")
        title.setStyleSheet("font-size:22px;font-weight:600;")
        titles.addWidget(title)
        self.subtitle = QLabel("观察它如何计划，也随时告诉它你的想法。")
        self.subtitle.setStyleSheet("color:#68798a;font-size:12px;")
        titles.addWidget(self.subtitle)
        header.addLayout(titles, 1)
        self.smaller = QPushButton("A−")
        self.smaller.setAccessibleName("减小阅读字号")
        self.larger = QPushButton("A+")
        self.larger.setAccessibleName("增大阅读字号")
        self.smaller.clicked.connect(lambda: self.change_size(-2))
        self.larger.clicked.connect(lambda: self.change_size(2))
        header.addWidget(self.smaller)
        header.addWidget(self.larger)
        self.latest = QPushButton("回到最新")
        self.latest.clicked.connect(self.scroll_latest)
        header.addWidget(self.latest)
        self.play = QPushButton("开始游玩")
        self.play.setObjectName("Primary")
        self.play.setMinimumWidth(134)
        self.play.clicked.connect(self.play_requested.emit)
        header.addWidget(self.play)
        root.addLayout(header)
        self.status = QLabel("尚未连接游戏")
        self.status.setWordWrap(True)
        self.status.setStyleSheet("color:#68798a;padding:5px 0;")
        root.addWidget(self.status)
        self.columns = QSplitter(Qt.Orientation.Horizontal)
        self.columns.setChildrenCollapsible(False)
        self.columns.setHandleWidth(14)
        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.setSpacing(8)
        heading = QLabel("思路与行动")
        heading.setStyleSheet("font-weight:600;font-size:14px;")
        left_layout.addWidget(heading)
        self.feed = QTextBrowser()
        self.feed.setObjectName("focusFeed")
        self.feed.setAccessibleName("AI 公开的思路摘要、行动和结果")
        self.feed.setOpenExternalLinks(False)
        self.feed.setMinimumHeight(140)
        left_layout.addWidget(self.feed, 1)
        self.columns.addWidget(left)
        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(8)
        chat_heading = QHBoxLayout()
        title = QLabel("与 AI 对话")
        title.setStyleSheet("font-weight:600;font-size:14px;")
        chat_heading.addWidget(title)
        chat_heading.addStretch()
        self.chat_hint = QLabel("暂停时也能交流")
        self.chat_hint.setStyleSheet("font-size:12px;color:#68798a;")
        chat_heading.addWidget(self.chat_hint)
        right_layout.addLayout(chat_heading)
        self.chat = QTextBrowser()
        self.chat.setObjectName("focusConversation")
        self.chat.setAccessibleName("玩家与 AI 的对话")
        self.chat.setOpenExternalLinks(False)
        self.chat.setMinimumHeight(120)
        right_layout.addWidget(self.chat, 1)
        self.question = QFrame()
        self.question.setObjectName("focusQuestion")
        self.question.setStyleSheet("QFrame#focusQuestion{background:#fff3dc;border:1px solid #e7cd97;border-radius:8px;}")
        question_layout = QVBoxLayout(self.question)
        question_layout.setContentsMargins(13, 11, 13, 11)
        question_layout.setSpacing(8)
        self.question_title = QLabel("等待你的回复")
        self.question_title.setStyleSheet("font-weight:600;color:#805d19;")
        question_layout.addWidget(self.question_title)
        self.question_text = QLabel()
        self.question_text.setTextFormat(Qt.TextFormat.PlainText)
        self.question_text.setWordWrap(True)
        self.question_text.setMaximumHeight(95)
        question_layout.addWidget(self.question_text)
        self.confirm_panel = QWidget()
        self.confirm_panel.setFixedHeight(40)
        buttons = QHBoxLayout(self.confirm_panel)
        buttons.setContentsMargins(0, 0, 0, 0)
        self.approve = QPushButton("同意本次操作")
        self.deny = QPushButton("取消本次操作")
        self.approve.clicked.connect(lambda: self.confirmation_requested.emit(True))
        self.deny.clicked.connect(lambda: self.confirmation_requested.emit(False))
        buttons.addWidget(self.approve)
        buttons.addWidget(self.deny)
        question_layout.addWidget(self.confirm_panel)
        self.question.setVisible(False)
        right_layout.addWidget(self.question)
        self.chat_input = MessageEditor()
        self.chat_input.setObjectName("focusChatInput")
        self.chat_input.setAccessibleName("发送给 AI 的消息")
        self.chat_input.setPlaceholderText("给 AI 下指令，或问一个问题…")
        self.chat_input.setStyleSheet("font-size:15px;")
        self.chat_input.setFixedHeight(86)
        self.chat_input.submitted.connect(self.submit_message)
        right_layout.addWidget(self.chat_input)
        input_footer = QHBoxLayout()
        hint = QLabel("Enter 发送，Shift+Enter 换行")
        hint.setStyleSheet("font-size:12px;color:#68798a;")
        input_footer.addWidget(hint, 1)
        self.send = QPushButton("发送")
        self.send.clicked.connect(self.submit_message)
        input_footer.addWidget(self.send)
        right_layout.addLayout(input_footer)
        self.columns.addWidget(right)
        self.columns.setStretchFactor(0, 3)
        self.columns.setStretchFactor(1, 2)
        self.columns.setSizes([680, 420])
        root.addWidget(self.columns, 1)
        self.render_feed()
        self.render_chat()

    def submit_message(self):
        text = self.chat_input.toPlainText().strip()
        if text and self.send.isEnabled():
            self.message_requested.emit(text)

    def document(self, content):
        return ('<html><head><style>body{font-family:"Microsoft YaHei UI";font-size:' + str(self.reading_size) +
                'px;color:#213246;}p{line-height:160%;margin:0 0 18px 0;}</style></head><body>' + content + '</body></html>')

    @staticmethod
    def replace(browser, content, force_bottom=False):
        scroll = browser.verticalScrollBar()
        before, bottom = scroll.value(), scroll.value() >= scroll.maximum() - 18
        selection = browser.textCursor()
        anchor, position = selection.anchor(), selection.position()
        browser.setHtml(content)
        if anchor != position:
            maximum = browser.document().characterCount() - 1
            selection = browser.textCursor()
            selection.setPosition(min(anchor, maximum))
            selection.setPosition(min(position, maximum), QTextCursor.MoveMode.KeepAnchor)
            browser.setTextCursor(selection)
        scroll.setValue(scroll.maximum() if bottom or force_bottom else before)

    def render_feed(self):
        labels = {"thought":"思路摘要", "action":"行动", "result":"结果", "success":"进展",
                  "error":"需要注意", "notice":"提示", "system":"连接"}
        verbs = {"observe":"观察", "inspect":"查看", "recipes":"查询配方", "move":"移动", "mine":"采集",
                 "craft":"制作", "build":"建造", "insert":"放入物品", "extract":"取回物品", "wait":"等待",
                 "set_recipe":"设置配方", "research":"开始科研", "rotate":"旋转", "deconstruct":"回收建筑",
                 "advance":"推进对话", "click":"选择", "scroll":"滚动", "press":"按住"}
        paragraphs = []
        for event in self.events:
            kind = event["kind"]
            color = "#2c5ee8" if kind == "thought" else "#ad493d" if kind == "error" else "#276d62" if kind == "success" else "#68798a"
            stamp = time.strftime("%H:%M:%S", time.localtime(event["time"]))
            message = event["message"]
            if kind == "action":
                action = event.get("action", {})
                message = verbs.get(action.get("action"), "执行游戏操作")
                if action.get("name"):
                    message += "：" + str(action["name"])
                if action.get("count"):
                    message += " × " + str(action["count"])
            paragraphs.append('<p><span style="font-size:12px;color:' + color + ';">' +
                              escaped(labels.get(kind, "记录")) + '　' + stamp + '</span><br>' + escaped(message) + '</p>')
        content = "".join(paragraphs) or '<p style="color:#68798a;">开始游玩后，这里会显示 AI 的观察摘要、计划与行动结果。</p>'
        self.replace(self.feed, self.document(content))

    def render_chat(self, force_bottom=False):
        paragraphs = []
        for entry in self.conversation:
            role = {"user":"你", "assistant":"AI", "system":"提示"}.get(entry["role"], "提示")
            color = "#2c5ee8" if entry["role"] == "user" else "#805d19" if entry.get("question_id") or entry["role"] == "system" else "#68798a"
            stamp = time.strftime("%H:%M:%S", time.localtime(entry["time"]))
            paragraphs.append('<p><span style="font-size:12px;color:' + color + ';">' + role + '　' + stamp +
                              '</span><br>' + escaped(entry["message"]) + '</p>')
        content = "".join(paragraphs) or '<p style="color:#68798a;">随时发送问题或指令。需要你做决定时，AI 会在这里等待回复。</p>'
        self.replace(self.chat, self.document(content), force_bottom)

    def add_events(self, events):
        changed = False
        for event in events:
            if event.get("kind") not in ("thought", "action", "result", "success", "error", "notice", "system") or event["id"] in self.event_ids:
                continue
            self.events.append({key: event[key] for key in ("id", "kind", "message", "time", "action") if key in event})
            self.event_ids.add(event["id"])
            changed = True
        if len(self.events) > 600:
            self.events = self.events[-600:]
            self.event_ids = {event["id"] for event in self.events}
        if changed:
            self.render_feed()

    def sync(self, state, *, play_text, play_enabled, pending=False, chat_pending=False, quitting=False):
        if state.get("run_id") != self.run_id:
            self.run_id = state.get("run_id")
            self.events, self.event_ids = [], set()
            self.render_feed()
        self.add_events(state.get("events", []))
        self.play.setText(play_text)
        self.play.setAccessibleName(play_text)
        self.play.setEnabled(play_enabled)
        paused = state["status"] == "paused"
        self.play.setProperty("paused", paused)
        self.play.style().unpolish(self.play)
        self.play.style().polish(self.play)
        self.subtitle.setText(state.get("provider_label", "AI") + " · " +
                              ("已连接游戏" if state.get("connected") else "等待连接游戏"))
        self.status.setText(state.get("message", "尚未连接游戏"))
        self.status.setStyleSheet("padding:5px 0;color:" + ("#805d19" if state.get("pending_question") else "#276d62" if paused else "#68798a") + ";")
        question = state.get("pending_question")
        new_question = bool(question and question["id"] != self.question_id)
        self.question_id = question["id"] if question else None
        self.waiting_for_reply = bool(question)
        self.setMinimumHeight(700 if question else 620)
        self.arrange_columns()
        self.question.setVisible(bool(question))
        confirmation = bool(question and question["kind"] == "confirmation")
        self.confirm_panel.setVisible(confirmation)
        self.question.setMinimumHeight(188 if confirmation else 144 if question else 0)
        if question:
            self.question_title.setText("需要你确认，AI 已暂停" if confirmation else "等待你的回答，AI 已暂停")
            self.question_text.setText(question["question"])
            self.question_text.setToolTip(question["question"])
        identity = tuple((entry["id"], entry["message"]) for entry in state.get("conversation", []))
        if identity != self.chat_identity:
            self.conversation = state.get("conversation", [])
            self.chat_identity = identity
            self.render_chat(new_question)
        enabled = not quitting and not chat_pending and state["status"] != "stopping"
        self.send.setEnabled(enabled)
        self.chat_input.setEnabled(not quitting and state["status"] != "stopping")
        self.approve.setEnabled(enabled and not state.get("chat_busy"))
        self.deny.setEnabled(enabled and not state.get("chat_busy"))
        self.chat_hint.setText("等待你确认" if confirmation else "等待你回答" if question else "正在回复…" if state.get("chat_busy") else "暂停时也能交流")
        self.chat_input.setPlaceholderText("回复‘同意’或‘取消’，也可以先提问…" if confirmation else
                                          "在这里回答 AI 的问题…" if question else "给 AI 下指令，或问一个问题…")

    def change_size(self, amount):
        self.reading_size = max(14, min(24, self.reading_size + amount))
        self.smaller.setEnabled(self.reading_size > 14)
        self.larger.setEnabled(self.reading_size < 24)
        self.render_feed()
        self.render_chat()

    def scroll_latest(self):
        for browser in (self.feed, self.chat):
            browser.verticalScrollBar().setValue(browser.verticalScrollBar().maximum())

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.arrange_columns()

    def arrange_columns(self):
        if hasattr(self, "columns"):
            orientation = Qt.Orientation.Vertical if self.width() < 980 and not self.waiting_for_reply else Qt.Orientation.Horizontal
            if self.columns.orientation() != orientation:
                self.columns.setOrientation(orientation)
                self.columns.setSizes([350, 350] if orientation == Qt.Orientation.Vertical else [680, 420])
