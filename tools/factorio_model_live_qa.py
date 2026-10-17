"""Real configured AI through the production Runner on a live test map.

The QA gate permits read-only actions, or one explicitly selected gear craft,
and pauses after the first result.
Then it tests ordinary conversation while paused. The key is used through
Settings/CompatibleAI; credentials and request headers are never emitted.
User settings and the user's existing AI conversation are not overwritten.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
import threading
import time
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from playall.config import Settings
from playall.games.factorio_attach import Factorio, BUNDLE, digest
from playall.llm import make_ai
from playall.processes import inspect_process
from playall.runner import Runner
from playall.storage import atomic_json


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("pid", type=int)
    parser.add_argument("--mode", choices=("inspect", "craft"), default="inspect")
    args = parser.parse_args()
    selected = inspect_process(args.pid)
    assert selected and selected.name.casefold() == "factorio.exe"
    stop = ROOT / "data/qa/factorio/STOP"
    run = ROOT / "data/qa/factorio" / ("model-" + time.strftime("%Y%m%d-%H%M%S"))
    run.mkdir(parents=True)
    original = Settings()
    original_paths = [original.path, original.directory / "sessions/factorio.json"]
    original_hashes = {p: digest(p) if p.exists() else None for p in original_paths}
    isolated = Settings(run / "app")
    isolated.data = original.snapshot()  # Encrypted profile data stays in memory.
    instruction = ("本次只读检查：先调用 game_action 的 inspect，查看 steam-power 科技的当前状态和解锁条件。不得进行任何改变游戏的动作。用简短中文说明观察，后续等待玩家指令。"
                   if args.mode == "inspect" else
                   "本次验证只进行一个操作：先调用 game_action，action=craft、name=iron-gear-wheel、count=1，用当前背包材料手工制作一个铁齿轮。用简短中文说明这次操作的目的。不要进行任何其他游戏动作，完成后等玩家指令。")
    isolated.data.update(provider="deepseek", game="factorio", step_delay=.5,
        system_prompt=instruction)
    completed = threading.Event()
    actions, calls, violations = [], [], []
    native = Factorio(isolated.directory, {"game_process": selected.selection()}, lambda *a, **kw: None)
    evidence = {"status": "running", "paid_ai_called": True, "provider": "deepseek",
                "os_input_sent": False, "source_sha256": digest(BUNDLE / "control.lua"),
                "checks": [], "native_actions": actions, "mode": args.mode,
                "scope": "real Runner, " + ("read-only inspection" if args.mode == "inspect" else "one native gear craft") + ", paused conversation"}
    output = run / "evidence.json"

    class ActionGate:
        def __getattr__(self, name):
            return getattr(native, name)

        def execute(self, action, control):
            allowed = (action.get("action") in ("observe", "inspect", "recipes") if args.mode == "inspect" else
                       action.get("action") == "craft" and action.get("name") == "iron-gear-wheel"
                       and action.get("count", 1) == 1 and set(action) <= {"action", "name", "count"})
            if not allowed:
                violations.append(action)
                completed.set()
                raise AssertionError("QA rejected a mutating action before the native game")
            result = native.execute(action, control)
            actions.append({"action": action, "result": result})
            completed.set()
            return result

    class CountedAI:
        def __init__(self, profile):
            self.actual = make_ai(profile)

        def chat(self, messages, tools, output_tokens):
            calls.append({"time": time.time(), "tool_names": [t["function"]["name"] for t in tools]})
            payload = json.dumps({"messages": messages, "tools": tools}, ensure_ascii=False)
            assert selected.path not in payload and '"game_process"' not in payload
            if native.rcon:
                assert native.password not in payload
            return self.actual.chat(messages, tools, output_tokens)

    runner = Runner(isolated)

    def wait_for(predicate, seconds):
        deadline = time.monotonic() + seconds
        while not predicate():
            assert not stop.exists(), "Stopped by human"
            assert time.monotonic() < deadline, runner.snapshot().get("message")
            if runner.thread and not runner.thread.is_alive():
                raise AssertionError(runner.snapshot().get("message"))
            time.sleep(.03)

    def passed(name):
        evidence["checks"].append({"name": name, "status": "passed"})
        atomic_json(output, evidence)
        print("PASSED", name, flush=True)

    try:
        with patch("playall.runner.make_game", return_value=ActionGate()), patch("playall.runner.make_ai", CountedAI):
            runner.start({"game": "factorio", "provider": "deepseek", "connect_only": True,
                          "game_process": selected.selection()})
            wait_for(lambda: runner.snapshot().get("connected"), 15)
            assert not calls and not actions and native.observe()["paused"]
            before = native.observe()["inventory"]
            if args.mode == "craft":
                assert before.get("iron-plate", 0) >= 2, "The native recipe needs two real iron plates"
            passed("connect_only_makes_no_model_request_or_game_action")
            runner.resume()
            wait_for(completed.is_set, 55)
            runner.pause()
            assert actions and not violations
            assert any(a["action"].get("action") == args.mode and a["result"].get("ok") for a in actions)
            if args.mode == "craft":
                after = native.observe()["inventory"]
                assert len(actions) == 1
                assert after.get("iron-gear-wheel", 0) == before.get("iron-gear-wheel", 0) + 1
                assert after.get("iron-plate", 0) == before.get("iron-plate", 0) - 2
                evidence["inventory_delta"] = {"iron-plate": -2, "iron-gear-wheel": 1}
            summaries = [event["message"] for event in runner.snapshot()["events"] if event["kind"] == "thought"]
            assert summaries, "The model did not provide a public action summary"
            evidence["public_summary_sample"] = summaries[0]
            assert native.observe()["paused"] and not runner.resume_event.is_set()
            passed("real_model_tool_call_parsed_and_executed_by_native_adapter")
            before_actions = len(actions)
            before_messages = len(runner.snapshot()["conversation"])
            runner.send_message("保持暂停，不要继续游玩。请用两句话说明刚才读到的科技状态，不要声称你操作过游戏。" if args.mode == "inspect" else
                                "保持暂停，不要继续游玩。请用两句话说明刚才的制作结果，不要进行下一步操作。")
            wait_for(lambda: not runner.snapshot().get("chat_busy"), 55)
            after = runner.snapshot()
            replies = [m["message"] for m in after["conversation"][before_messages:] if m["role"] == "assistant"]
            assert replies and not runner.resume_event.is_set() and native.observe()["paused"]
            assert len(actions) == before_actions
            passed("paused_chat_returns_real_reply_without_game_actions_or_auto_resume")
            evidence.update(status="passed", model_requests=len(calls), reply=replies[-1],
                            game_mutations_submitted=args.mode == "craft", usage={k: after[k] for k in ("prompt_tokens", "completion_tokens", "total_tokens")})
    except BaseException as error:
        evidence.update(status="failed", error=type(error).__name__ + ": " + str(error), model_requests=len(calls))
        raise
    finally:
        runner.stop()
        if runner.thread:
            runner.thread.join(55)
        if runner.chat_thread:
            runner.chat_thread.join(55)
        evidence["workers_stopped"] = not (runner.thread and runner.thread.is_alive()) and not (runner.chat_thread and runner.chat_thread.is_alive())
        evidence["native_connection_released"] = native.rcon is None
        evidence["original_settings_and_conversation_unchanged"] = all((digest(p) if p.exists() else None) == before for p, before in original_hashes.items())
        evidence["violations"] = violations
        atomic_json(output, evidence)
        print("EVIDENCE", output, flush=True)


if __name__ == "__main__":
    main()
