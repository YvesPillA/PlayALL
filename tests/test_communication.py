"""Human input and confirmations must stop real dispatch, not just change prompts."""
import copy
import json
import threading
import time
import unittest
from unittest.mock import patch

from playall.runner import Runner, valid_history_group
from tests.support import IsolatedSettingsMixin
from tests.test_runner import GatedAI, RecordingGame, response


def tool_call(name, args, identity):
    return {"id": identity, "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}


def batch(*calls):
    return {"choices": [{"message": {"role": "assistant", "content": "测试决定", "tool_calls": list(calls)}}]}


def text_reply(text="收到你的消息。", *calls):
    return {"choices": [{"message": {"role": "assistant", "content": text, "tool_calls": list(calls)}}]}


class ConfirmationGame(RecordingGame):
    def __init__(self):
        super().__init__()
        self.approvals = []
        self.approved_dispatches = []
        self.checkpoints = 0
        self.changed = False

    def review_action(self, action):
        return {"ok": True, "requires_confirmation": True, "question": "要在这个槽位新建游戏吗？", "ticket": "f" * 32}

    def approve_action(self, ticket):
        self.approvals.append(ticket)
        return {"ok": False, "reason": "stale_confirmation"} if self.changed else {"ok": True, "approval": "a" * 32}

    def execute_approved(self, action, approval, control):
        with control.action():
            self.approved_dispatches.append((copy.deepcopy(action), approval))
            return super().execute(action, control)

    def checkpoint_due(self):
        return True

    def checkpoint(self):
        self.checkpoints += 1
        return {"ok": True, "saved": True}


class RouterAI:
    def __init__(self, autonomous, chat):
        self.autonomous, self.chat_ai = autonomous, chat

    def chat(self, messages, tools, output_tokens):
        target = self.autonomous if any(t["function"]["name"] == "game_action" for t in tools) else self.chat_ai
        return target.chat(messages, tools, output_tokens)

    def release_all(self):
        self.autonomous.release_all()
        self.chat_ai.release_all()


class CommunicationTests(IsolatedSettingsMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.settings.update({"provider": "custom", "game": "sandbox", "step_delay": 0,
            "profiles": [{"id": "custom", "base_url": "http://127.0.0.1:1"}]})
        blocker = patch("playall.llm.CompatibleAI._request", side_effect=AssertionError("No paid AI in tests"))
        blocker.start()
        self.addCleanup(blocker.stop)

    def make_runner(self, ai, game):
        for stub in (patch("playall.runner.make_ai", return_value=ai), patch("playall.runner.make_game", return_value=game)):
            stub.start()
            self.addCleanup(stub.stop)
        runner = Runner(self.settings)
        self.addCleanup(self.drain, runner, ai)
        return runner

    def drain(self, runner, ai):
        chat = runner.chat_thread
        runner.stop()
        ai.release_all()
        if runner.thread:
            runner.thread.join(3)
            self.assertFalse(runner.thread.is_alive())
        if chat:
            chat.join(3)
            self.assertFalse(chat.is_alive())

    def wait_for(self, predicate):
        deadline = time.monotonic() + 3
        while not predicate():
            if time.monotonic() >= deadline:
                self.fail("Expected communication state was not reached")
            threading.Event().wait(.005)

    def pending_batch(self):
        return batch(tool_call("game_action", {"action": "mine", "name": "iron-ore", "count": 1}, "important"),
                     tool_call("game_action", {"action": "mine", "name": "coal", "count": 2}, "must-cancel"),
                     tool_call("report_progress", {"summary": "must-not-remember-unexecuted"}, "must-cancel-summary"))

    def test_important_action_and_entire_remainder_wait_without_saving_or_calling_ai(self):
        game, ai = ConfirmationGame(), GatedAI([self.pending_batch()], gates=(2,))
        runner = self.make_runner(ai, game)
        runner.start()
        self.wait_for(lambda: runner.snapshot().get("pending_question") and bool(runner.context))
        self.assertEqual(game.executed, [])
        self.assertEqual(game.checkpoints, 0)
        self.assertEqual(len(ai.calls), 1)
        self.assertFalse(runner.resume_event.is_set())
        with self.assertRaisesRegex(ValueError, "交流窗口"):
            runner.resume()
        runner.pause()
        self.assertEqual(game.checkpoints, 0)
        self.assertTrue(all(valid_history_group(group) for group in runner.context))
        replies = [json.loads(m["content"]) for m in runner.context[-1][1:]]
        self.assertTrue(replies[0]["awaiting_user"])
        self.assertTrue(all(r.get("cancelled") for r in replies[1:]))
        self.assertEqual(runner.journal, [])
        self.assertEqual(game.approvals, [])
        runner.stop()
        runner.thread.join(2)
        self.assertEqual(game.checkpoints, 0, "Disconnect while awaiting input must not submit a game save")

    def test_explicit_approval_executes_exact_candidate_once_then_replans_without_old_siblings(self):
        game, ai = ConfirmationGame(), GatedAI([self.pending_batch()], gates=(2,))
        runner = self.make_runner(ai, game)
        runner.start()
        self.wait_for(lambda: bool(runner.snapshot().get("pending_question")) and bool(runner.context))
        question = runner.snapshot()["pending_question"]
        self.assertNotIn("ticket", question)
        runner.send_message("同意")
        self.assertTrue(ai.entered[2].wait(2))
        self.assertEqual(game.approvals, ["f" * 32])
        self.assertEqual(len(game.approved_dispatches), 1)
        self.assertEqual(game.executed, [{"action": "mine", "name": "iron-ore", "count": 1}])
        self.assertEqual(runner.journal, [])
        with self.assertRaises(ValueError):
            runner.answer_question(question["id"], "同意", approved=True)

    def test_deny_and_stale_confirmation_keep_ai_paused(self):
        for stale in (False, True):
            with self.subTest(stale=stale):
                game, ai = ConfirmationGame(), GatedAI([self.pending_batch()], gates=(2,))
                runner = self.make_runner(ai, game)
                runner.start()
                self.wait_for(lambda: bool(runner.snapshot().get("pending_question")) and bool(runner.context))
                game.changed = stale
                runner.send_message("同意" if stale else "取消")
                self.assertFalse(runner.resume_event.is_set())
                self.assertEqual(game.executed, [])
                self.assertFalse(runner.snapshot().get("pending_question"))
                self.assertEqual(len(ai.calls), 1)
                self.drain(runner, ai)

    def test_generic_ai_question_cancels_siblings_and_passes_human_answer_into_next_decision(self):
        ai = GatedAI([batch(tool_call("ask_user", {"question": "这次走哪条路？"}, "ask"),
            tool_call("game_action", {"action": "mine", "name": "coal", "count": 1}, "cancel"))], gates=(2,))
        game = RecordingGame()
        runner = self.make_runner(ai, game)
        runner.start()
        self.wait_for(lambda: bool(runner.snapshot().get("pending_question")) and bool(runner.context))
        runner.send_message("先看看左边")
        self.assertTrue(ai.entered[2].wait(2))
        self.assertEqual(game.executed, [])
        self.assertIn("先看看左边", json.dumps(ai.calls[1][0], ensure_ascii=False))
        self.assertTrue(all(valid_history_group(group) for group in runner.context))

    def test_new_instruction_cancels_inflight_game_response_and_reaches_next_decision(self):
        auto = GatedAI(gates=(1, 2))
        chat = GatedAI([text_reply("先检查资源，我记住了。")], gates=())
        ai, game = RouterAI(auto, chat), RecordingGame()
        runner = self.make_runner(ai, game)
        runner.start()
        self.assertTrue(auto.entered[1].wait(2))
        runner.send_message("先检查资源再行动")
        self.wait_for(lambda: not runner.snapshot().get("chat_busy"))
        auto.release[1].set()
        self.assertTrue(auto.entered[2].wait(2))
        self.assertEqual(game.executed, [])
        self.assertIn("先检查资源再行动", json.dumps(auto.calls[1][0], ensure_ascii=False))
        self.assertNotIn("game_action", [t["function"]["name"] for t in chat.calls[0][1]])

    def test_paused_chat_answers_without_starting_game_or_executing_injected_game_tools(self):
        auto = GatedAI(gates=())
        chat = GatedAI([text_reply("当前已连接。", tool_call("game_action", {"action": "mine", "name": "coal", "count": 1}, "injected"))], gates=())
        ai, game = RouterAI(auto, chat), RecordingGame()
        runner = self.make_runner(ai, game)
        runner.start({"connect_only": True})
        self.wait_for(lambda: runner.snapshot()["status"] == "paused")
        runner.send_message("现在连上了吗？")
        self.wait_for(lambda: not runner.snapshot().get("chat_busy"))
        self.assertEqual(game.executed, [])
        self.assertEqual(auto.calls, [])
        self.assertFalse(runner.resume_event.is_set())
        self.assertFalse(runner.snapshot()["ai_started"])
        saved = json.loads((self.directory / "sessions/sandbox.json").read_text(encoding="utf-8"))
        self.assertEqual([m["role"] for m in saved["conversation"]], ["user", "assistant"])

    def test_paused_chat_receives_executed_action_and_result_without_private_reasoning(self):
        first = response({"action": "mine", "name": "iron-ore", "count": 1})
        first["choices"][0]["message"]["reasoning_content"] = "PRIVATE-REASONING-NOT-FOR-CHAT"
        auto = GatedAI([first], gates=(2,))
        chat = GatedAI([text_reply("刚才采矿成功，保持暂停。")], gates=())
        game = RecordingGame()
        runner = self.make_runner(RouterAI(auto, chat), game)
        runner.start()
        self.assertTrue(auto.entered[2].wait(2))
        runner.pause()
        runner.send_message("刚才采矿做成了吗？保持暂停。")
        self.wait_for(lambda: not runner.snapshot().get("chat_busy"))
        context = json.loads(chat.calls[0][0][1]["content"].split("：", 1)[1])
        self.assertEqual(context["recent_game_tool_results"], [{
            "action": {"action": "mine", "name": "iron-ore", "count": 1},
            "result": {"ok": True, "message": "测试动作完成"}}])
        self.assertNotIn("PRIVATE-REASONING-NOT-FOR-CHAT", json.dumps(chat.calls[0][0]))
        self.assertEqual(game.executed, [{"action": "mine", "name": "iron-ore", "count": 1}])
        self.assertFalse(runner.resume_event.is_set())

    def test_question_about_pending_operation_does_not_approve_or_resume_it(self):
        auto = GatedAI([self.pending_batch()], gates=(2,))
        chat = GatedAI([text_reply("会改变游戏进度，请明确确认。", tool_call("play_control", {"state": "resume"}, "cannot-bypass"))], gates=())
        game = ConfirmationGame()
        runner = self.make_runner(RouterAI(auto, chat), game)
        runner.start()
        self.wait_for(lambda: bool(runner.snapshot().get("pending_question")) and bool(runner.context))
        runner.send_message("这个会覆盖进度吗？")
        self.wait_for(lambda: not runner.snapshot().get("chat_busy"))
        self.assertTrue(runner.snapshot().get("pending_question"))
        self.assertEqual(game.approvals, [])
        self.assertEqual(game.executed, [])
        self.assertEqual(game.checkpoints, 0)
        self.assertFalse(runner.resume_event.is_set())
        context = json.loads(chat.calls[0][0][1]["content"].split("：", 1)[1])
        records = context["recent_game_tool_results"]
        self.assertTrue(records[0]["result"]["awaiting_user"])
        self.assertTrue(records[1]["result"]["cancelled"])

    def test_manual_pause_during_chat_reply_prevents_automatic_resume(self):
        auto, chat = GatedAI(gates=(1,)), GatedAI([text_reply()], gates=(1,))
        ai, game = RouterAI(auto, chat), RecordingGame()
        runner = self.make_runner(ai, game)
        runner.start()
        self.assertTrue(auto.entered[1].wait(2))
        runner.send_message("当前资源有多少？")
        self.assertTrue(chat.entered[1].wait(2))
        runner.pause()
        chat.release_all()
        self.wait_for(lambda: not runner.snapshot().get("chat_busy"))
        auto.release_all()
        self.assertFalse(runner.resume_event.is_set())
        self.assertEqual(game.executed, [])

    def test_messages_queued_during_a_reply_are_answered_and_not_lost(self):
        auto = GatedAI(gates=())
        chat = GatedAI([text_reply("第一条收到。"), text_reply("第二条也收到。")], gates=(1,))
        runner = self.make_runner(RouterAI(auto, chat), RecordingGame())
        runner.start({"connect_only": True})
        self.wait_for(lambda: runner.snapshot()["status"] == "paused")
        runner.send_message("第一条问题")
        self.assertTrue(chat.entered[1].wait(2))
        runner.send_message("第二条问题")
        chat.release_all()
        self.wait_for(lambda: not runner.snapshot().get("chat_busy"))
        self.assertEqual(len(chat.calls), 2)
        self.assertIn("第二条问题", json.dumps(chat.calls[1][0], ensure_ascii=False))
        self.assertEqual(auto.calls, [])
        self.assertEqual([m["message"] for m in runner.snapshot()["conversation"] if m["role"] == "user"],
                         ["第一条问题", "第二条问题"])

    def test_stop_discards_queued_replies_and_keeps_messages_sent_before_response(self):
        auto, chat = GatedAI(gates=()), GatedAI([text_reply()], gates=(1,))
        runner = self.make_runner(RouterAI(auto, chat), RecordingGame())
        runner.send_message("回复前关软件也要保留这条消息")
        self.assertTrue(chat.entered[1].wait(2))
        runner.send_message("这条也要保留，但关闭后不能再发请求")
        worker = runner.chat_thread
        runner.stop()
        chat.release_all()
        worker.join(2)
        self.assertFalse(worker.is_alive())
        self.assertEqual(len(chat.calls), 1)
        saved = json.loads((self.directory / "sessions/sandbox.json").read_text(encoding="utf-8"))
        self.assertEqual(len(saved["conversation"]), 2)
        self.assertTrue(all(m["role"] == "user" for m in saved["conversation"]))


if __name__ == "__main__":
    unittest.main()
