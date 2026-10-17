"""Exercise and render the actual Qt widgets in an isolated local practice game."""
import json
import os
import sys
import tempfile
import time
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.pop("DEEPSEEK_API_KEY", None)
os.environ.pop("PLAYALL_API_KEY", None)
from PySide6.QtCore import Qt, QPoint
from PySide6.QtTest import QTest
from PySide6.QtGui import QFontDatabase, QFont
from PySide6.QtWidgets import QApplication
from playall.config import Settings
from playall.desktop import MainWindow, SettingsDialog, ProcessDialog, DebugDialog, STYLE
from playall.processes import GameProcess


def main():
    output = Path(__file__).resolve().parents[1] / "data/qa/native"
    output.mkdir(parents=True, exist_ok=True)
    app = QApplication([])
    if app.platformName() == "offscreen":
        for name in ("msyh.ttc", "msyhbd.ttc", "segoeui.ttf", "segoeuib.ttf"):
            QFontDatabase.addApplicationFont(str(Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts" / name))
    app.setFont(QFont("Microsoft YaHei UI", 10))
    app.setStyle("Fusion")
    app.setStyleSheet(STYLE)
    evidence = {"surface": "native Qt Widgets", "browser_used": False, "checks": []}
    with tempfile.TemporaryDirectory(prefix="playall-qt-") as directory, ExitStack() as cleanup:
        settings = Settings(Path(directory))
        window = MainWindow(settings, auto_open_game=False)
        def shutdown():
            window.runner.stop()
            if window.runner.thread:
                window.runner.thread.join(10)
        cleanup.callback(shutdown)
        window.show()
        def wait_for(condition, seconds=15):
            deadline = time.monotonic() + seconds
            while not condition():
                app.processEvents()
                if time.monotonic() > deadline:
                    raise AssertionError(window.runner.snapshot())
                QTest.qWait(30)
            app.processEvents()
        def capture(name, widget=window):
            app.processEvents()
            QTest.qWait(150)
            widget.grab().save(str(output / f"{name}.png"))
        def assert_layout():
            app.processEvents()
            log_bottom = window.log.mapTo(window, QPoint(0, window.log.height())).y()
            chat_top = window.chat.mapTo(window, QPoint(0, 0)).y()
            bottom = window.chat_input.mapTo(window, QPoint(0, window.chat_input.height())).y()
            footer_top = window.footer.mapTo(window, QPoint(0, 0)).y()
            assert bottom <= footer_top, (bottom, footer_top, window.size())
            assert log_bottom < chat_top and window.log.height() >= 90 and window.chat.height() >= 96
            assert window.statusBar().geometry().bottom() < window.height()
            return {"width": window.width(), "height": window.height(), "record_bottom": log_bottom,
                    "chat_top": chat_top, "input_bottom": bottom, "footer_top": footer_top}
        capture("idle")
        assert_layout()
        assert all(item.text() != "让 AI 接着玩" for item in window.findChildren(type(window.status)))
        assert not hasattr(window, "game"), "Main surface must not contain a game selector"
        assert not window.primary.isEnabled()
        fake = GameProcess(1234, "Aistalt.exe", str(Path(directory) / "Aistalt.exe"), 1)
        factory = GameProcess(77, "factorio.exe", str(Path(directory) / "factorio.exe"), 2)
        with patch("playall.desktop.list_processes", return_value=[GameProcess(23, "Other.exe"), factory, fake]), \
             patch("playall.desktop.inspect_process", side_effect=lambda pid: {77: factory, 1234: fake}[pid]) as inspect, \
             patch("playall.games.aistalt.is_supported_process", return_value=True), \
             patch("playall.games.factorio_attach.is_supported_process", return_value=True):
            picker = ProcessDialog(window)
            picker.show()
            assert picker.table.topLevelItemCount() == 2
            assert [call.args[0] for call in inspect.call_args_list] == [1234, 77], "Unrelated processes must not be inspected"
            picker.table.setCurrentItem(picker.table.topLevelItem(0))
            assert picker.select.isEnabled()
            capture("processes", picker)
            picker.choose()
            assert picker.selection["pid"] == 1234 and picker.game_id == "aistalt"
            window.target_process, window.target_game_id = picker.selection, picker.game_id
            window.refresh()
        with patch.object(window.runner, "start", return_value={"ok": True}) as start:
            QTest.mouseClick(window.connect_button, Qt.MouseButton.LeftButton)
            assert start.call_args.args[0]["connect_only"] is True
            assert start.call_args.args[0]["game_process"]["pid"] == 1234
            assert not window.primary.isEnabled()
        evidence["checks"].append("process selection sends connect_only; AI start requires a connected process")
        evidence["checks"].append("only adapted, verified game processes are listed; no game selector on main surface")
        window.runner.debug.remember_secret("synthetic-private-key")
        window.runner.debug.write("connect", "synthetic-private-key", error="synthetic connection failure")
        debug = DebugDialog(window)
        debug.show()
        capture("debug", debug)
        assert "synthetic-private-key" not in debug.text.toPlainText()
        assert "synthetic connection failure" in debug.text.toPlainText()
        debug.close()
        evidence["checks"].append("native debug viewer displays connection errors with credentials redacted")
        layout_cases = []
        for width, height in ((480, 640), (520, 760), (620, 900)):
            window.resize(width, height)
            app.processEvents()
            layout_cases.append(assert_layout())
        window.resize(560, 840)
        evidence["layout_cases"] = layout_cases
        evidence["checks"].append("minimum window height reserves a separate footer and keeps action records unobscured")
        assert not any(text in window.findChild(type(window.status), "playStatus").text() for text in ("token", "目标"))
        dialog = SettingsDialog(window)
        dialog.show()
        assert {dialog.adapter.itemData(i) for i in range(dialog.adapter.count())} == {"factorio", "aistalt"}
        dialog.adapter.setCurrentIndex(dialog.adapter.findData("factorio"))
        assert dialog.prepare_button.isVisible()
        capture("settings", dialog)
        dialog.close()
        window.provider.setCurrentIndex(window.provider.findData("rehearsal"))
        # The practice game is an internal QA target, never a selectable process.
        window.target_game_id, window.target_process = "sandbox", None
        window.refresh()
        QTest.mouseClick(window.primary, Qt.MouseButton.LeftButton)
        wait_for(lambda: (window.runner.snapshot().get("observation") or {}).get("iron_plates_produced", 0) >= 12, 25)
        wait_for(lambda: window.runner.snapshot()["actions"] >= 10, 10)
        game = window.runner.game
        capture("running")
        QTest.mouseClick(window.primary, Qt.MouseButton.LeftButton)
        wait_for(lambda: window.runner.snapshot()["status"] == "paused" and not window.pending)
        paused = window.runner.snapshot()
        QTest.qWait(800)
        assert window.runner.snapshot()["actions"] == paused["actions"]
        assert window.runner.snapshot()["observation"]["tick"] == paused["observation"]["tick"]
        assert window.runner.thread.is_alive()
        capture("paused")
        assert_layout()
        evidence["checks"].append("pause freezes actions and game ticks while retaining the live game")
        window.chat_input.setText("当前有什么资源？")
        QTest.keyClick(window.chat_input, Qt.Key.Key_Return)
        wait_for(lambda: not window.chat_pending and not window.runner.snapshot().get("chat_busy") and
                 len(window.runner.snapshot()["conversation"]) >= 2)
        assert window.runner.snapshot()["actions"] == paused["actions"]
        assert "当前有什么资源？" in window.chat.toPlainText()
        assert "当前有什么资源？" not in window.log.toPlainText()
        capture("paused-chat")
        evidence["checks"].append("chat below action records accepts Enter, renders the reply, and does not start a paused game")
        window.runner._ask("要新建游戏吗？回答前不会执行后续操作。", action={"action": "mine", "name": "coal", "count": 1},
                           review={"ticket": "f" * 32})
        window.refresh()
        assert not window.primary.isEnabled() and window.approve_button.isVisible()
        assert_layout()
        capture("confirmation")
        QTest.mouseClick(window.deny_button, Qt.MouseButton.LeftButton)
        wait_for(lambda: not window.chat_pending and not window.runner.snapshot().get("pending_question"))
        assert not window.runner.resume_event.is_set()
        assert window.runner.snapshot()["actions"] == paused["actions"]
        evidence["checks"].append("pending question disables Continue; cancel retains pause and blocks subsequent actions")
        window.prompt.setPlainText("持续生产，遇到失败先检查原因；我按暂停时保留进度。")
        QTest.mouseClick(window.primary, Qt.MouseButton.LeftButton)
        wait_for(lambda: window.runner.snapshot()["actions"] > paused["actions"])
        assert window.runner.game is game
        assert window.runner.snapshot()["system_prompt"] == window.prompt.toPlainText()
        evidence["checks"].append("resume keeps the same adapter and applies the edited long-term prompt")
        window.close()
        wait_for(lambda: not window.runner.snapshot()["active"], 15)
        app.processEvents()
        report = window.runner.snapshot()
        assert report["status"] == "stopped", report
        session = json.loads((settings.directory / "sessions/sandbox.json").read_text(encoding="utf-8"))
        assert session["history"] and session["counters"]["actions"] >= paused["actions"]
        evidence["checks"].append("native window close saves game and complete AI conversation")
        evidence["status"] = "Passed"
        evidence["actions"] = report["actions"]
        evidence["iron_plates_produced"] = report["observation"]["iron_plates_produced"]
    (output / "evidence.json").write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(evidence, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
