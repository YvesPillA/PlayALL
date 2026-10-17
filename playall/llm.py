from __future__ import annotations

import json
import math
import urllib.error
import urllib.request
from urllib.parse import urlparse


class ProviderError(RuntimeError):
    pass


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class CompatibleAI:
    def __init__(self, profile):
        self.profile = profile
        self.opener = urllib.request.build_opener(NoRedirect)

    def _request(self, path, payload=None):
        url = self.profile["base_url"].rstrip("/") + path
        headers = {"Content-Type": "application/json", "Accept": "application/json", "User-Agent": "PlayALL/0.1"}
        if self.profile.get("api_key"):
            headers["Authorization"] = "Bearer " + self.profile["api_key"]
        request = urllib.request.Request(url, data=json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8") if payload is not None else None, headers=headers)
        try:
            with self.opener.open(request, timeout=45) as response:
                raw = response.read(4 * 1024 * 1024 + 1)
                if len(raw) > 4 * 1024 * 1024:
                    raise ProviderError("AI 返回内容过大，已停止本轮。")
                data = json.loads(raw)
                if not isinstance(data, dict):
                    raise ProviderError("AI 接口返回的内容格式不正确。")
                return data
        except urllib.error.HTTPError as error:
            error.close()
            messages = {400: "AI 拒绝了请求，请检查模型名和工具调用支持。", 401: "API Key 无效，请在设置中更换。",
                        402: "AI 账户余额不足，请充值或切换免费演练。", 403: "此 API Key 没有调用权限。",
                        404: "找不到接口或模型，请检查接口地址和模型名。", 429: "AI 服务限流，请稍后继续这局。"}
            raise ProviderError(messages.get(error.code, f"AI 服务返回 HTTP {error.code}，本局已安全停止。")) from None
        except (urllib.error.URLError, TimeoutError, OSError):
            raise ProviderError("连接 AI 超时或网络不可用。已停止新请求，可稍后继续这局。") from None
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise ProviderError("AI 接口没有返回有效 JSON，请检查接口地址。") from None

    def probe(self):
        data = self._request("/models")
        entries = data.get("data", [])
        if not isinstance(entries, list):
            raise ProviderError("AI 接口返回的模型列表格式不正确。")
        models = [item["id"] for item in entries if isinstance(item, dict) and isinstance(item.get("id"), str)]
        if not models:
            raise ProviderError("接口没有返回模型列表；可以手动填写支持工具调用的模型。")
        current = self.profile["model"]
        return {"ok": current in models, "models": models[:100],
                "message": "连接成功，模型可用；此检查不生成内容。" if current in models else "已连接，但当前模型不在列表中。请选一个可用模型。"}

    def chat(self, messages, tools, output_tokens):
        payload = {"model": self.profile["model"], "messages": messages, "tools": tools,
                   "tool_choice": "auto", "max_tokens": output_tokens, "stream": False}
        if urlparse(self.profile["base_url"]).hostname == "api.deepseek.com":
            payload["thinking"] = {"type": "disabled"}
        return self._request("/chat/completions", payload)


class RehearsalAI:
    """State-based fixed policy, conspicuously labelled as a script in the UI."""
    def __init__(self, profile):
        self.profile = profile
        self.build_attempt = 0

    def probe(self):
        return {"ok": True, "models": ["offline"], "message": "免费演练不连接 AI；使用固定采矿冶炼脚本。"}

    def decide(self, state):
        inv = state.get("inventory", {})
        furnaces = [b for b in state.get("buildings", []) if b["name"] == "stone-furnace"]
        if not furnaces:
            if inv.get("iron-ore", 0) < 12:
                return "game_action", {"action": "mine", "name": "iron-ore", "count": 12}, "先找到铁矿，采够一炉的原料。"
            if inv.get("coal", 0) < 8:
                return "game_action", {"action": "mine", "name": "coal", "count": 8}, "采煤，为石炉准备燃料。"
            if inv.get("stone-furnace", 0) < 1:
                if inv.get("stone", 0) < 5:
                    return "game_action", {"action": "mine", "name": "stone", "count": 5}, "收集石头，制作新的石炉。"
                return "game_action", {"action": "craft", "name": "stone-furnace", "count": 1}, "用石头制作石炉。"
            positions = [(4, 0), (-4, 0), (0, 4), (0, -4), (8, 0), (0, 8), (-8, 0), (0, -8)]
            dx, dy = positions[self.build_attempt % len(positions)]
            position = state.get("position", {})
            x, y = math.floor(position.get("x", 0)) + dx + .5, math.floor(position.get("y", 0)) + dy + .5
            self.build_attempt += 1
            return "game_action", {"action": "build", "name": "stone-furnace", "x": x, "y": y}, "找一块空地，放下石炉。"
        furnace = furnaces[0]
        coordinates = {"x": furnace["x"], "y": furnace["y"]}
        if furnace.get("output", {}).get("iron-plate", 0) > 0:
            return "game_action", {"action": "extract", "name": "iron-plate", "count": min(100, furnace["output"]["iron-plate"]), "inventory": "output", **coordinates}, "从石炉取回已经炼好的铁板，然后继续生产。"
        if furnace.get("fuel", {}).get("coal", 0) == 0:
            if inv.get("coal", 0) < 8:
                return "game_action", {"action": "mine", "name": "coal", "count": 8}, "补充燃料。"
            return "game_action", {"action": "insert", "name": "coal", "count": 8, "inventory": "fuel", **coordinates}, "给石炉加煤。"
        if furnace.get("input", {}).get("iron-ore", 0) < 1:
            if inv.get("iron-ore", 0) < 12:
                return "game_action", {"action": "mine", "name": "iron-ore", "count": 12}, "为石炉补充铁矿石。"
            return "game_action", {"action": "insert", "name": "iron-ore", "count": 12, "inventory": "input", **coordinates}, "把铁矿石放进炉子。"
        return "game_action", {"action": "wait", "seconds": 45}, "等待游戏里的石炉自然冶炼。"

    def chat(self, messages, tools, output_tokens):
        if not any(tool.get("function", {}).get("name") == "game_action" for tool in tools):
            return {"choices": [{"message": {"role": "assistant", "content":
                "免费演练使用固定脚本，无法理解自由指令或回答问题。操作确认和暂停仍有效；选择真实 AI 后即可交流。"}}],
                "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}}
        latest = json.loads(messages[-1]["content"])
        observation = latest["observation"]
        actions = next((tool["function"]["parameters"]["properties"].get("action", {}).get("enum", [])
                        for tool in tools if tool["function"]["name"] == "game_action"), [])
        if "advance" in actions and isinstance(observation.get("controls"), list):
            enabled = [control for control in observation["controls"] if control.get("enabled")]
            next_line = next((control for control in enabled if control.get("name") == "Btn_NextLine"), None)
            option = next((control for control in enabled if control.get("click") and "option" in control.get("path", "").lower()), None)
            name, arguments, content = "game_action", {"action": "wait", "seconds": 2}, "免费演练：等待游戏内容就绪。"
            if next_line:
                arguments, content = {"action": "advance", "view_revision": observation["view_revision"]}, "免费演练：通过游戏原有继续按钮推进。"
            elif option:
                arguments, content = {"action": "click", "control_id": option["id"], "view_revision": observation["view_revision"]}, "免费演练：选择当前第一个可用选项。"
        else:
            name, arguments, content = self.decide(observation)
        return {"choices": [{"message": {"role": "assistant", "content": content,
            "tool_calls": [{"id": f"rehearsal_{latest['turn']}", "type": "function", "function": {"name": name, "arguments": json.dumps(arguments, ensure_ascii=False)}}]}}],
            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}}


def make_ai(profile):
    return RehearsalAI(profile) if profile["id"] == "rehearsal" else CompatibleAI(profile)
