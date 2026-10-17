"""Real Qt windows with an isolated free practice game, never desktop input.

Checks shared pause/chat/confirmation state and captures the actual widgets.
Screenshots are UI QA, not evidence of Factorio physics or model reasoning.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from PySide6.QtCore import QPoint, Qt
from PySide6.QtGui import QFont, QFontDatabase, QTextCursor
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication
from playall.config import Settings
from playall.desktop import MainWindow, STYLE
from playall.storage import atomic_json


def main():
    output = ROOT / "data/qa/focus-window"
    output.mkdir(parents=True, exist_ok=True)
    app = QApplication([])
    if app.platformName() == "offscreen":
        for name in ("msyh.ttc", "msyhbd.ttc", "segoeui.ttf", "segoeuib.ttf"):
            QFontDatabase.addApplicationFont(str(Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts" / name))
    app.setFont(QFont("Microsoft YaHei UI", 10))
    app.setStyle("Fusion")
    app.setStyleSheet(STYLE)
    evidence = {"surface": "native Qt independent window", "mode": "offscreen, isolated practice game",
                "paid_ai_called": False, "os_input_sent": False, "checks": [], "layouts": []}
    with tempfile.TemporaryDirectory(prefix="playall-focus-qa-") as directory:
        settings = Settings(Path(directory))
        settings.update({"provider": "rehearsal", "game": "sandbox", "step_delay": .1})
        main_window = MainWindow(settings, auto_open_game=False)
        viewer = main_window.focus_window

        def wait_for(condition, seconds=15):
            deadline = time.monotonic() + seconds
            while not condition():
                app.processEvents()
                assert time.monotonic() < deadline, main_window.runner.snapshot()
                QTest.qWait(25)
            app.processEvents()

        def capture(name):
            main_window.refresh()
            app.processEvents()
            QTest.qWait(80)
            viewer.grab().save(str(output / (name + ".png")))

        def passed(name):
            evidence["checks"].append(name)
            print("PASSED", name, flush=True)

        try:
            main_window.show()
            QTest.mouseClick(main_window.focus_button, Qt.MouseButton.LeftButton)
            assert viewer.isVisible() and viewer.isWindow() and viewer.width() > main_window.width()
            assert not main_window.runner.snapshot()["active"] and main_window.runner.game is None
            capture("empty")
            passed("opening_the_independent_window_does_not_start_game_or_ai")

            main_window.target_game_id, main_window.target_process = "sandbox", None
            main_window.refresh()
            QTest.mouseClick(main_window.primary, Qt.MouseButton.LeftButton)
            wait_for(lambda: main_window.runner.snapshot()["actions"] >= 1)
            assert viewer.isVisible()
            wait_for(lambda: viewer.play.text() == "暂停")
            capture("running")
            QTest.mouseClick(viewer.play, Qt.MouseButton.LeftButton)
            wait_for(lambda: main_window.runner.snapshot()["status"] == "paused" and not main_window.pending)
            paused = main_window.runner.snapshot()["actions"]
            QTest.qWait(400)
            assert main_window.runner.snapshot()["actions"] == paused
            assert viewer.play.text() == main_window.primary.text()
            passed("start_opens_large_window_and_both_pause_controls_share_one_runner")

            viewer.chat_input.setPlainText("当前有哪些资源？保持暂停。")
            QTest.keyClick(viewer.chat_input, Qt.Key.Key_Return)
            wait_for(lambda: not main_window.chat_pending and not main_window.runner.snapshot().get("chat_busy") and
                     len(main_window.runner.snapshot()["conversation"]) >= 2)
            assert "当前有哪些资源" in viewer.chat.toPlainText() and "当前有哪些资源" in main_window.chat.toPlainText()
            assert not viewer.chat_input.toPlainText() and not main_window.runner.resume_event.is_set()
            viewer.chat_input.setPlainText("第一行")
            QTest.keyClick(viewer.chat_input, Qt.Key.Key_Return, Qt.KeyboardModifier.ShiftModifier)
            assert "\n" in viewer.chat_input.toPlainText()
            viewer.chat_input.clear()
            passed("independent_chat_sends_to_existing_conversation_and_shift_enter_adds_line")

            main_window.runner._ask("要进入另一份存档吗？当前进度会保留，回答前 AI 已暂停。",
                action={"action": "mine", "name": "coal", "count": 1}, review={"ticket": "f" * 32})
            main_window.refresh()
            assert not viewer.play.isEnabled() and not main_window.primary.isEnabled()
            assert viewer.question.isVisible() and viewer.approve.isVisible() and viewer.deny.isEnabled()
            QTest.qWait(450)
            assert main_window.runner.snapshot()["actions"] == paused
            capture("confirmation")
            viewer.resize(860, 620)
            app.processEvents()
            assert viewer.approve.height() >= 30 and viewer.deny.height() >= 30
            assert viewer.approve.mapTo(viewer.question, QPoint(0, viewer.approve.height())).y() <= viewer.question.height()
            assert viewer.send.mapTo(viewer, QPoint(0, viewer.send.height())).y() <= viewer.height()
            capture("narrow-confirmation")
            viewer.resize(1180, 820)
            QTest.mouseClick(viewer.deny, Qt.MouseButton.LeftButton)
            wait_for(lambda: not main_window.chat_pending and not main_window.runner.snapshot().get("pending_question"))
            assert not main_window.runner.resume_event.is_set()
            assert main_window.runner.snapshot()["actions"] == paused
            passed("important_question_disables_both_continue_buttons_and_cancel_keeps_pause")

            now = time.time()
            for i in range(35):
                main_window.runner.emit("thought", f"观察记录 {i+1}：核对现有材料与机器状态，再决定下一步操作。\n计划：材料不足时先采集，建造后检查真实结果。", reasoning_content="PRIVATE-REASONING-MARKER")
            main_window.refresh()
            assert "PRIVATE-REASONING-MARKER" not in viewer.feed.toPlainText()
            selected = viewer.feed.textCursor()
            selected.setPosition(0)
            selected.setPosition(5, QTextCursor.MoveMode.KeepAnchor)
            viewer.feed.setTextCursor(selected)
            original_selection = viewer.feed.textCursor().selectedText()
            viewer.feed.verticalScrollBar().setValue(0)
            main_window.runner.emit("thought", "新增公开摘要：继续检查生产结果。")
            main_window.refresh()
            assert viewer.feed.verticalScrollBar().value() == 0
            assert viewer.feed.textCursor().selectedText() == original_selection
            assert "新增公开摘要" in viewer.feed.toPlainText()
            passed("new_events_preserve_reading_position_and_selection_and_ignore_private_fields")
            viewer.change_size(2)
            assert viewer.reading_size == 18
            viewer.scroll_latest()
            capture("reading")
            passed("reading_font_can_grow_and_latest_button_restores_live_follow")

            for width, height in ((1180, 820), (1440, 920), (860, 620), (860, 900)):
                viewer.resize(width, height)
                app.processEvents()
                bottom = viewer.send.mapTo(viewer, QPoint(0, viewer.send.height())).y()
                assert bottom <= viewer.height(), (width, height, bottom, viewer.size())
                assert viewer.feed.height() >= 140 and viewer.chat.height() >= 120
                evidence["layouts"].append({"requested": [width, height], "actual": [viewer.width(), viewer.height()],
                                           "feed": [viewer.feed.width(), viewer.feed.height()], "chat": [viewer.chat.width(), viewer.chat.height()],
                                           "input_bottom": bottom})
            capture("narrow")
            passed("large_and_narrow_window_layouts_keep_reading_and_input_visible")
            text = viewer.feed.toPlainText()
            game = main_window.runner.game
            viewer.close()
            assert not viewer.isVisible() and main_window.runner.thread.is_alive() and main_window.runner.game is game
            main_window.open_focus()
            assert viewer.isVisible() and viewer.feed.toPlainText() == text
            passed("closing_and_reopening_view_retains_session_and_history")
            main_window.close()
            wait_for(lambda: not main_window.runner.snapshot()["active"], 15)
            app.processEvents()
            main_window.refresh()
            app.processEvents()
            assert not viewer.isVisible()
            passed("closing_main_window_saves_and_closes_independent_view")
            evidence["status"] = "passed"
        finally:
            main_window.runner.stop()
            if main_window.runner.thread:
                main_window.runner.thread.join(10)
            viewer.close()
            main_window.close()
    atomic_json(output / "evidence.json", evidence)
    print(json.dumps(evidence, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
