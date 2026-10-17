from __future__ import annotations

import copy
import ctypes
import json
import os
import re
import sys
import threading
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent))
APP_HOME = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else ROOT
DEFAULTS = {
    "provider": "deepseek",
    "game": "factorio",
    "system_prompt": "自主游玩，观察环境并制定长期计划。按照游戏规则，从基础建设起步，逐步探索、发展和解锁新内容。完成一个阶段后自行安排下一步，遇到失败先检查原因并换办法。用简短中文说明当前打算，直到我按暂停。",
    "profiles": [
        {"id": "deepseek", "label": "DeepSeek", "base_url": "https://api.deepseek.com",
         "model": "deepseek-flash", "input_price": 0.3, "output_price": 1.2},
        {"id": "custom", "label": "其他兼容 AI", "base_url": "https://api.deepseek.com",
         "model": "deepseek-flash", "input_price": 0.3, "output_price": 1.2},
        {"id": "rehearsal", "label": "免费演练（固定脚本）", "base_url": "", "model": "offline",
         "input_price": 0, "output_price": 0},
    ],
    "factorio_path": "",
    "aistalt_path": "",
    "speed": 1,
    "output_tokens": 1024,
    "step_delay": 0.5,
}


def protect(value: str) -> str:
    if not value:
        return ""
    if os.name != "nt":
        raise ValueError("此版本只在 Windows 保存密钥；其他系统请使用环境变量。")
    from ctypes import wintypes
    class Blob(ctypes.Structure):
        _fields_ = [("size", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_ubyte))]
    raw = value.encode("utf-8")
    buffer = (ctypes.c_ubyte * len(raw)).from_buffer_copy(raw)
    source, target = Blob(len(raw), buffer), Blob()
    if not ctypes.windll.crypt32.CryptProtectData(ctypes.byref(source), "PlayALL", None, None, None, 1, ctypes.byref(target)):
        raise OSError("Windows 无法加密密钥。")
    try:
        import base64
        return base64.b64encode(ctypes.string_at(target.data, target.size)).decode("ascii")
    finally:
        ctypes.windll.kernel32.LocalFree(target.data)


def unprotect(value: str) -> str:
    if not value:
        return ""
    import base64
    from ctypes import wintypes
    class Blob(ctypes.Structure):
        _fields_ = [("size", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_ubyte))]
    raw = base64.b64decode(value)
    buffer = (ctypes.c_ubyte * len(raw)).from_buffer_copy(raw)
    source, target = Blob(len(raw), buffer), Blob()
    if not ctypes.windll.crypt32.CryptUnprotectData(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(target)):
        raise ValueError("密钥无法解密，请在设置中重新填写。")
    try:
        return ctypes.string_at(target.data, target.size).decode("utf-8")
    finally:
        ctypes.windll.kernel32.LocalFree(target.data)


def validate_endpoint(value: str) -> str:
    url = urlparse(value.rstrip("/"))
    try:
        url.port
    except ValueError:
        raise ValueError("接口端口需要是 1 到 65535 之间的数字。") from None
    if url.port == 0:
        raise ValueError("接口端口需要是 1 到 65535 之间的数字。")
    if url.username or url.password or url.query or url.fragment:
        raise ValueError("接口地址不能含账户、查询参数或片段。")
    if url.scheme != "https" and not (url.scheme == "http" and url.hostname in ("localhost", "127.0.0.1", "::1")):
        raise ValueError("接口需要 HTTPS；本机模型可用 http://127.0.0.1。")
    if not url.hostname:
        raise ValueError("请输入完整的 AI 接口地址。")
    return value.rstrip("/")


def steam_libraries() -> list[Path]:
    default = Path(os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)")) / "Steam"
    steam = Path(os.environ.get("STEAM_PATH") or default)
    libraries = [steam]
    vdf = steam / "steamapps/libraryfolders.vdf"
    try:
        libraries.extend(Path(p.replace("\\\\", "\\")) for p in re.findall(r'"path"\s+"([^"]+)"', vdf.read_text(encoding="utf-8")))
    except OSError:
        pass
    return list(dict.fromkeys(libraries))


def detect_factorio() -> str:
    candidates = [Path(os.environ["FACTORIO_PATH"])] if os.environ.get("FACTORIO_PATH") else []
    for lib in steam_libraries():
        candidates.append(lib / "steamapps/common/Factorio/bin/x64/factorio.exe")
    candidates.extend([Path(os.environ.get("PROGRAMFILES", r"C:\Program Files")) / "Factorio/bin/x64/factorio.exe",
                       ROOT / "factorio/bin/x64/factorio.exe"])
    return next((str(p) for p in candidates if p.is_file()), "")


def detect_aistalt() -> str:
    candidates = [Path(os.environ["AISTALT_PATH"])] if os.environ.get("AISTALT_PATH") else []
    candidates.extend(lib / "steamapps/common/幼神 Demo/Build/Aistalt.exe" for lib in steam_libraries())
    return next((str(path) for path in candidates if path.is_file()), "")


class Settings:
    def __init__(self, directory: Path | None = None):
        self.directory = directory or APP_HOME / "data"
        self.directory.mkdir(parents=True, exist_ok=True)
        self.path = self.directory / "settings.json"
        self.lock = threading.RLock()
        self.data = copy.deepcopy(DEFAULTS)
        if self.path.exists():
            loaded = json.loads(self.path.read_text(encoding="utf-8"))
            self.data.update({k: v for k, v in loaded.items() if k in DEFAULTS})
        if not self.data["factorio_path"]:
            self.data["factorio_path"] = detect_factorio()
        if not self.data["aistalt_path"]:
            self.data["aistalt_path"] = detect_aistalt()

    def snapshot(self) -> dict:
        with self.lock:
            return copy.deepcopy(self.data)

    def public(self) -> dict:
        data = self.snapshot()
        for profile in data["profiles"]:
            profile["has_key"] = bool(profile.pop("secret", "") or self.environment_key(profile["id"]))
        data["factorio_detected"] = Path(data["factorio_path"]).is_file() if data["factorio_path"] else False
        return data

    @staticmethod
    def environment_key(profile_id: str) -> str:
        return os.environ.get("DEEPSEEK_API_KEY" if profile_id == "deepseek" else "PLAYALL_API_KEY", "")

    def profile(self, profile_id: str) -> dict:
        item = next((p for p in self.snapshot()["profiles"] if p["id"] == profile_id), None)
        if item is None:
            raise ValueError("请选择有效的 AI。")
        item["api_key"] = self.environment_key(profile_id) or unprotect(item.pop("secret", ""))
        return item

    def update(self, changes: dict) -> dict:
        with self.lock:
            updated = self.snapshot()
            for key in ("provider", "game", "system_prompt", "factorio_path", "aistalt_path"):
                if key in changes:
                    if not isinstance(changes[key], str) or len(changes[key]) > (20000 if key == "system_prompt" else 1024):
                        raise ValueError("设置内容过长或格式不正确。")
                    updated[key] = changes[key].strip()
            from .games.registry import game_specs
            if updated["provider"] not in ("deepseek", "custom", "rehearsal") or updated["game"] not in game_specs():
                raise ValueError("AI 或游戏不存在。")
            if not updated["system_prompt"]:
                raise ValueError("请填写总提示词。")
            for key, low, high, cast in [("speed", 1, 8, float),
                                       ("output_tokens", 256, 4096, int), ("step_delay", 0, 30, float)]:
                if key in changes:
                    val = cast(changes[key])
                    if not low <= val <= high:
                        raise ValueError(f"{key} 需要介于 {low} 和 {high}。")
                    updated[key] = val
            if "profiles" in changes:
                if not isinstance(changes["profiles"], list):
                    raise ValueError("AI 配置格式不正确。")
                for patch in changes["profiles"]:
                    if not isinstance(patch, dict):
                        raise ValueError("AI 配置格式不正确。")
                    item = next((p for p in updated["profiles"] if p["id"] == patch.get("id") and p["id"] != "rehearsal"), None)
                    if item is None:
                        raise ValueError("无法修改此 AI。")
                    if "base_url" in patch:
                        item["base_url"] = validate_endpoint(patch["base_url"])
                    for key in ("model", "label"):
                        if key in patch:
                            if not isinstance(patch[key], str) or not 1 <= len(patch[key]) <= 120:
                                raise ValueError("请输入模型名称与显示名称。")
                            item[key] = patch[key].strip()
                    for key in ("input_price", "output_price"):
                        if key in patch:
                            val = float(patch[key])
                            if not 0 <= val <= 1000:
                                raise ValueError("价格需介于 0 和 1000 美元 / 百万 token。")
                            item[key] = val
                    if patch.get("clear_key"):
                        item.pop("secret", None)
                    if patch.get("api_key"):
                        key = patch["api_key"].strip()
                        if len(key) > 2048:
                            raise ValueError("密钥过长。")
                        item["secret"] = protect(key)
            temporary = self.path.with_suffix(".tmp")
            temporary.write_text(json.dumps(updated, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(temporary, self.path)
            self.data = updated
            return self.public()
