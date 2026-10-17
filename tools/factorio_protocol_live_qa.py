"""Native single-player protocol, cancellation and checkpoint regression.

Uses only the shipped bridge on the user-authorized test map. One wood is
temporarily put in an existing chest and returned. Movement is bounded; no
game launch/load, OS input, arbitrary Lua, grants or model request is used.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import shutil
import sys
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from playall.games.factorio_attach import Factorio, BUNDLE, digest, profile_paths
from playall.processes import inspect_process
from playall.storage import atomic_json
from tools.factorio_gameplay_qa import Control


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("pid", type=int)
    parser.add_argument("--chest-x", type=float, default=108.5)
    parser.add_argument("--chest-y", type=float, default=-28.5)
    args = parser.parse_args()
    selected = inspect_process(args.pid)
    assert selected and selected.name.casefold() == "factorio.exe"
    run = ROOT / "data/qa/factorio" / ("protocol-" + time.strftime("%Y%m%d-%H%M%S"))
    run.mkdir(parents=True)
    control = Control(ROOT / "data/qa/factorio/STOP")
    control.check()
    _, data = profile_paths(Path(selected.path))
    named = data / "saves/test.zip"
    original_hash = digest(named)
    backup = run / "before-saves"
    backup.mkdir()
    for path in (data / "saves").glob("_autosave-playall-*.zip"):
        shutil.copy2(path, backup / path.name)
    evidence = {"status": "running", "paid_ai_called": False, "os_input_sent": False,
                "source_sha256": digest(BUNDLE / "control.lua"), "checks": [], "native_actions": []}
    output = run / "evidence.json"
    game = None

    def save():
        atomic_json(output, evidence)

    def passed(name, **details):
        evidence["checks"].append({"name": name, "status": "passed", **details})
        save()
        print("PASSED", name, flush=True)

    def connect():
        instance = Factorio(run, {"game_process": selected.selection()}, lambda *a, **kw: None)
        instance.prepare(control)
        instance.connect_window(control)
        return instance

    def raw(method, value=None, *, key=None):
        request = {"protocol": 2, "id": key or uuid.uuid4().hex, "session": game.session, "method": method}
        if value is not None:
            request["value"] = value
        with game.lock:
            game._identity()
            reply = game.rcon.command("/playall-bridge " + json.dumps(request))
        response = json.loads(reply.split("PLAYALL:", 1)[1].strip())
        assert (response["id"], response["session"], response["protocol"]) == (request["id"], game.session, 2)
        return response

    def finish_action():
        until = time.monotonic() + 20
        while True:
            control.check()
            state = game.observe()
            if not state["busy"]:
                assert state["last_result"]["ok"], state["last_result"]
                return state
            assert time.monotonic() < until, state.get("job")
            time.sleep(.05)

    def execute(action):
        evidence["native_actions"].append(action)
        result = game.execute(action, control)
        assert result["ok"], result
        return result

    try:
        game = connect()
        baseline = game.observe()
        evidence["before"] = {k: v for k, v in baseline.items() if k != "map"}
        assert baseline["paused"] and baseline["inventory"].get("wood", 0) >= 1
        game.resume()
        wood_before = baseline["inventory"]["wood"]
        action = {"action": "insert", "name": "wood", "count": 1, "inventory": "chest", "x": args.chest_x, "y": args.chest_y}
        key = uuid.uuid4().hex
        first = raw("execute", action, key=key)
        assert first["ok"] and first["result"]["ok"], first
        finish_action()
        assert game.observe()["inventory"].get("wood", 0) == wood_before - 1
        second = raw("execute", action, key=key)
        assert second == first
        assert game.observe()["inventory"].get("wood", 0) == wood_before - 1
        changed = raw("execute", {**action, "count": 2}, key=key)
        assert not changed["ok"] and changed["error"] == "request_id_reused"
        assert not game.observe()["paused"]
        passed("duplicate_request_consumes_item_once_and_changed_payload_is_refused")
        execute({**action, "action": "extract"})
        assert game.observe()["inventory"]["wood"] == wood_before
        passed("test_item_returned_to_player")

        start = game.observe()["position"]
        response = game._remote("execute", {"action": "move", "x": start["x"] + 14, "y": start["y"] - 2})
        assert response["ok"] and response["busy"], response
        time.sleep(.4)
        assert game.observe()["busy"], "Movement completed before the pause probe"
        game.pause()
        before = game.observe()
        assert before["paused"] and not before["busy"]
        time.sleep(.6)
        after = game.observe()
        assert before["position"] == after["position"], (before["position"], after["position"])
        assert game.execute({"action": "move", "x": start["x"], "y": start["y"]}, control)["ok"] is False
        connection = game.rcon
        game.resume()
        game.connect_window(control)
        assert game.rcon is connection and not game.observe()["paused"]
        passed("pause_stops_walking_and_resume_reuses_connection", position=after["position"])

        invalid = raw("execute", {"action": "mine", "name": "iron-ore", "lua": "forbidden"})
        assert not invalid["ok"] and game.observe()["paused"]
        passed("native_unknown_parameter_refused_and_ai_paused")

        existing = [(p.stat().st_mtime_ns, i) for i in (1, 2, 3)
                    if (p := data / "saves" / f"_autosave-playall-{i}.zip").is_file()]
        expected_first = max(existing)[1] % 3 + 1 if existing else 1
        result = game.checkpoint()
        assert result["saved"] and game.save_slot == expected_first, (result, game.save_slot, expected_first)
        stamp = (data / "saves" / f"_autosave-playall-{game.save_slot}.zip").stat().st_mtime_ns
        repeat = game.checkpoint()
        assert repeat["unchanged"] and (data / "saves" / f"_autosave-playall-{game.save_slot}.zip").stat().st_mtime_ns == stamp
        first_slot = game.save_slot
        game.close()
        game = connect()
        game.resume()
        position = game.observe()["position"]
        execute({"action": "move", "x": position["x"], "y": position["y"] + 2})
        game.pause()
        assert game.checkpoint()["saved"] and game.save_slot == first_slot % 3 + 1
        passed("native_checkpoint_rotation_survives_reconnect_and_repeat_is_deduplicated",
               slots=[first_slot, game.save_slot])

        game.resume()
        assert game._remote("execute", {"action": "wait", "seconds": 30})["busy"]
        game.heartbeat_stop.set()
        game.heartbeat_thread.join(6)
        until = time.monotonic() + 8
        while not game._remote("hello")["paused"]:
            assert time.monotonic() < until, "Native lease did not expire"
            control.sleep(.2)
        assert not raw("observe")["ok"]
        hello = game._remote("hello")
        assert hello["manual_control"]
        passed("missing_heartbeat_expires_lease_and_cancels_pending_wait")
        evidence["after"] = hello
        assert digest(named) == original_hash
        evidence["status"] = "passed"
    except BaseException as error:
        evidence.update(status="failed", error=type(error).__name__ + ": " + str(error))
        raise
    finally:
        if game:
            try:
                game.pause()
            except (OSError, ValueError, RuntimeError):
                pass  # Expiry already returned control to the player.
            game.close()
        evidence["named_test_unchanged"] = digest(named) == original_hash
        evidence["connection_released"] = game is None or game.rcon is None
        save()
        print("EVIDENCE", output, flush=True)


if __name__ == "__main__":
    main()
