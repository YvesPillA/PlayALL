from __future__ import annotations

import math

ACTIONS = ("observe", "move", "mine", "craft", "build", "insert", "extract", "set_recipe",
           "research", "wait", "inspect", "recipes", "rotate", "deconstruct")


def validate_action(action: dict) -> dict:
    if not isinstance(action, dict) or action.get("action") not in ACTIONS:
        raise ValueError("请选择有效的游戏动作。")
    allowed = {"action", "name", "x", "y", "count", "seconds", "direction", "inventory"}
    if set(action) - allowed:
        raise ValueError("动作包含未知参数。")
    for key in ("name", "inventory"):
        if key in action and (not isinstance(action[key], str) or len(action[key]) > 100):
            raise ValueError(f"{key} 格式不正确。")
    for key in ("x", "y", "seconds", "count"):
        if key in action:
            val = action[key]
            if isinstance(val, bool) or not isinstance(val, (int, float)) or not math.isfinite(val):
                raise ValueError(f"{key} 必须是有限数字。")
    for key in ("x", "y"):
        if key in action and abs(action[key]) > 100000:
            raise ValueError("目的地超出范围。")
    if "count" in action and (not 1 <= action["count"] <= 100 or int(action["count"]) != action["count"]):
        raise ValueError("每次数量需要是 1 到 100 的整数。")
    if "seconds" in action and not 0.1 <= action["seconds"] <= 120:
        raise ValueError("每次等待需要介于 0.1 和 120 游戏秒。")
    if "direction" in action and action["direction"] not in ("north", "east", "south", "west"):
        raise ValueError("方向需要是 north / east / south / west。")
    if action["action"] in ("move", "build") and not {"x", "y"} <= set(action):
        raise ValueError("这个动作需要 x 和 y 坐标。")
    if action["action"] in ("mine", "craft", "build", "insert", "extract", "set_recipe", "research") and not action.get("name"):
        raise ValueError("这个动作需要 name。")
    return dict(action)


def tools_for_game():
    return [
        {"type": "function", "function": {
            "name": "game_action",
            "description": "Operate the game. Long actions automatically walk into reach and finish before returning. Use prototype names from observations/recipes, never invent materials. inspect at x,y shows a machine; inspect with name shows a recipe and, when available, technology conditions. A technology with trigger unlocks automatically by completing that condition, not by queuing research. mine name targets nearest resource/tree, count is mining cycles. insert/extract name is an ITEM and x,y identifies the machine. build name is an ITEM, direction is the game's entity orientation; inspect pickup_position/drop_position for inserters and drills. craft count is recipe cycles. recipes name optionally filters. wait seconds are GAME seconds. Only one game action per call is recommended.",
            "parameters": {"type": "object", "properties": {
                "action": {"type": "string", "enum": list(ACTIONS)},
                "name": {"type": "string"}, "x": {"type": "number"}, "y": {"type": "number"},
                "count": {"type": "integer", "minimum": 1, "maximum": 100},
                "seconds": {"type": "number", "minimum": 0.1, "maximum": 120},
                "direction": {"type": "string", "enum": ["north", "east", "south", "west"]},
                "inventory": {"type": "string", "enum": ["fuel", "input", "output", "chest", "lab", "ammo", "modules"]},
            }, "required": ["action"], "additionalProperties": False},
        }},
        {"type": "function", "function": {"name": "report_progress", "description": "Remember a milestone, durable plan or lesson from observed evidence. This records progress and continues playing. It never ends the session.",
             "parameters": {"type": "object", "properties": {"summary": {"type": "string"}}, "required": ["summary"], "additionalProperties": False}}},
    ]
