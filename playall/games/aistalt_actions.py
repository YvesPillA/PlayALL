"""Only existing, observed game controls are exposed to the model."""
import math


def validate_action(value):
    if not isinstance(value, dict):
        raise ValueError("游戏动作必须是对象。")
    action = value.get("action")
    fields = {
        "observe": set(), "wait": {"seconds"}, "advance": {"view_revision"},
        "click": {"control_id", "view_revision"},
        "text": {"control_id", "view_revision", "text", "submit"},
        "scroll": {"control_id", "view_revision", "axis", "amount"},
        "set_value": {"control_id", "view_revision", "value"},
    }
    if not isinstance(action, str) or action not in fields or set(value) - {"action"} - fields[action]:
        raise ValueError("只能使用幼神已安装的游戏动作和字段。")
    if action == "observe":
        return {"action": action}
    if action == "wait":
        seconds = value.get("seconds")
        if isinstance(seconds, bool) or not isinstance(seconds, (int, float)) or not math.isfinite(seconds) or not 0 < seconds <= 30:
            raise ValueError("单次等待需要介于 0 和 30 秒之间。")
        return dict(value)
    revision = value.get("view_revision")
    if isinstance(revision, bool) or not isinstance(revision, int) or revision < 0:
        raise ValueError("请使用最新观察中的 view_revision。")
    if action != "advance":
        control = value.get("control_id")
        if not isinstance(control, str) or not 1 <= len(control) <= 128:
            raise ValueError("请使用当前观察中的控件 ID。")
    if action == "text":
        if not isinstance(value.get("text"), str) or len(value["text"]) > 8000:
            raise ValueError("请输入不超过 8000 字的游戏文本。")
        if "submit" in value and not isinstance(value["submit"], bool):
            raise ValueError("submit 必须是布尔值。")
    if action in ("scroll", "set_value"):
        key = "amount" if action == "scroll" else "value"
        number = value.get(key)
        if isinstance(number, bool) or not isinstance(number, (int, float)) or not math.isfinite(number):
            raise ValueError("请提供有限数值。")
        if action == "scroll" and (value.get("axis") not in ("x", "y") or not -1 <= number <= 1 or number == 0):
            raise ValueError("滚动方向为 x/y，幅度介于 -1 和 1 之间且不能为 0。")
    return dict(value)


def tools_for_game():
    parameters = {"type": "object", "properties": {
        "action": {"type": "string", "enum": ["observe", "wait", "advance", "click", "text", "scroll", "set_value"]},
        "seconds": {"type": "number", "description": "Wait for native loading/animation, greater than 0 and at most 30 seconds per call."},
        "control_id": {"type": "string", "description": "Exact id of an enabled control from the current observation."},
        "view_revision": {"type": "integer", "description": "view_revision from the latest observation; required for every mutation."},
        "text": {"type": "string"}, "submit": {"type": "boolean"},
        "axis": {"type": "string", "enum": ["x", "y"]},
        "amount": {"type": "number", "description": "Scroll fraction, -1 to 1."},
        "value": {"type": "number", "description": "A slider value within its observed min/max."},
    }, "required": ["action"], "additionalProperties": False}
    return [
        {"type": "function", "function": {"name": "game_action", "description":
            "Read or operate real Aistalt game controls. Use one mutating action at a time, then inspect the result. "
            "Native events preserve dialogue conditions, dice checks, costs and inventory rules.", "parameters": parameters}},
        {"type": "function", "function": {"name": "report_progress", "description":
            "Remember story, choices, plans and lessons, then keep playing.", "parameters": {
                "type": "object", "properties": {"summary": {"type": "string"}}, "required": ["summary"],
                "additionalProperties": False}}},
    ]
