from __future__ import annotations

import copy
import json
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from playall.llm import ProviderError
from playall.runner import Runner
from tests.support import IsolatedSettingsMixin


def response(action=None, *, progress=None, call_id="test-call", usage=None):
    function = {"name": "report_progress", "arguments": json.dumps({"summary": progress})} if progress is not None else {
        "name": "game_action", "arguments": json.dumps(action or {"action": "mine", "name": "iron-ore", "count": 1}),
    }
    return {"choices": [{"message": {"role": "assistant", "content": "执行测试动作", "tool_calls": [
        {"id": call_id, "type": "function", "function": function},
    ]}}], "usage": usage or {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18}}


class GatedAI:
    """In-process AI with request gates; never makes a network request."""

    def __init__(self, responses=None, *, gates=(1,)):
        self.responses = responses or []
        self.calls = []
        self.entered = {index: threading.Event() for index in gates}
        self.release = {index: threading.Event() for index in gates}

    def chat(self, messages, tools, output_tokens):
        self.calls.append((copy.deepcopy(messages), copy.deepcopy(tools), output_tokens))
        index = len(self.calls)
        if index in self.entered:
            self.entered[index].set()
            if not self.release[index].wait(5):
                raise AssertionError("Test did not release its simulated in-flight AI request")
        selected = self.responses[index - 1] if index <= len(self.responses) else response(call_id=f"call-{index}")
        if isinstance(selected, Exception):
            raise selected
        return copy.deepcopy(selected)

    def release_all(self):
        for event in self.release.values():
            event.set()


class RecordingGame:
    def __init__(self):
        self.executed = []
        self.prepare_calls = self.pause_calls = self.resume_calls = self.cancel_calls = self.close_calls = 0
        self.state = {"ok": True, "game": "sandbox", "inventory": {}, "buildings": [],
                      "iron_plates_produced": 0, "map": "large-map-omitted-from-ai"}

    def prepare(self, control, new_world=False):
        self.prepare_calls += 1
        control.check()

    def observe(self):
        return copy.deepcopy(self.state)

    def execute(self, action, control):
        control.check()
        self.executed.append(copy.deepcopy(action))
        if action["action"] == "mine":
            self.state["inventory"][action["name"]] = self.state["inventory"].get(action["name"], 0) + action.get("count", 1)
        return {"ok": True, "message": "测试动作完成"}

    def pause(self):
        self.pause_calls += 1

    def resume(self):
        self.resume_calls += 1

    def cancel(self):
        self.cancel_calls += 1

    def close(self):
        self.close_calls += 1


class RetriableSaveGame(RecordingGame):
    def __init__(self):
        super().__init__()
        self.process = Mock()
        self.process.poll.return_value = None
        self.allow_save = threading.Event()
        self.close_attempts = 0

    def close(self):
        self.close_attempts += 1
        if not self.allow_save.is_set():
            raise RuntimeError("synthetic game checkpoint failure")
        self.close_calls += 1
        self.process.poll.return_value = 0


class BetweenActionsGame(RecordingGame):
    def __init__(self):
        super().__init__()
        self.first_action_done = threading.Event()
        self.return_first_action = threading.Event()

    def execute(self, action, control):
        result = super().execute(action, control)
        if len(self.executed) == 1:
            self.first_action_done.set()
            if not self.return_first_action.wait(5):
                raise AssertionError("Test did not release its first game action")
        return result


class RunnerTests(IsolatedSettingsMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        # A mistaken selection must not touch an installed game or a paid AI.
        for blocker in (
            patch("playall.games.factorio.Factorio", side_effect=AssertionError("Tests must not launch Factorio")),
            patch("playall.llm.CompatibleAI._request", side_effect=AssertionError("Tests must not call an external AI")),
        ):
            blocker.start()
            self.addCleanup(blocker.stop)
        self.settings.update({"provider": "custom", "game": "sandbox", "step_delay": 0,
                              "output_tokens": 256, "profiles": [{"id": "custom", "base_url": "http://127.0.0.1:1",
                                                                 "input_price": 1, "output_price": 2}]})

    def make_runner(self, ai=None, game=None, *, factory=None, other_ais=()):
        if ai is not None or factory is not None:
            provider = patch("playall.runner.make_ai", side_effect=factory) if factory else patch("playall.runner.make_ai", return_value=ai)
            provider.start()
            self.addCleanup(provider.stop)
        if game is not None:
            adapter = patch("playall.runner.make_game", return_value=game)
            adapter.start()
            self.addCleanup(adapter.stop)
        runner = Runner(self.settings)
        self.addCleanup(self.drain_runner, runner, (ai, *other_ais))
        return runner

    def drain_runner(self, runner, ais):
        if runner.thread and runner.thread.is_alive():
            runner.stop()
            for ai in ais:
                if ai is not None:
                    ai.release_all()
            runner.thread.join(3)
            self.assertFalse(runner.thread.is_alive(), "Test runner leaked a background thread")

    def await_done(self, runner):
        self.assertIsNotNone(runner.thread)
        runner.thread.join(3)
        self.assertFalse(runner.thread.is_alive(), "Runner did not terminate promptly")
        return runner.snapshot()

    def await_condition(self, predicate, message):
        deadline = time.monotonic() + 3
        while not predicate():
            if time.monotonic() >= deadline:
                self.fail(message)
            threading.Event().wait(0.005)

    def stop_and_release(self, runner, *ais):
        runner.stop()
        for ai in ais:
            ai.release_all()
        return self.await_done(runner)

    def test_stop_during_inflight_response_never_executes_late_action_and_counts_usage(self):
        ai, game = GatedAI(), RecordingGame()
        runner = self.make_runner(ai, game)
        runner.start({})
        self.assertTrue(ai.entered[1].wait(2), "Runner never sent its simulated request")
        self.assertTrue(runner.snapshot()["request_in_flight"])
        runner.stop()
        self.assertEqual(runner.snapshot()["status"], "stopping")
        with self.assertRaises(ValueError):
            runner.start({})
        ai.release_all()
        state = self.await_done(runner)
        self.assertEqual(state["status"], "stopped")
        self.assertFalse(state["request_in_flight"])
        self.assertFalse(state["active"])
        self.assertEqual(game.executed, [])
        self.assertEqual(state["actions"], 0)
        self.assertEqual(state["total_tokens"], 18)
        self.assertAlmostEqual(state["cost"], 25 / 1_000_000)
        self.assertEqual((game.cancel_calls, game.close_calls), (1, 1))

    def test_tool_only_response_publishes_summary_without_passing_it_to_game(self):
        first = response({"action": "mine", "name": "iron-ore", "count": 1,
                          "summary": "当前缺少铁矿，先采集一份原料。"})
        message = first["choices"][0]["message"]
        message["content"] = None
        message["reasoning_content"] = "PRIVATE-REASONING-MARKER"
        ai, game = GatedAI([first], gates=(1, 2)), RecordingGame()
        runner = self.make_runner(ai, game)
        runner.start({})
        self.assertTrue(ai.entered[1].wait(2))
        schema = next(t["function"]["parameters"] for t in ai.calls[0][1] if t["function"]["name"] == "game_action")
        self.assertIn("summary", schema["required"])
        ai.release[1].set()
        self.assertTrue(ai.entered[2].wait(2))
        state = self.stop_and_release(runner, ai)
        self.assertEqual(game.executed, [{"action": "mine", "name": "iron-ore", "count": 1}])
        thoughts = [e["message"] for e in state["events"] if e["kind"] == "thought"]
        self.assertEqual(thoughts, ["当前缺少铁矿，先采集一份原料。"])
        self.assertNotIn("PRIVATE-REASONING-MARKER", " ".join(e["message"] for e in state["events"]))

    def test_invalid_public_summary_is_rejected_before_native_dispatch(self):
        first = response({"action": "mine", "name": "iron-ore", "count": 1, "summary": {"bad": "shape"}})
        first["choices"][0]["message"]["content"] = None
        ai, game = GatedAI([first], gates=(1, 2)), RecordingGame()
        runner = self.make_runner(ai, game)
        runner.start({})
        self.assertTrue(ai.entered[1].wait(2))
        ai.release[1].set()
        self.assertTrue(ai.entered[2].wait(2))
        self.stop_and_release(runner, ai)
        self.assertEqual(game.executed, [])

    def test_pause_holds_late_action_then_resumes_same_game_and_context(self):
        ai, game = GatedAI(gates=(1, 2)), RecordingGame()
        runner = self.make_runner(ai, game)
        runner.start({})
        self.assertTrue(ai.entered[1].wait(2))
        run_id = runner.snapshot()["run_id"]
        runner.pause()
        ai.release[1].set()
        self.await_condition(lambda: not runner.snapshot()["request_in_flight"], "AI response did not return")
        self.assertEqual(game.executed, [])
        self.assertEqual(len(ai.calls), 1)
        self.assertEqual(runner.snapshot()["status"], "paused")
        self.assertTrue(runner.snapshot()["active"])
        self.assertIs(runner.game, game)
        runner.resume()
        self.assertTrue(ai.entered[2].wait(2))
        self.assertEqual(len(game.executed), 1)
        self.assertEqual(game.prepare_calls, 1)
        self.assertIs(runner.game, game)
        self.assertEqual(runner.snapshot()["run_id"], run_id)
        self.assertEqual((game.pause_calls, game.resume_calls), (1, 1))
        sent = ai.calls[1][0]
        self.assertTrue(any(message.get("role") == "tool" for message in sent), "Resume discarded the previous tool result")
        state = self.stop_and_release(runner, ai)
        self.assertEqual(state["status"], "stopped")
        self.assertEqual(state["actions"], 1)
        self.assertEqual(state["total_tokens"], 36)

    def test_stop_while_paused_does_not_require_resume_or_apply_queued_action(self):
        ai, game = GatedAI(), RecordingGame()
        runner = self.make_runner(ai, game)
        runner.start({})
        self.assertTrue(ai.entered[1].wait(2))
        runner.pause()
        ai.release_all()
        self.await_condition(lambda: not runner.snapshot()["request_in_flight"], "AI response did not return")
        self.assertEqual(self.stop_and_release(runner, ai)["status"], "stopped")
        self.assertEqual(game.executed, [])
        self.assertEqual(game.resume_calls, 0)

    def test_stop_is_nonblocking_when_a_game_command_holds_the_action_gate(self):
        runner = self.make_runner()
        acquired, release, returned = threading.Event(), threading.Event(), threading.Event()

        def hold_gate():
            with runner.action_lock:
                acquired.set()
                release.wait(3)

        def request_stop():
            try:
                runner.stop()
            finally:
                returned.set()

        holder = threading.Thread(target=hold_gate, daemon=True)
        holder.start()
        self.assertTrue(acquired.wait(1))
        stopper = threading.Thread(target=request_stop, daemon=True)
        stopper.start()
        try:
            self.assertTrue(returned.wait(0.2), "Stop waited for an in-flight game command's action gate")
            self.assertTrue(runner.stop_event.is_set())
        finally:
            release.set()
            holder.join(1)
            stopper.join(1)
        self.assertFalse(holder.is_alive())
        self.assertFalse(stopper.is_alive())

    def test_failed_shutdown_save_keeps_live_game_and_can_be_retried(self):
        ai, game = GatedAI(), RetriableSaveGame()
        runner = self.make_runner(ai, game)
        # Always release the synthetic storage failure before normal test cleanup.
        self.addCleanup(game.allow_save.set)
        runner.start({})
        self.assertTrue(ai.entered[1].wait(2))
        runner.stop()
        ai.release_all()
        self.await_condition(lambda: runner.snapshot().get("save_error") is True, "Save failure was not exposed as retriable")
        state = runner.snapshot()
        self.assertEqual(state["status"], "paused")
        self.assertTrue(state["active"])
        self.assertIs(runner.game, game)
        self.assertIsNone(game.process.poll())
        self.assertEqual(game.close_attempts, 1)
        self.assertEqual(game.close_calls, 0)
        self.assertEqual(len(ai.calls), 1)
        game.allow_save.set()
        runner.stop()  # The desktop's Retry Save action retries the same live game.
        state = self.await_done(runner)
        self.assertEqual(state["status"], "stopped", state["message"])
        self.assertFalse(state["save_error"])
        self.assertFalse(state["active"])
        self.assertEqual(game.close_attempts, 2)
        self.assertEqual(game.close_calls, 1)
        self.assertEqual(len(ai.calls), 1, "Retrying storage must not send another AI request")
        self.assertIsInstance(json.loads((self.directory / "sessions" / "sandbox.json").read_text(encoding="utf-8")), dict)

    def test_event_log_disk_failure_keeps_memory_events_and_does_not_skip_cleanup(self):
        ai, game = GatedAI(), RecordingGame()
        runner = self.make_runner(ai, game)
        original_open = Path.open

        def fail_event_log(path, *args, **kwargs):
            if path.suffix == ".jsonl":
                raise OSError("synthetic event-log disk failure")
            return original_open(path, *args, **kwargs)

        with patch.object(Path, "open", new=fail_event_log):
            runner.start({})
            self.assertTrue(ai.entered[1].wait(2), "A log failure prevented the agent from starting")
            runner.emit("notice", "memory-event-survives-disk-failure")
            self.assertTrue(any(event["message"] == "memory-event-survives-disk-failure" for event in runner.snapshot()["events"]))
            state = self.stop_and_release(runner, ai)
        self.assertEqual(state["status"], "stopped", state["message"])
        self.assertFalse(state["active"])
        self.assertEqual((game.cancel_calls, game.close_calls), (1, 1))
        record = self.directory / "runs" / f"{state['run_id']}.json"
        self.assertEqual(json.loads(record.read_text(encoding="utf-8"))["status"], "stopped")

    def test_milestones_do_not_finish_and_high_usage_does_not_limit_play(self):
        huge_usage = {"prompt_tokens": 2_000_000, "completion_tokens": 1_000_000, "total_tokens": 3_000_000}
        ai = GatedAI([
            response(call_id="mine-first", usage=huge_usage),
            response(progress="采矿阶段完成，继续扩建", call_id="milestone", usage=huge_usage),
            response({"action": "mine", "name": "coal", "count": 2}, call_id="mine-next", usage=huge_usage),
        ], gates=(4,))
        game = RecordingGame()
        runner = self.make_runner(ai, game)
        runner.start({})
        self.assertTrue(ai.entered[4].wait(3), "A milestone or display-only usage counter stopped the autonomous loop")
        state = runner.snapshot()
        self.assertTrue(state["active"])
        self.assertEqual(state["status"], "running")
        self.assertEqual(state["actions"], 2)
        self.assertEqual(state["total_tokens"], 9_000_000)
        self.assertAlmostEqual(state["cost"], 12)
        self.assertEqual([action["name"] for action in game.executed], ["iron-ore", "coal"])
        sent_observation = json.loads(ai.calls[0][0][-1]["content"])["observation"]
        self.assertNotIn("map", sent_observation)
        state = self.stop_and_release(runner, ai)
        self.assertEqual(state["status"], "stopped")
        self.assertNotIn("limits", state)

    def test_usage_total_is_derived_when_provider_omits_total_tokens(self):
        ai = GatedAI([response(usage={"prompt_tokens": 11, "completion_tokens": 7})], gates=(2,))
        game = RecordingGame()
        runner = self.make_runner(ai, game)
        runner.start({})
        self.assertTrue(ai.entered[2].wait(2))
        state = runner.snapshot()
        self.assertEqual(state["prompt_tokens"], 11)
        self.assertEqual(state["completion_tokens"], 7)
        self.assertEqual(state["total_tokens"], 18)
        self.assertAlmostEqual(state["cost"], 25 / 1_000_000)
        self.stop_and_release(runner, ai)

    def test_pause_checkpoint_preserves_context_and_restores_after_app_restart(self):
        ai, game = GatedAI([response(call_id="remember-this-action")], gates=(2,)), RecordingGame()
        runner = self.make_runner(ai, game)
        runner.start({})
        self.assertTrue(ai.entered[2].wait(2))
        runner.pause()
        path = self.directory / "sessions" / "sandbox.json"
        self.await_condition(path.is_file, "Pause did not save a session checkpoint")
        checkpoint = json.loads(path.read_text(encoding="utf-8"))
        for key in ("history", "journal", "counters", "system_prompt"):
            self.assertIn(key, checkpoint)
        self.assertIn("remember-this-action", json.dumps(checkpoint["history"]))
        self.stop_and_release(runner, ai)

        restored_ai, restored_game = GatedAI(), RecordingGame()
        restored = self.make_runner(restored_ai, restored_game)
        restored.start({"new_world": False})
        self.assertTrue(restored_ai.entered[1].wait(2))
        restored_messages = restored_ai.calls[0][0]
        self.assertIn("remember-this-action", json.dumps(restored_messages))
        self.assertTrue(any(message.get("role") == "tool" for message in restored_messages))
        self.assertGreaterEqual(restored.snapshot()["total_tokens"], 18)
        self.stop_and_release(restored, restored_ai)

    def test_failed_atomic_checkpoint_replace_keeps_last_good_session(self):
        ai, game = GatedAI([response(call_id="persist-before-failure")], gates=(2,)), RecordingGame()
        runner = self.make_runner(ai, game)
        runner.start({})
        self.assertTrue(ai.entered[2].wait(2))
        path = self.directory / "sessions" / "sandbox.json"
        self.await_condition(path.is_file, "A full AI turn did not save its checkpoint")
        before = path.read_bytes()
        with patch("os.replace", side_effect=OSError("synthetic checkpoint replace failure")):
            try:
                runner.pause()
            except OSError:
                pass  # Reporting the storage error to the caller is acceptable.
        self.assertEqual(path.read_bytes(), before, "A failed save destroyed the last good session")
        self.assertIn("persist-before-failure", json.dumps(json.loads(path.read_bytes())["history"]))
        self.stop_and_release(runner, ai)

    def test_resume_can_switch_ai_and_prompt_without_recreating_game_or_history(self):
        self.settings.update({"profiles": [{"id": "deepseek", "base_url": "http://127.0.0.1:1",
                                            "input_price": 3, "output_price": 4}]})
        first_ai = GatedAI([response(call_id="remember-before-provider-switch")], gates=(2,))
        second_ai, game = GatedAI(), RecordingGame()
        clients = {"custom": first_ai, "deepseek": second_ai}
        runner = self.make_runner(first_ai, game, factory=lambda profile: clients[profile["id"]], other_ais=(second_ai,))
        runner.start({})
        self.assertTrue(first_ai.entered[2].wait(2))
        runner.pause()
        first_ai.release_all()
        self.await_condition(lambda: not runner.snapshot()["request_in_flight"], "First AI response did not return")
        new_prompt = "先建设发电，再研究科技，并持续扩大工厂。"
        runner.resume({"provider": "deepseek", "system_prompt": new_prompt})
        self.assertTrue(second_ai.entered[1].wait(2), "Resume did not apply the selected AI")
        self.assertIs(runner.game, game)
        self.assertEqual(game.prepare_calls, 1)
        self.assertEqual(len(game.executed), 1)
        messages = second_ai.calls[0][0]
        self.assertIn(new_prompt, json.dumps(messages, ensure_ascii=False))
        self.assertTrue(any(message.get("role") == "tool" for message in messages), "Switching providers discarded context")
        self.assertIn("remember-before-provider-switch", json.dumps(messages))
        self.assertEqual(runner.snapshot()["provider"], "deepseek")
        state = self.stop_and_release(runner, first_ai, second_ai)
        self.assertEqual(state["total_tokens"], 54)
        self.assertAlmostEqual(state["cost"], 111 / 1_000_000,
                               msg="Each response must retain the price of the provider that handled it")

    def test_switching_ai_between_tool_calls_discards_all_remaining_old_actions_and_milestones(self):
        self.settings.update({"profiles": [{"id": "deepseek", "base_url": "http://127.0.0.1:1"}]})
        old_response = response(call_id="old-first-action")
        old_response["choices"][0]["message"]["tool_calls"].extend([
            response({"action": "mine", "name": "coal", "count": 2}, call_id="old-remaining-action")["choices"][0]["message"]["tool_calls"][0],
            response(progress="old-response-milestone-must-not-be-remembered", call_id="old-remaining-milestone")["choices"][0]["message"]["tool_calls"][0],
        ])
        first_ai, second_ai, game = GatedAI([old_response], gates=()), GatedAI(), BetweenActionsGame()
        clients = {"custom": first_ai, "deepseek": second_ai}
        runner = self.make_runner(first_ai, game, factory=lambda profile: clients[profile["id"]], other_ais=(second_ai,))
        self.addCleanup(game.return_first_action.set)
        runner.start({})
        self.assertTrue(game.first_action_done.wait(2))
        runner.pause()
        runner.resume({"provider": "deepseek", "system_prompt": "新 AI 重新观察并制定下一步。"})
        game.return_first_action.set()
        self.assertTrue(second_ai.entered[1].wait(2), "New AI never received control after the old tool group")
        self.assertEqual([action["name"] for action in game.executed], ["iron-ore"])
        self.assertNotIn("old-response-milestone-must-not-be-remembered", runner.journal)
        self.assertEqual(runner.snapshot()["actions"], 1)
        self.stop_and_release(runner, first_ai, second_ai)

    def test_old_provider_late_error_does_not_pause_a_newly_selected_provider(self):
        self.settings.update({"profiles": [{"id": "deepseek", "base_url": "http://127.0.0.1:1"}]})
        first_ai = GatedAI([ProviderError("old-provider-late-network-error")])
        second_ai, game = GatedAI(), RecordingGame()
        clients = {"custom": first_ai, "deepseek": second_ai}
        runner = self.make_runner(first_ai, game, factory=lambda profile: clients[profile["id"]], other_ais=(second_ai,))
        runner.start({})
        self.assertTrue(first_ai.entered[1].wait(2))
        runner.pause()
        runner.resume({"provider": "deepseek"})
        first_ai.release_all()
        self.assertTrue(second_ai.entered[1].wait(2), "An obsolete provider error paused the newly selected provider")
        state = runner.snapshot()
        self.assertEqual(state["status"], "running")
        self.assertEqual(state["provider"], "deepseek")
        self.assertEqual(game.pause_calls, 1, "Only the explicit user pause should reach the game")
        self.assertIs(runner.game, game)
        self.assertEqual(game.close_calls, 0)
        self.stop_and_release(runner, first_ai, second_ai)

    def test_network_error_auto_pauses_and_resume_preserves_game_and_context(self):
        ai = GatedAI([response(call_id="action-before-network-error"), ProviderError("本机测试模拟网络中断")], gates=(3,))
        game = RecordingGame()
        runner = self.make_runner(ai, game)
        runner.start({})
        self.await_condition(lambda: runner.snapshot()["status"] == "paused", "A provider error did not safely pause")
        self.assertTrue(runner.snapshot()["active"])
        self.assertIs(runner.game, game)
        self.assertEqual(game.close_calls, 0)
        self.assertEqual(runner.snapshot()["actions"], 1)
        self.assertEqual(runner.snapshot()["total_tokens"], 18)
        runner.resume()
        self.assertTrue(ai.entered[3].wait(2))
        self.assertEqual(game.prepare_calls, 1)
        self.assertIn("action-before-network-error", json.dumps(ai.calls[2][0]))
        self.assertEqual(self.stop_and_release(runner, ai)["status"], "stopped")

    def test_invalid_ai_choice_auto_pauses_instead_of_closing_the_game(self):
        invalid = {"choices": [None], "usage": {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18}}
        ai = GatedAI([response(call_id="valid-action-before-bad-choice"), invalid], gates=(3,))
        game = RecordingGame()
        runner = self.make_runner(ai, game)
        runner.start({})
        self.await_condition(lambda: runner.snapshot()["status"] in ("paused", "error"), "Invalid AI response did not stop new requests")
        self.assertEqual(runner.snapshot()["status"], "paused", runner.snapshot()["message"])
        self.assertTrue(runner.snapshot()["active"])
        self.assertEqual(game.close_calls, 0)
        runner.resume()
        self.assertTrue(ai.entered[3].wait(2))
        self.assertIn("valid-action-before-bad-choice", json.dumps(ai.calls[2][0]))
        self.stop_and_release(runner, ai)

    def test_corrupt_session_and_history_do_not_prevent_safe_start_or_history_listing(self):
        session_path = self.directory / "sessions" / "sandbox.json"
        session_path.parent.mkdir(parents=True)
        session_path.write_text("{corrupted-checkpoint", encoding="utf-8")
        runs = self.directory / "runs"
        runs.mkdir()
        (runs / "z-broken.json").write_text("{incomplete", encoding="utf-8")
        (runs / "y-wrong-type.json").write_text("[]", encoding="utf-8")
        (runs / "x-invalid-utf8.json").write_bytes(b"\xff\xfe")
        (runs / "a-valid.json").write_text(json.dumps({"run_id": "valid-history", "status": "stopped"}), encoding="utf-8")
        ai, game = GatedAI(), RecordingGame()
        runner = self.make_runner(ai, game)
        self.assertIn("valid-history", [entry["run_id"] for entry in runner.history()])
        runner.start({"new_world": False})
        self.assertTrue(ai.entered[1].wait(2), "A damaged session prevented a fresh safe run")
        state = self.stop_and_release(runner, ai)
        self.assertEqual(state["status"], "stopped")
        self.assertIsInstance(json.loads(session_path.read_text(encoding="utf-8")), dict)

    def test_semantically_corrupt_orphan_tool_message_is_not_sent_to_ai_on_restore(self):
        path = self.directory / "sessions" / "sandbox.json"
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({"version": 1, "game": "sandbox", "system_prompt": "恢复测试",
                                    "history": [[{"role": "tool", "tool_call_id": "missing-assistant-call", "content": "orphan-tool-result"}]],
                                    "journal": [], "counters": {}}), encoding="utf-8")
        ai, game = GatedAI(), RecordingGame()
        runner = self.make_runner(ai, game)
        runner.start({"new_world": False})
        self.assertTrue(ai.entered[1].wait(2))
        self.assertNotIn("orphan-tool-result", json.dumps(ai.calls[0][0]),
                         "An orphan tool result causes compatible AI APIs to reject every resumed request")
        self.stop_and_release(runner, ai)

    def test_offline_practice_produces_new_plates_and_keeps_running_until_paused(self):
        self.settings.update({"provider": "rehearsal"})
        runner = self.make_runner()
        runner.start({"new_world": True})
        self.await_condition(lambda: (runner.snapshot().get("observation") or {}).get("inventory", {}).get("iron-plate", 0) >= 18,
                             "Offline practice did not produce and collect ten new plates")
        runner.pause()
        state = runner.snapshot()
        self.assertEqual(state["status"], "paused")
        self.assertTrue(state["active"])
        self.assertGreaterEqual(state["observation"]["iron_plates_produced"], 10)
        self.assertGreaterEqual(state["observation"]["inventory"]["iron-plate"], 18)
        self.assertEqual(state["observation"]["game"], "sandbox")
        self.assertIn("非 Factorio", state["observation"]["source"])
        self.assertEqual(state["total_tokens"], 0)
        self.assertEqual(state["cost"], 0)
        self.assertEqual(self.stop_and_release(runner)["status"], "stopped")
        saved = json.loads((self.directory / "sandbox.json").read_text(encoding="utf-8"))
        self.assertGreaterEqual(saved["iron_plates_produced"], 10)

    def test_declared_local_ipv6_endpoint_does_not_require_a_key(self):
        self.settings.update({"profiles": [{"id": "custom", "base_url": "http://[::1]:1234"}]})
        ai, game = GatedAI(), RecordingGame()
        runner = self.make_runner(ai, game)
        runner.start({})
        self.assertTrue(ai.entered[1].wait(2))
        self.assertEqual(self.stop_and_release(runner, ai)["status"], "stopped")


if __name__ == "__main__":
    unittest.main()
