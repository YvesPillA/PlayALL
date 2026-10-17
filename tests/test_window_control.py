from __future__ import annotations

import copy
import json
import threading
import time
import unittest
from unittest.mock import patch

from playall.games.registry import GameSpec
from playall.runner import Runner
from tests.support import IsolatedSettingsMixin
from tests.test_runner import GatedAI, RecordingGame, response


class WindowGame(RecordingGame):
    """A windowed adapter stub with no native window or external process."""

    def __init__(self):
        super().__init__()
        self.state["game"] = "window-test"
        self.state["position"] = {"x": 0, "y": 0}
        self.lifecycle = []
        self.ready = False
        self.connect_calls = self.checkpoint_calls = 0
        self.connect_entered = threading.Event()
        self.connect_release = threading.Event()
        self.block_initial_connect = False
        self.connect_error = None
        self.pause_result = self.resume_result = None
        self.disconnect_after_action = False
        self.block_first_action = False
        self.first_action_done = threading.Event()
        self.return_first_action = threading.Event()

    def prepare(self, control, new_world=False):
        super().prepare(control, new_world=new_world)
        self.lifecycle.append("prepared")

    def connect_window(self, control):
        self.connect_calls += 1
        self.connect_entered.set()
        # On Resume this must be allowed while resume_event is still clear.
        control.check()
        if self.block_initial_connect and self.connect_calls == 1:
            while not self.connect_release.wait(0.01):
                control.check()
        if self.connect_error is not None:
            raise self.connect_error
        self.ready = True
        self.lifecycle.append("window-connected")
        return {"ok": True, "message": "test character is in the game"}

    def window_ready(self):
        return self.ready

    def execute(self, action, control):
        result = super().execute(action, control)
        if self.block_first_action and len(self.executed) == 1:
            self.first_action_done.set()
            if not self.return_first_action.wait(5):
                raise AssertionError("Test did not return its first simulated game action")
        if self.disconnect_after_action:
            self.ready = False
        return result

    def pause(self):
        super().pause()
        return copy.deepcopy(self.pause_result)

    def resume(self):
        super().resume()
        return copy.deepcopy(self.resume_result)

    def checkpoint(self):
        self.checkpoint_calls += 1
        self.lifecycle.append("checkpoint")

    def close(self):
        super().close()
        self.lifecycle.append("closed")


class WindowRunnerTests(IsolatedSettingsMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        spec = GameSpec("window-test", "窗口测试游戏", f"{__name__}:WindowGame", "Test window adapter rules", has_game_window=True)
        registry = patch.dict("playall.games.registry._games", {"window-test": spec}, clear=False)
        registry.start()
        self.addCleanup(registry.stop)
        for blocker in (
            patch("playall.games.factorio.Factorio", side_effect=AssertionError("Tests must not launch Factorio")),
            patch("playall.llm.CompatibleAI._request", side_effect=AssertionError("Tests must not call an external AI")),
        ):
            blocker.start()
            self.addCleanup(blocker.stop)
        self.settings.update({"provider": "custom", "game": "window-test", "step_delay": 0, "output_tokens": 256,
                              "profiles": [{"id": "custom", "base_url": "http://127.0.0.1:1"}]})

    def make_runner(self, ai, game):
        provider = patch("playall.runner.make_ai", return_value=ai)
        adapter = patch("playall.runner.make_game", return_value=game)
        for stub in (provider, adapter):
            stub.start()
            self.addCleanup(stub.stop)
        runner = Runner(self.settings, connect_game_window=True)
        self.addCleanup(self.drain, runner, ai, game)
        return runner

    def drain(self, runner, ai, game):
        if runner.thread and runner.thread.is_alive():
            runner.stop()
            game.connect_release.set()
            game.return_first_action.set()
            ai.release_all()
            runner.thread.join(3)
            self.assertFalse(runner.thread.is_alive(), "Window test leaked a runner thread")

    def await_condition(self, predicate, message):
        deadline = time.monotonic() + 3
        while not predicate():
            if time.monotonic() >= deadline:
                self.fail(message)
            threading.Event().wait(0.005)

    def stop_and_release(self, runner, ai, game):
        runner.stop()
        game.connect_release.set()
        game.return_first_action.set()
        ai.release_all()
        runner.thread.join(3)
        self.assertFalse(runner.thread.is_alive(), "Window test did not finish saving")
        return runner.snapshot()

    def resume_without_deadlock(self, runner, game):
        returned = threading.Event()
        errors = []

        def resume():
            try:
                runner.resume()
            except BaseException as error:
                errors.append(error)
            finally:
                returned.set()

        thread = threading.Thread(target=resume, daemon=True, name="Test reconnect on resume")
        thread.start()
        if not returned.wait(1):
            # Release a wrongly pause-sensitive connector so cleanup can finish.
            runner.stop()
            runner.resume_event.set()
            game.connect_release.set()
            thread.join(1)
            self.fail("Resume deadlocked while checking window connection with resume_event cleared")
        thread.join(1)
        self.assertEqual(errors, [], f"Resume failed: {errors}")

    def observation_sent(self, ai, call_index):
        return json.loads(ai.calls[call_index][0][-1]["content"])["observation"]

    def test_first_ai_request_waits_until_window_connection_is_confirmed(self):
        ai, game = GatedAI(), WindowGame()
        game.block_initial_connect = True
        runner = self.make_runner(ai, game)
        runner.start()
        self.assertTrue(game.connect_entered.wait(2), "The game window was never connected")
        self.assertEqual(game.prepare_calls, 1)
        self.assertEqual(ai.calls, [], "AI was asked to act before the character entered the game")
        self.assertEqual(runner.snapshot()["turns"], 0)
        self.assertFalse(game.ready)
        game.connect_release.set()
        self.assertTrue(ai.entered[1].wait(2))
        self.assertEqual(game.lifecycle[:2], ["prepared", "window-connected"])
        self.assertTrue(game.ready)
        self.assertEqual(self.stop_and_release(runner, ai, game)["status"], "stopped")

    def test_initial_window_connection_failure_saves_and_closes_without_requesting_ai(self):
        ai, game = GatedAI(), WindowGame()
        game.connect_error = RuntimeError("synthetic initial window connection failure")
        runner = self.make_runner(ai, game)
        runner.start()
        runner.thread.join(3)
        self.assertFalse(runner.thread.is_alive(), "A failed connection left the game running")
        state = runner.snapshot()
        self.assertEqual(state["status"], "error")
        self.assertIn("synthetic initial window connection failure", state["message"])
        self.assertEqual(ai.calls, [])
        self.assertEqual((game.cancel_calls, game.close_calls), (1, 1))
        self.assertGreaterEqual(game.checkpoint_calls, 1)
        self.assertLess(game.lifecycle.index("checkpoint"), game.lifecycle.index("closed"))
        session = json.loads((self.directory / "sessions" / "window-test.json").read_text(encoding="utf-8"))
        self.assertEqual(session["history"], [])
        self.assertEqual(session["counters"]["turns"], 0)

    def test_window_disconnect_between_turns_pauses_before_another_ai_request(self):
        ai, game = GatedAI(gates=(2,)), WindowGame()
        game.disconnect_after_action = True
        runner = self.make_runner(ai, game)
        runner.start()
        self.await_condition(lambda: runner.snapshot()["status"] == "paused" or ai.entered[2].is_set(), "Window disconnect was not handled")
        self.assertEqual(runner.snapshot()["status"], "paused", "A disconnected window allowed a new AI request")
        self.assertEqual(len(ai.calls), 1)
        self.assertEqual(len(game.executed), 1)
        self.assertEqual(game.close_calls, 0)
        self.assertTrue(runner.snapshot()["active"])
        self.stop_and_release(runner, ai, game)

    def test_window_disconnect_during_ai_request_pauses_before_returned_tools_execute(self):
        ai, game = GatedAI(), WindowGame()
        runner = self.make_runner(ai, game)
        runner.start()
        self.assertTrue(ai.entered[1].wait(2))
        game.ready = False
        ai.release_all()
        self.await_condition(lambda: runner.snapshot()["status"] in ("paused", "error") or bool(game.executed), "Returned AI response did not check its window")
        self.assertEqual(runner.snapshot()["status"], "paused")
        self.assertEqual(game.executed, [])
        self.assertEqual(len(ai.calls), 1)
        self.assertEqual(game.close_calls, 0)
        self.assertTrue(runner.snapshot()["active"])
        self.stop_and_release(runner, ai, game)

    def test_manual_takeover_discards_pending_response_preserves_history_and_reobserves_human_changes(self):
        ai = GatedAI([response(call_id="completed-before-manual-control"),
                      response({"action": "mine", "name": "coal", "count": 5}, call_id="obsolete-pending-action")], gates=(2, 3))
        game = WindowGame()
        game.pause_result = {"replan": True, "manual_control": True}
        game.resume_result = {"replan": True}
        runner = self.make_runner(ai, game)
        runner.start()
        self.assertTrue(ai.entered[2].wait(2))
        revision = runner.revision
        runner.pause()
        self.assertGreater(runner.revision, revision)
        state = runner.snapshot()
        self.assertTrue("接手" in state["message"] or "手动" in state["message"], state["message"])
        game.state["position"] = {"x": 99, "y": -12}
        game.state["inventory"]["iron-ore"] = 41
        game.state["manual_marker"] = "changed-by-human"
        ai.release[2].set()
        self.await_condition(lambda: not runner.snapshot()["request_in_flight"], "Old response did not return while paused")
        self.assertEqual([action["name"] for action in game.executed], ["iron-ore"])
        self.resume_without_deadlock(runner, game)
        self.assertTrue(ai.entered[3].wait(2), "AI did not replan after manual takeover")
        self.assertEqual(game.connect_calls, 2)
        self.assertEqual(game.prepare_calls, 1)
        self.assertIs(runner.game, game)
        observation = self.observation_sent(ai, 2)
        self.assertEqual(observation["manual_marker"], "changed-by-human")
        self.assertEqual(observation["position"], {"x": 99, "y": -12})
        self.assertEqual(observation["inventory"]["iron-ore"], 41)
        self.assertIn("completed-before-manual-control", json.dumps(ai.calls[2][0]))
        self.assertEqual([action["name"] for action in game.executed], ["iron-ore"])
        self.stop_and_release(runner, ai, game)

    def test_resume_replan_flag_independently_cancels_pending_action_and_refreshes_observation(self):
        ai, game = GatedAI(gates=(1, 2)), WindowGame()
        game.resume_result = {"replan": True}
        runner = self.make_runner(ai, game)
        runner.start()
        self.assertTrue(ai.entered[1].wait(2))
        runner.pause()  # No manual/replan flag on Pause: old adapters keep this behavior.
        game.state["manual_marker"] = "state-changed-before-resume"
        ai.release[1].set()
        self.await_condition(lambda: not runner.snapshot()["request_in_flight"], "Pending response did not return")
        revision = runner.revision
        self.resume_without_deadlock(runner, game)
        self.assertGreater(runner.revision, revision, "The adapter's Resume replan request was ignored")
        self.assertTrue(ai.entered[2].wait(2))
        self.assertEqual(game.executed, [])
        self.assertEqual(self.observation_sent(ai, 1)["manual_marker"], "state-changed-before-resume")
        self.assertEqual(game.connect_calls, 2)
        self.stop_and_release(runner, ai, game)

    def test_manual_takeover_cancels_remaining_tools_and_milestones_in_same_response(self):
        old = response(call_id="first-leg-before-takeover")
        old["choices"][0]["message"]["tool_calls"].extend([
            response({"action": "mine", "name": "coal", "count": 3}, call_id="remaining-old-action")["choices"][0]["message"]["tool_calls"][0],
            response(progress="obsolete-manual-takeover-milestone", call_id="remaining-old-progress")["choices"][0]["message"]["tool_calls"][0],
        ])
        ai, game = GatedAI([old], gates=(2,)), WindowGame()
        game.block_first_action = True
        game.pause_result = {"replan": True, "manual_control": True}
        game.resume_result = {"replan": True}
        runner = self.make_runner(ai, game)
        runner.start()
        self.assertTrue(game.first_action_done.wait(2))
        runner.pause()
        game.state["manual_marker"] = "human-changed-world-between-tools"
        game.return_first_action.set()
        self.resume_without_deadlock(runner, game)
        self.assertTrue(ai.entered[2].wait(2))
        self.assertEqual([action["name"] for action in game.executed], ["iron-ore"])
        self.assertNotIn("obsolete-manual-takeover-milestone", runner.journal)
        self.assertEqual(self.observation_sent(ai, 1)["manual_marker"], "human-changed-world-between-tools")
        self.stop_and_release(runner, ai, game)


if __name__ == "__main__":
    unittest.main()
