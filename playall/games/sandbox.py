"""An explicitly labelled practice game; never a Factorio substitute in evidence."""
from __future__ import annotations

import json
import threading
from contextlib import nullcontext
from pathlib import Path

from .actions import validate_action


class Sandbox:
    id = "sandbox"
    label = "采矿练习场"

    def __init__(self, directory, settings, emit):
        self.path = Path(directory) / "sandbox.json"
        self.settings, self.emit = settings, emit
        self.lock = threading.RLock()
        self.state = {}

    def prepare(self, control, new_world=False):
        control.check()
        if self.path.exists() and not new_world:
            self.state = json.loads(self.path.read_text(encoding="utf-8"))
        else:
            self.state = {"ok": True, "game": "sandbox", "source": "独立练习场（非 Factorio）", "tick": 0,
                          "position": {"x": 0, "y": 0}, "health": 250, "inventory": {"iron-plate": 8, "stone-furnace": 1, "burner-mining-drill": 1, "wood": 1},
                          "iron_plates_produced": 0, "buildings": [], "resources": [
                              {"name": "iron-ore", "x": 14, "y": 8, "amount": 2000},
                              {"name": "coal", "x": -16, "y": 12, "amount": 1500},
                              {"name": "stone", "x": 10, "y": -12, "amount": 1000},
                              {"name": "copper-ore", "x": -20, "y": -18, "amount": 1800}],
                          "technologies": [], "busy": False}
        self.emit("system", "已进入独立采矿练习场。这里不是 Factorio。")

    def observe(self):
        with self.lock:
            return json.loads(json.dumps(self.state))

    def execute(self, action, control):
        a = validate_action(action)
        control.check()
        control.sleep(0.2)
        with control.action() if hasattr(control, "action") else nullcontext(), self.lock:
            inv = self.state["inventory"]
            kind, name, count = a["action"], a.get("name", ""), int(a.get("count", 1))
            target = next((b for b in self.state["buildings"] if abs(b["x"] - a.get("x", 1e6)) < 1 and abs(b["y"] - a.get("y", 1e6)) < 1), None)
            if kind == "observe":
                return self.observe()
            if kind == "move":
                self.state["position"] = {"x": a["x"], "y": a["y"]}
            elif kind == "mine":
                resource = next((r for r in self.state["resources"] if r["name"] == name), None)
                if not resource:
                    return {"ok": False, "error": "练习场没有这种资源。"}
                taken = min(count, resource["amount"])
                resource["amount"] -= taken
                inv[name] = inv.get(name, 0) + taken
                self.state["position"] = {"x": resource["x"], "y": resource["y"]}
            elif kind == "build":
                if inv.get(name, 0) < 1:
                    return {"ok": False, "error": "背包里没有这个建筑。"}
                if target:
                    return {"ok": False, "error": "这个位置已经有建筑。"}
                inv[name] -= 1
                self.state["buildings"].append({"name": name, "x": a["x"], "y": a["y"], "input": {}, "fuel": {}, "output": {}, "status": "waiting"})
            elif kind in ("insert", "extract"):
                if not target:
                    return {"ok": False, "error": "该位置没有建筑。"}
                compartment = a.get("inventory", "fuel" if name == "coal" else "input")
                store = target.setdefault(compartment, {})
                source, destination = (inv, store) if kind == "insert" else (store, inv)
                taken = min(count, source.get(name, 0))
                if taken == 0:
                    return {"ok": False, "error": "没有可以转移的物品。"}
                source[name] -= taken
                destination[name] = destination.get(name, 0) + taken
            elif kind == "wait":
                for building in self.state["buildings"]:
                    if building["name"] != "stone-furnace":
                        continue
                    ore, fuel = building["input"].get("iron-ore", 0), building["fuel"].get("coal", 0)
                    produced = min(ore, fuel * 4, int(a.get("seconds", 5) / 3.2))
                    if produced:
                        building["input"]["iron-ore"] -= produced
                        building["fuel"]["coal"] -= (produced + 3) // 4
                        building["output"]["iron-plate"] = building["output"].get("iron-plate", 0) + produced
                        self.state["iron_plates_produced"] += produced
                        building["status"] = "working"
            elif kind == "inspect":
                return {"ok": True, "entity": target} if target else {"ok": False, "error": "没有找到建筑。"}
            else:
                return {"ok": False, "error": "练习场仅支持移动、开采、建造、物品转移与冶炼；完整功能请选择 Factorio。"}
            self.state["tick"] += int(a.get("seconds", 1) * 60)
            return {"ok": True, "message": "练习动作完成。"}

    def pause(self):
        pass

    def resume(self):
        pass

    def cancel(self):
        pass

    def close(self):
        self.checkpoint()

    def checkpoint(self):
        if self.state:
            from ..storage import atomic_json
            with self.lock:
                atomic_json(self.path, self.state)

    def watch(self):
        raise ValueError("这是独立的控制链路练习场，没有游戏客户端。请选择 Factorio 打开真实游戏。")
