"""Real-engine acceptance test. Writes evidence; never calls a paid AI API."""
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from playall.config import detect_factorio
from playall.games.factorio import Factorio


class Control:
    def check(self):
        pass

    def sleep(self, seconds):
        time.sleep(seconds)


def main():
    root = Path(__file__).resolve().parents[1]
    directory = root / "data/engine-smoke"
    control = Control()
    evidence = {"engine": "Factorio", "results": []}
    game = Factorio(directory, {"factorio_path": detect_factorio(), "speed": 8}, lambda kind, text: print(kind, text, flush=True))
    try:
        game.prepare(control, new_world=True)
        before = game.observe()
        game._remote("connection", {"required": True})
        blocked = game.execute({"action": "mine", "name": "iron-ore", "count": 1}, control)
        assert blocked.get("reason") == "window_closed", blocked
        assert game.observe()["inventory"] == before["inventory"]
        assert game.observe()["tick"] == before["tick"]
        game._remote("connection", {"required": False})
        evidence["window_gate"] = {"disconnected_action_rejected": True, "inventory_unchanged": True, "tick_unchanged": True}
        evidence["before"] = {k: v for k, v in before.items() if k != "map"}
        print("CONNECTED", json.dumps(evidence["before"], ensure_ascii=False), flush=True)
        def act(**action):
            print("ACTION", json.dumps(action), flush=True)
            result = game.execute(action, control)
            evidence["results"].append({"action": action, "result": result})
            print("RESULT", json.dumps(result, ensure_ascii=False), flush=True)
            if not result.get("ok"):
                raise AssertionError(result)
            return game.observe()
        act(action="craft", name="iron-gear-wheel", count=1)
        act(action="mine", name="iron-ore", count=12)
        act(action="mine", name="coal", count=8)
        act(action="build", name="stone-furnace", x=4, y=0)
        act(action="insert", name="coal", count=8, x=4, y=0, inventory="fuel")
        act(action="insert", name="iron-ore", count=12, x=4, y=0, inventory="input")
        act(action="wait", seconds=45)
        after = act(action="extract", name="iron-plate", count=10, x=4, y=0, inventory="output")
        evidence["after"] = {k: v for k, v in after.items() if k != "map"}
        assert after["iron_plates_produced"] >= 10, after
        assert after["inventory"]["iron-gear-wheel"] >= 1, after
        assert after["inventory"]["iron-plate"] >= 16, after
        act(action="inspect", name="burner-mining-drill")
        act(action="recipes", name="transport-belt")
        # Extract without an explicit inventory tests the LuaEntity fallback.
        act(action="extract", name="iron-plate", count=1, x=4, y=0)
        # Place and fuel a drill on a real ore tile, not a simulated resource.
        patches = [p for p in after["resources"] if p["name"] == "iron-ore"]
        ore = patches[0]
        x, y = int(ore["x"]), int(ore["y"])
        act(action="build", name="burner-mining-drill", x=x, y=y, direction="east")
        act(action="mine", name="coal", count=4)
        act(action="insert", name="coal", count=3, x=x, y=y, inventory="fuel")
        act(action="rotate", x=x, y=y)
        act(action="wait", seconds=8)
        before_save = game.observe()
        game.pause()
        frozen_tick = game.observe()["tick"]
        time.sleep(.35)
        assert game.observe()["tick"] == frozen_tick
        game.resume()
        game.close()
        game = Factorio(directory, {"factorio_path": detect_factorio(), "speed": 8}, lambda kind, text: print(kind, text, flush=True))
        game.prepare(control)
        reloaded = game.observe()
        assert reloaded["inventory"] == before_save["inventory"], (reloaded, before_save)
        assert reloaded["iron_plates_produced"] == before_save["iron_plates_produced"]
        assert len(reloaded["buildings"]) >= 2
        evidence["reload"] = {"inventory_preserved": True, "production_preserved": True,
                              "buildings_preserved": True, "pause_tick_frozen": True}
        evidence["status"] = "Passed"
    except Exception as error:
        evidence["status"] = "Failed"
        evidence["error"] = str(error)
        raise
    finally:
        game.close()
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "evidence.json").write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8")
    print("REAL_ENGINE_SMOKE_PASSED", flush=True)


if __name__ == "__main__":
    main()
