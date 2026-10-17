"""Exercise the actual selected single-player through the shipped interfaces.

Requires explicit human authorization to test gameplay and a disconnected
PlayALL window. No OS input, arbitrary Lua, game launcher or paid AI is used.
Original named saves are backed up, and never loaded or overwritten here.
"""
from __future__ import annotations

import argparse
from contextlib import nullcontext
import json
import math
import os
from pathlib import Path
import shutil
import sys
import time
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.pop("DEEPSEEK_API_KEY", None)
os.environ.pop("PLAYALL_API_KEY", None)
from playall.config import Settings
from playall.games.factorio_attach import Factorio, BUNDLE, digest, profile_paths
from playall.processes import inspect_process, validate_selection
from playall.runner import Runner
from playall.storage import atomic_json


def summary(state):
    return {key: state.get(key) for key in ("tick", "character_id", "position", "inventory", "buildings",
            "resources", "iron_plates_produced", "paused", "manual_control", "busy", "job", "last_result")}


class Control:
    def __init__(self, stop):
        self.stop = stop
        self.reported = 0

    def check(self):
        if self.stop.exists():
            raise InterruptedError("Gameplay verification stopped")

    def sleep(self, seconds):
        until = time.monotonic() + seconds
        while time.monotonic() < until:
            self.check()
            time.sleep(max(0, min(.1, until - time.monotonic())))

    def publish(self, state):
        if time.monotonic() - self.reported > 8:
            self.reported = time.monotonic()
            print("PROGRESS", json.dumps({"job": state.get("job"), "position": state.get("position")}, ensure_ascii=False), flush=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("pid", type=int)
    args = parser.parse_args()
    selected = inspect_process(args.pid)
    if not selected or selected.name.casefold() != "factorio.exe":
        raise ValueError("Selected Factorio is not running")
    run = ROOT / "data/qa/factorio" / ("gameplay-" + time.strftime("%Y%m%d-%H%M%S"))
    run.mkdir(parents=True)
    evidence = {"status": "running", "pid": selected.pid, "source_sha256": digest(BUNDLE / "control.lua"),
                "paid_ai_called": False, "os_input_sent": False, "checks": [], "actions": []}
    output = run / "evidence.json"
    stop = ROOT / "data/qa/factorio/STOP"
    if stop.exists():
        raise InterruptedError("Remove the previous QA STOP file explicitly before running")
    control = Control(stop)
    config, data = profile_paths(Path(selected.path))
    named_save = data / "saves/test.zip"
    named_hash = digest(named_save)
    backup = run / "before-test"
    backup.mkdir()
    for path in [named_save, *(data / "saves").glob("_autosave-playall-*.zip")]:
        shutil.copy2(path, backup / path.name)
    evidence["named_test_save_sha256"] = named_hash
    atomic_json(output, evidence)
    settings = Settings(run / "app")
    settings.update({"game": "factorio", "provider": "rehearsal", "step_delay": .2})
    runner = Runner(settings)
    game = None
    def save():
        atomic_json(output, evidence)
    def wait_for(predicate, seconds, description):
        deadline, reported = time.monotonic() + seconds, 0
        while True:
            control.check()
            state = runner.snapshot()
            if predicate(state):
                return state
            if state["status"] == "error" or not runner.thread.is_alive():
                raise RuntimeError(description + ": " + str(state.get("message")))
            if time.monotonic() >= deadline:
                raise TimeoutError(description + ": " + json.dumps(summary(state.get("observation") or {}), ensure_ascii=False))
            if time.monotonic() - reported > 8:
                reported = time.monotonic()
                obs = state.get("observation") or {}
                print("RUNNER", json.dumps({"stage": description, "status": state["status"], "job": obs.get("job"),
                      "position": obs.get("position"), "inventory": obs.get("inventory"),
                      "plates_produced": obs.get("iron_plates_produced")}, ensure_ascii=False), flush=True)
            time.sleep(.15)
    try:
        runner.start({"connect_only": True, "game": "factorio", "provider": "rehearsal", "game_process": selected.selection()})
        wait_for(lambda s: s.get("connected"), 12, "initial connection")
        game = runner.game
        before = game.observe()
        evidence["before"] = summary(before)
        assert not runner.snapshot().get("ai_started") and runner.snapshot()["turns"] == 0
        evidence["checks"].append("real Runner connect_only: no decision or game action")
        # Protect the exact current in-memory state, including the human's
        # movement after their previous manual save. This is a dedicated slot.
        baseline = data / "saves/_autosave-playall-3.zip"
        old = baseline.stat().st_mtime_ns if baseline.exists() else 0
        game._remote("checkpoint", 3)
        until = time.monotonic() + 12
        while not baseline.exists() or baseline.stat().st_mtime_ns == old:
            control.check()
            if time.monotonic() > until:
                raise RuntimeError("Could not confirm baseline autosave")
            time.sleep(.1)
        shutil.copy2(baseline, backup / "current-memory-baseline.zip")
        connection = game.rcon
        runner.resume()
        wait_for(lambda s: (s.get("observation") or {}).get("busy"), 20, "begin first native task")
        time.sleep(.5)
        runner.pause()
        paused = game.observe()
        assert paused["paused"] and paused["manual_control"] and not paused["busy"]
        turns = runner.snapshot()["turns"]
        time.sleep(.5)
        again = game.observe()
        assert again["position"] == paused["position"] and runner.snapshot()["turns"] == turns
        evidence["checks"].append("pause cancels native task, keeps character stationary and stops decisions")
        evidence["paused"] = summary(again)
        save()
        runner.resume()
        assert runner.game is game and game.rcon is connection
        initial_production = before.get("iron_plates_produced", 0)
        def produced(s):
            obs = s.get("observation") or {}
            return (obs.get("iron_plates_produced", 0) - initial_production >= 3
                    and obs.get("inventory", {}).get("iron-plate", 0) >= 3)
        result = wait_for(produced, 360, "complete mining, furnace, smelting and extraction")
        runner.pause()
        evidence["production"] = summary(game.observe())
        evidence["checks"].append("same Runner/connection resumed; actual mining, native furnace build, fuel, ore, smelting and extraction succeeded")
        save()
        runner.stop()
        runner.thread.join(20)
        assert not runner.thread.is_alive(), "Runner failed to detach"
        evidence["checks"].append("Runner stop released connection and retained game")
        game = Factorio(run / "direct", {"game_process": selected.selection()}, lambda *a: None)
        game.prepare(control)
        game.connect_window(control)
        game.resume()
        def act(**action):
            print("ACTION", json.dumps(action, ensure_ascii=False), flush=True)
            result = game.execute(action, control)
            observation = game.observe()
            evidence["actions"].append({"action": action, "result": result, "after": summary(observation)})
            save()
            print("RESULT", json.dumps(result, ensure_ascii=False), flush=True)
            if not result.get("ok"):
                raise RuntimeError("Native action refused: " + json.dumps(result, ensure_ascii=False))
            return observation
        act(action="inspect", name="iron-gear-wheel")
        act(action="recipes", name="transport-belt")
        count = game.observe()["inventory"].get("iron-gear-wheel", 0)
        crafted = act(action="craft", name="iron-gear-wheel", count=1)
        assert crafted["inventory"].get("iron-gear-wheel", 0) >= count + 1
        evidence["checks"].append("recipe inspection and real handcrafting consume real materials")
        if crafted["inventory"].get("wood", 0) < 2:
            act(action="mine", name="wood", count=1)
        act(action="craft", name="wooden-chest", count=1)
        def build_near(name):
            origin = game.observe()["position"]
            for dx, dy in ((4,0),(-4,0),(0,4),(0,-4),(5,3),(-5,-3),(7,0),(0,7)):
                action = {"action": "build", "name": name, "x": math.floor(origin["x"]) + dx + .5,
                          "y": math.floor(origin["y"]) + dy + .5}
                print("ACTION", json.dumps(action), flush=True)
                result = game.execute(action, control)
                state = game.observe()
                evidence["actions"].append({"action": action, "result": result, "after": summary(state)})
                save()
                if result.get("ok"):
                    return next(b for b in state["buildings"] if b["name"] == name and abs(b["x"]-action["x"]) <= 1 and abs(b["y"]-action["y"]) <= 1)
            raise RuntimeError("No valid placement found nearby")
        chest = build_near("wooden-chest")
        xy = {"x": chest["x"], "y": chest["y"]}
        inserted = act(action="insert", name="iron-plate", count=1, inventory="chest", **xy)
        assert next(b for b in inserted["buildings"] if b.get("id")==chest["id"])["contents"].get("iron-plate",0)==1
        act(action="extract", name="iron-plate", count=1, inventory="chest", **xy)
        removed = act(action="deconstruct", count=1, **xy)
        assert not any(b.get("id")==chest["id"] for b in removed["buildings"])
        evidence["checks"].append("native chest placement, transfer both ways and mining back the test-owned empty chest")
        game.pause()
        saved = game.checkpoint()
        assert saved.get("saved")
        slot = game.save_slot
        archive = data / "saves" / f"_autosave-playall-{slot}.zip"
        with zipfile.ZipFile(archive) as z:
            assert any(n.endswith(("/level.dat", "/level.dat0")) for n in z.namelist())
        stamp = archive.stat().st_mtime_ns
        repeated = game.checkpoint()
        assert repeated.get("unchanged") and archive.stat().st_mtime_ns == stamp
        expected = game.observe()
        evidence["expected_reload"] = summary(expected)
        name = "PlayALL-verify-" + time.strftime("%Y%m%d-%H%M%S") + ".zip"
        restore = data / "saves" / name
        if restore.exists():
            raise FileExistsError(restore)
        shutil.copy2(archive, restore)
        evidence["restore_save_name"] = name
        evidence["restore_save_sha256"] = digest(restore)
        assert digest(named_save) == named_hash, "Named test save was unexpectedly modified"
        evidence["checks"].append("native autosave is valid, duplicate checkpoint does not rewrite, original named save unchanged")
        evidence["status"] = "passed; reload check pending human load"
    except BaseException as error:
        evidence.update(status="failed", error=type(error).__name__ + ": " + str(error))
        raise
    finally:
        runner.stop()
        if runner.thread:
            runner.thread.join(20)
        if game:
            try:
                game.pause()
            except Exception:
                pass
            game.close()
        evidence["named_test_save_unchanged"] = digest(named_save) == named_hash
        save()
        atomic_json(ROOT / "data/qa/factorio/latest-gameplay.json", {"evidence": str(output), "status": evidence["status"]})
        print("EVIDENCE", str(output), evidence["status"], flush=True)


if __name__ == "__main__":
    main()
