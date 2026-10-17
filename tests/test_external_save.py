"""Offline external-game save failures must preserve history and release leases."""
from __future__ import annotations

import json
from pathlib import Path
import threading
import time
import unittest
from unittest.mock import patch

from playall.runner import Runner, valid_history_group
from playall.storage import atomic_json
from tests.support import IsolatedSettingsMixin
from tests.test_runner import GatedAI, RecordingGame, response


class ExternalSaveGame(RecordingGame):
    """A user-owned game with a transport worker, without a game or network."""

    def __init__(self, *, checkpoint_error=None):
        super().__init__()
        self.process = None
        self.checkpoint_error = checkpoint_error
        self.checkpoint_calls = self.detach_calls = 0
        self.worker_stopped = threading.Event()
        self.worker = None

    def prepare(self, control, new_world=False):
        super().prepare(control, new_world=new_world)
        self.worker = threading.Thread(target=self.worker_stopped.wait,
                                       name="test-external-game-transport", daemon=True)
        self.worker.start()

    def checkpoint(self):
        self.checkpoint_calls += 1
        if self.checkpoint_error is not None:
            raise self.checkpoint_error
        return {"ok": True, "saved": True}

    def checkpoint_due(self):
        # These tests deliberately exercise a due checkpoint and its failures.
        return True

    def close(self):
        super().close()
        self.detach_calls += 1
        self.release_fixture_worker()

    def release_fixture_worker(self):
        self.worker_stopped.set()
        if self.worker is not None:
            self.worker.join(1)


class ExternalSaveTests(IsolatedSettingsMixin, unittest.TestCase):
    def setUp(self):
        detection = patch("playall.config.detect_aistalt", return_value="")
        detection.start()
        self.addCleanup(detection.stop)
        super().setUp()
        for blocker in (
            patch("subprocess.Popen", side_effect=AssertionError("Tests must not launch a game")),
            patch("playall.llm.CompatibleAI._request", side_effect=AssertionError("Tests must not call an external AI")),
        ):
            blocker.start()
            self.addCleanup(blocker.stop)
        self.settings.update({"provider": "custom", "game": "sandbox", "step_delay": 0,
                              "output_tokens": 256,
                              "profiles": [{"id": "custom", "base_url": "http://127.0.0.1:1"}]})

    def make_runner(self, ai, game):
        for stub in (patch("playall.runner.make_ai", return_value=ai),
                     patch("playall.runner.make_game", return_value=game)):
            stub.start()
            self.addCleanup(stub.stop)
        runner = Runner(self.settings)
        self.addCleanup(self.drain_runner, runner, ai, game)
        return runner

    def drain_runner(self, runner, ai, game):
        try:
            if runner.thread and runner.thread.is_alive():
                runner.stop()
                ai.release_all()
                runner.thread.join(3)
                self.assertFalse(runner.thread.is_alive(), "External-save test leaked a runner thread")
        finally:
            # A regression must fail the assertion without leaking its fixture.
            game.release_fixture_worker()

    def await_condition(self, predicate, message):
        deadline = time.monotonic() + 3
        while not predicate():
            if time.monotonic() >= deadline:
                self.fail(message)
            threading.Event().wait(0.005)

    def stop_and_assert_detached(self, runner, ai, game):
        runner.stop()
        ai.release_all()
        runner.thread.join(3)
        self.assertFalse(runner.thread.is_alive(), "Save failure prevented the runner from stopping")
        self.assertEqual(game.close_calls, 1, "External game cleanup was skipped or repeated")
        self.assertEqual(game.detach_calls, 1, "External game's transport lease was not released")
        self.assertIsNotNone(game.worker)
        self.assertFalse(game.worker.is_alive(), "External game's transport worker was left running")
        self.assertFalse(runner.snapshot()["active"])

    def test_native_checkpoint_failure_preserves_completed_history_and_stop_detaches(self):
        ai = GatedAI([response(call_id="completed-before-native-save-failure")], gates=(2,))
        game = ExternalSaveGame(checkpoint_error=RuntimeError("synthetic native checkpoint failure"))
        runner = self.make_runner(ai, game)
        runner.start()
        path = self.directory / "sessions/sandbox.json"
        self.await_condition(lambda: runner.snapshot()["status"] == "paused" and path.is_file(),
                             "Native save failure did not pause with the AI history persisted")
        session = json.loads(path.read_text(encoding="utf-8"))
        self.assertTrue(session["history"])
        self.assertTrue(all(valid_history_group(group) for group in session["history"]))
        self.assertEqual(session["history"][-1][0]["tool_calls"][0]["id"],
                         "completed-before-native-save-failure")
        self.assertEqual(session["history"][-1][1]["tool_call_id"],
                         "completed-before-native-save-failure")
        self.assertTrue(json.loads(session["history"][-1][1]["content"])["ok"])
        self.assertEqual(session["counters"]["actions"], 1)
        self.assertTrue(game.worker.is_alive())
        self.stop_and_assert_detached(runner, ai, game)
        self.assertGreaterEqual(game.checkpoint_calls, 2)
        self.assertEqual(len(ai.calls), 1, "Saving or stopping sent another AI request")
        state = runner.snapshot()
        self.assertEqual(state["status"], "error", "An unconfirmed game save was reported as successful")
        self.assertIn("synthetic native checkpoint failure", state["message"])
        self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["history"], session["history"])

    def test_session_write_failure_on_stop_keeps_last_good_history_and_detaches(self):
        ai = GatedAI([response(call_id="persisted-before-session-disk-failure")], gates=(2,))
        game = ExternalSaveGame()
        runner = self.make_runner(ai, game)
        runner.start()
        self.assertTrue(ai.entered[2].wait(3), "The completed turn was not followed by the gated AI request")
        path = self.directory / "sessions/sandbox.json"
        before = path.read_bytes()
        self.assertIn("persisted-before-session-disk-failure", json.loads(before)["history"][0][0]["tool_calls"][0]["id"])

        def fail_session_write(target, value):
            if Path(target) == path:
                raise OSError("synthetic session storage failure")
            return atomic_json(target, value)

        with patch("playall.runner.atomic_json", side_effect=fail_session_write):
            self.stop_and_assert_detached(runner, ai, game)
        self.assertEqual(path.read_bytes(), before, "Failed shutdown writing destroyed the last good AI history")
        self.assertGreaterEqual(game.checkpoint_calls, 2)
        self.assertEqual(len(game.executed), 1, "The late AI response executed after Stop")
        state = runner.snapshot()
        self.assertEqual(state["status"], "error")
        self.assertIn("synthetic session storage failure", state["message"])


if __name__ == "__main__":
    unittest.main()
