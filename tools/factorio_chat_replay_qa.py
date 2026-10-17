"""Replay a real isolated play session into paused chat; never dispatch actions.

The earlier model action must have completed successfully. This exercises
normal saved-session restoration and the production chat context with the
configured provider. No credentials, request body, or private reasoning are
written to the evidence.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import shutil
import sys
import time
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from playall.config import Settings
from playall.games.factorio_attach import Factorio, BUNDLE, digest
from playall.llm import make_ai
from playall.processes import inspect_process
from playall.runner import Runner, game_tool_records
from playall.storage import atomic_json


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("pid", type=int)
    parser.add_argument("prior_evidence", type=Path)
    args = parser.parse_args()
    prior_path = args.prior_evidence.resolve()
    prior = json.loads(prior_path.read_text(encoding="utf-8"))
    assert prior.get("mode") == "craft" and any(
        a["action"].get("action") == "craft" and a["result"].get("ok") for a in prior["native_actions"])
    selected = inspect_process(args.pid)
    assert selected and selected.name.casefold() == "factorio.exe"
    run = ROOT / "data/qa/factorio" / ("chat-replay-" + time.strftime("%Y%m%d-%H%M%S"))
    run.mkdir(parents=True)
    source = Settings()
    protected = [source.path, source.directory / "sessions/factorio.json"]
    hashes = {p: digest(p) if p.exists() else None for p in protected}
    isolated = Settings(run / "app")
    isolated.data = source.snapshot()
    isolated.data.update(provider="deepseek", game="factorio", system_prompt="保持暂停，只回答问题，不执行新的游戏动作。")
    session = isolated.directory / "sessions/factorio.json"
    session.parent.mkdir(parents=True)
    shutil.copy2(prior_path.parent / "app/sessions/factorio.json", session)
    native = Factorio(isolated.directory, {"game_process": selected.selection()}, lambda *a, **kw: None)
    calls, attempted = [], []
    evidence = {"status": "running", "paid_ai_called": True, "source": str(prior_path),
        "source_sha256": digest(BUNDLE / "control.lua"), "os_input_sent": False,
        "scope": "real paused chat with restored successful native craft record"}
    output = run / "evidence.json"
    stop = ROOT / "data/qa/factorio/STOP"

    class NoActionGate:
        def __getattr__(self, name):
            return getattr(native, name)

        def execute(self, action, control):
            attempted.append(action)
            raise AssertionError("Paused chat must never dispatch a game action")

    class CountedAI:
        def __init__(self, profile):
            self.actual = make_ai(profile)

        def chat(self, messages, tools, output_tokens):
            assert [t["function"]["name"] for t in tools] == ["play_control"]
            context = json.loads(messages[1]["content"].split("：", 1)[1])
            assert context["recent_game_tool_results"][-1]["action"]["name"] == "iron-gear-wheel"
            payload = "\n".join(m.get("content") or "" for m in messages)
            assert selected.path not in payload and '"game_process"' not in payload
            assert native.password not in payload
            calls.append(time.time())
            return self.actual.chat(messages, tools, output_tokens)

    runner = Runner(isolated)

    def wait_for(predicate, seconds):
        deadline = time.monotonic() + seconds
        while not predicate():
            assert not stop.exists(), "Stopped by human"
            assert time.monotonic() < deadline, runner.snapshot().get("message")
            assert runner.thread and runner.thread.is_alive(), runner.snapshot().get("message")
            time.sleep(.03)

    try:
        with patch("playall.runner.make_game", return_value=NoActionGate()), patch("playall.runner.make_ai", CountedAI):
            runner.start({"game": "factorio", "provider": "deepseek", "connect_only": True,
                          "game_process": selected.selection()})
            wait_for(lambda: runner.snapshot().get("connected"), 15)
            assert not calls and native.observe()["paused"]
            records = game_tool_records(runner.context)
            assert records and records[-1]["action"]["name"] == "iron-gear-wheel" and records[-1]["result"]["ok"]
            before = native.observe()
            before_messages = len(runner.snapshot()["conversation"])
            runner.send_message("保持暂停。刚才那个齿轮制作操作的结果是什么？请根据实际工具记录，用两句话回答，不要执行新动作。")
            wait_for(lambda: not runner.snapshot().get("chat_busy"), 55)
            after = native.observe()
            replies = [m["message"] for m in runner.snapshot()["conversation"][before_messages:] if m["role"] == "assistant"]
            assert replies and len(calls) == 1 and not attempted
            assert after["paused"] and after["manual_control"] and not runner.resume_event.is_set()
            assert before["inventory"] == after["inventory"] and before["position"] == after["position"]
            evidence.update(status="passed; response accuracy requires review", reply=replies[-1], model_requests=1,
                restored_tool_record=records[-1], no_game_actions=True, inventory_unchanged=True,
                position_unchanged=True, remained_paused=True)
    except BaseException as error:
        evidence.update(status="failed", error=type(error).__name__ + ": " + str(error), model_requests=len(calls))
        raise
    finally:
        runner.stop()
        if runner.thread:
            runner.thread.join(55)
        if runner.chat_thread:
            runner.chat_thread.join(55)
        evidence["native_connection_released"] = native.rcon is None
        evidence["original_settings_and_conversation_unchanged"] = all((digest(p) if p.exists() else None) == h for p, h in hashes.items())
        atomic_json(output, evidence)
        print("EVIDENCE", output, flush=True)
        if evidence.get("reply"):
            print("PUBLIC_REPLY", evidence["reply"], flush=True)


if __name__ == "__main__":
    main()
