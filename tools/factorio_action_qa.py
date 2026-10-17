"""Run an explicit action list on the human-authorized, selected test map.

No launcher, OS input, model request, grants, research unlocks or arbitrary Lua.
Every action/result is recorded; the first failure stops the list. Always
pauses and releases this tool's own connection. Named saves are never written.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from playall.games.actions import validate_action
from playall.games.factorio_attach import Factorio, BUNDLE, BRIDGE_VERSION, digest, profile_paths
from playall.processes import inspect_process
from playall.storage import atomic_json
from tools.factorio_gameplay_qa import Control


def brief(state):
    return {key: value for key, value in state.items() if key not in ("map",)}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("pid", type=int)
    parser.add_argument("plan", type=Path)
    args = parser.parse_args()
    plan = json.loads(args.plan.read_text(encoding="utf-8-sig"))
    steps = plan["actions"]
    assert isinstance(steps, list) and steps
    for step in steps:
        validate_action(step["action"])
        assert type(step.get("expected_ok", True)) is bool
    selected = inspect_process(args.pid)
    if not selected or selected.name.casefold() != "factorio.exe":
        raise ValueError("The selected Factorio process is not running")
    run = ROOT / "data/qa/factorio" / ("actions-" + time.strftime("%Y%m%d-%H%M%S"))
    run.mkdir(parents=True)
    output = run / "evidence.json"
    stop = ROOT / "data/qa/factorio/STOP"
    control = Control(stop)
    control.check()
    _, profile = profile_paths(Path(selected.path))
    named = profile / "saves/test.zip"
    before_hash = digest(named) if named.is_file() else None
    evidence = {"label": plan.get("label", args.plan.stem), "status": "running", "paid_ai_called": False,
                "os_input_sent": False, "source_sha256": digest(BUNDLE / "control.lua"),
                "bridge": BRIDGE_VERSION, "pid": selected.pid, "actions": [], "named_test_sha256": before_hash}
    atomic_json(output, evidence)
    game = Factorio(run, {"game_process": selected.selection()}, lambda *a, **kw: None)
    try:
        game.prepare(control)
        game.connect_window(control)
        evidence["before"] = brief(game.observe())
        assert evidence["before"]["paused"] and evidence["before"]["manual_control"]
        game.resume()
        for index, step in enumerate(steps, 1):
            control.check()
            record = {"index": index, "action": step["action"], "started_at": time.time()}
            evidence["actions"].append(record)
            atomic_json(output, evidence)
            print("ACTION", index, json.dumps(step["action"], ensure_ascii=False), flush=True)
            result = game.execute(step["action"], control)
            record.update(result=result, elapsed=time.time() - record["started_at"], after=brief(game.observe()))
            atomic_json(output, evidence)
            assert result.get("ok") is step.get("expected_ok", True), result
            for path, expected in step.get("expect", {}).items():
                value = record["after"]
                for part in path.split("."):
                    value = value.get(part) if isinstance(value, dict) else None
                assert value == expected, (path, expected, value)
            print("RESULT", index, json.dumps(result, ensure_ascii=False)[:600], flush=True)
        game.pause()
        evidence["after"] = brief(game.observe())
        if plan.get("checkpoint"):
            evidence["checkpoint"] = game.checkpoint()
        assert not before_hash or digest(named) == before_hash
        evidence["status"] = "passed"
    except BaseException as error:
        evidence.update(status="failed", error=type(error).__name__ + ": " + str(error))
        raise
    finally:
        try:
            if game.rcon:
                game.pause()
                evidence["final"] = brief(game.observe())
        except Exception as error:
            evidence["cleanup_error"] = type(error).__name__
        game.close()
        evidence["connection_released"] = game.rcon is None
        evidence["named_test_unchanged"] = not before_hash or digest(named) == before_hash
        atomic_json(output, evidence)
        atomic_json(ROOT / "data/qa/factorio/latest-actions.json", {"status": evidence["status"], "path": str(output)})
        print("EVIDENCE", output, flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
