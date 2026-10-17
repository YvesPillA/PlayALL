"""Factorio's built-in local RCON, attached to one user-selected process.

No launcher, server, player, save loading, OS input or executable patching.
The older dedicated-server adapter is retained only for historical tools.
"""
from __future__ import annotations

import configparser
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import socket
import threading
import time
import uuid
import zipfile
from contextlib import nullcontext

from ..config import ROOT
from ..processes import list_processes, tcp_listener_pid, validate_selection
from ..storage import atomic_json
from .actions import validate_action
from .rcon import Rcon


BRIDGE_VERSION = "0.2.2"
MOD_NAME = "playall-bridge_" + BRIDGE_VERSION
BUNDLE = ROOT / "mods" / MOD_NAME
FILES = ("control.lua", "info.json", "build.json")


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def is_supported_process(process):
    try:
        exe = Path(process.path)
        build = json.loads((BUNDLE / "build.json").read_text(encoding="utf-8"))
        return (exe.name.casefold() == "factorio.exe" and exe.is_file()
                and digest(exe) == build["executable_sha256"])
    except (OSError, ValueError, KeyError, TypeError):
        return False


def ini_values(data):
    parser = configparser.ConfigParser(interpolation=None, strict=True)
    try:
        parser.read_string(data.decode("utf-8-sig"))
    except (UnicodeError, configparser.Error) as error:
        raise ValueError("Factorio 配置无法安全读取，已保留原文件。") from error
    return parser


def update_ini(data, values):
    """Preserve comments, BOM, line endings and every unrelated setting."""
    ini_values(data)
    text = data.decode("utf-8-sig")
    newline = "\r\n" if "\r\n" in text else "\n"
    lines, section, seen = text.splitlines(keepends=True), None, set()
    for index, line in enumerate(lines):
        body = line.rstrip("\r\n")
        header = re.fullmatch(r"\s*\[([^\]]+)\]\s*", body)
        if header:
            section = header.group(1)
        elif section == "other" and not body.lstrip().startswith((";", "#")) and "=" in body:
            key, old = body.split("=", 1)
            if key.strip() in values:
                seen.add(key.strip())
                lines[index] = key + "=" + values[key.strip()] + line[len(body):]
    missing = {k: v for k, v in values.items() if k not in seen}
    if missing:
        # Inserting immediately after the section header also works when the
        # section is followed by comments or by another section.
        location = next((i + 1 for i, line in enumerate(lines) if line.strip() == "[other]"), None)
        addition = [f"{k}={v}{newline}" for k, v in missing.items()]
        if location is None:
            if lines and not lines[-1].endswith(("\r", "\n")):
                lines[-1] += newline
            lines.extend([f"[other]{newline}", *addition])
        else:
            if not lines[location - 1].endswith(("\r", "\n")):
                lines[location - 1] += newline
            lines[location:location] = addition
    return (b"\xef\xbb\xbf" if data.startswith(b"\xef\xbb\xbf") else b"") + "".join(lines).encode("utf-8")


def profile_paths(exe):
    """Resolve the documented default config-path, never inspect command lines."""
    exe = Path(exe).resolve()
    installation = exe.parents[2]
    settings = {}
    for line in (installation / "config-path.cfg").read_text(encoding="utf-8-sig").splitlines():
        if "=" in line and not line.lstrip().startswith(("#", ";")):
            key, value = line.split("=", 1)
            settings[key.strip()] = value.strip()
    system = Path(os.environ.get("APPDATA", str(Path.home() / "AppData/Roaming"))) / "Factorio"
    default_data = system if settings.get("use-system-read-write-data-directories", "false").lower() == "true" else installation

    def expand(value):
        value = value.replace("__PATH__system-write-data__", str(default_data)).replace("__PATH__executable__", str(exe.parent))
        if "__PATH__" in value:
            raise ValueError("这个 Factorio 自定义数据路径尚未适配，已保留原配置。")
        path = Path(value)
        return path.resolve() if path.is_absolute() else (installation / path).resolve()

    config = expand(settings.get("config-path", "__PATH__system-write-data__/config")) / "config.ini"
    parser = ini_values(config.read_bytes() if config.exists() else b"")
    data = expand(parser.get("path", "write-data", fallback=str(default_data)))
    return config, data


def local_endpoint(parser):
    value = parser.get("other", "local-rcon-socket", fallback="0.0.0.0:0")
    password = parser.get("other", "local-rcon-password", fallback="")
    match = re.fullmatch(r"127\.0\.0\.1:([0-9]{1,5})", value)
    if not match or not 1 <= int(match.group(1)) <= 65535 or not password or "\n" in password or "\r" in password:
        raise ValueError("本机连接组件尚未准备好。请关闭 Factorio，在设置中点「准备连接组件」，再亲自打开游戏。")
    return int(match.group(1)), password


def atomic_bytes(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        temporary.write_bytes(data)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def prepare_connection(directory, exe, emit):
    """Explicit setup only, while closed; transactional and backed up."""
    exe = Path(exe)
    identity = type("Identity", (), {"path": str(exe)})()
    if not is_supported_process(identity):
        raise ValueError("当前组件只验证了 Factorio 2.0.77 Steam Windows 版，请核对游戏文件。")
    if any(p.name.casefold() == "factorio.exe" for p in list_processes()):
        raise ValueError("请先在游戏里保存并关闭 Factorio，再准备连接组件。")
    config, data = profile_paths(exe)
    original = config.read_bytes() if config.exists() else b""
    parser = ini_values(original)
    socket_value = parser.get("other", "local-rcon-socket", fallback="0.0.0.0:0")
    if socket_value in ("", "0.0.0.0:0", "127.0.0.1:0"):
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        updated = update_ini(original, {"local-rcon-socket": f"127.0.0.1:{port}",
                                        "local-rcon-password": secrets.token_urlsafe(32)})
    else:
        # An existing valid local service can be shared; never replace an
        # unknown network binding or a user's authentication configuration.
        local_endpoint(parser)
        updated = original
    mod_list = data / "mods/mod-list.json"
    before_list = mod_list.read_bytes() if mod_list.exists() else None
    mods = json.loads(before_list.decode("utf-8-sig")) if before_list is not None else {"mods": [{"name": "base", "enabled": True}]}
    if (not isinstance(mods, dict) or not isinstance(mods.get("mods"), list)
            or any(not isinstance(m, dict) or not isinstance(m.get("name"), str) for m in mods["mods"])):
        raise ValueError("模组列表格式无效，已保留原文件。")
    entries = [m for m in mods["mods"] if m["name"] == "playall-bridge"]
    if len(entries) > 1:
        raise ValueError("连接模组有重复条目，已保留原文件。")
    if entries:
        entries[0]["enabled"] = True
    else:
        mods["mods"].append({"name": "playall-bridge", "enabled": True})
    target = data / "mods" / MOD_NAME
    manifest_path = data / "playall/installation.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.exists() else {}
    owned = manifest.get("files", {})
    changes = {config: updated, mod_list: json.dumps(mods, ensure_ascii=False, indent=2).encode("utf-8")}
    for name in FILES:
        destination, source = target / name, BUNDLE / name
        if destination.exists() and digest(destination) not in (digest(source), owned.get(name)):
            raise ValueError("游戏里有未知版本的 PlayALL 模组，已保留文件，请先核对。")
        changes[destination] = source.read_bytes()
    changes[manifest_path] = json.dumps({"protocol": 2, "version": BRIDGE_VERSION,
        "files": {name: digest(BUNDLE / name) for name in FILES}}, indent=2).encode("utf-8")
    originals = {path: path.read_bytes() if path.exists() else None for path in changes}
    backup = data / "playall/backups" / str(time.time_ns())
    for name, path in (("config.ini", config), ("mod-list.json", mod_list)):
        if originals[path] is not None and originals[path] != changes[path]:
            atomic_bytes(backup / name, originals[path])
    try:
        for path, content in changes.items():
            atomic_bytes(path, content)
    except Exception:
        for path, content in originals.items():
            if content is None:
                path.unlink(missing_ok=True)
            else:
                atomic_bytes(path, content)
        raise
    emit("diagnostic", "Factorio 本机模组及连接设置已准备，原配置已备份。")
    return {"ok": True, "message": "组件已准备好。请亲自打开 Factorio，进入单人存档，再选择进程连接；不会自动开游戏或新建地图。"}


class Factorio:
    id = "factorio"
    label = "Factorio"

    def __init__(self, directory, settings, emit):
        self.directory = Path(directory) / "factorio"
        self.settings, self.emit = settings, emit
        self.selected_process = settings.get("game_process")
        self.exe = Path(self.selected_process.get("path", "") if isinstance(self.selected_process, dict) else "")
        self.lock = threading.RLock()
        self.rcon = None
        self.session = uuid.uuid4().hex
        self.heartbeat_stop = threading.Event()
        self.heartbeat_thread = None
        self.fault = None
        self.paused = True
        self.mutated = False
        self.progress_signature = None
        self.saved_signature = None
        self.last_checkpoint = time.monotonic()
        self.save_slot = 0
        self.data = None
        self.port = None

    def prepare(self, control, new_world=False):
        control.check()
        if new_world:
            raise ValueError("请亲自在游戏中新建或载入地图；连接不会改变存档。")
        validate_selection(self.selected_process, "factorio.exe")
        if not is_supported_process(type("Identity", (), {"path": str(self.exe)})()):
            raise ValueError("所选 Factorio 版本尚未验证。")
        config, self.data = profile_paths(self.exe)
        self.port, self.password = local_endpoint(ini_values(config.read_bytes()))
        for name in FILES:
            installed = self.data / "mods" / MOD_NAME / name
            if not installed.is_file() or digest(installed) != digest(BUNDLE / name):
                raise ValueError("Factorio 连接模组缺失或需要更新。请先关闭游戏，在设置里准备连接组件。")
        self._identity()
        self.emit("diagnostic", "Factorio 已核对所选进程与本机接口，不启动游戏或请求模型。")

    def _identity(self):
        validate_selection(self.selected_process, "factorio.exe")
        if self.fault:
            raise RuntimeError(self.fault)
        owner = tcp_listener_pid(self.port)
        if owner is None:
            raise ValueError("本机接口尚未开启。请先在 Factorio 进入单人地图，等待加载完成，再点击连接；主菜单还不能连接。若已进入地图，请关闭游戏后在设置中重新准备连接组件。")
        if owner != self.selected_process["pid"]:
            raise ValueError("这个本机接口不属于所选 Factorio 进程。请核对组件并重新选择，连接不会启动其他实例。")

    def connect_window(self, control):
        control.check()
        self._identity()
        if self.rcon:
            if not self.window_ready():
                raise ValueError("请先回到已经连接的单人地图。")
            return {"ok": True}
        self.rcon = Rcon("127.0.0.1", self.port, self.password, timeout=5)
        try:
            state = self._remote("hello")
            if state.get("bridge") != BRIDGE_VERSION or state.get("protocol") != 2 or state.get("version") != "2.0.77":
                raise ValueError("游戏内的连接组件版本不匹配，请关闭游戏后重新准备。")
            if not state.get("ready"):
                raise ValueError(state.get("error", "请亲自进入单人地图后连接。"))
            self._remote("claim")
            self.observe()
            self.heartbeat_thread = threading.Thread(target=self._heartbeat, name="Factorio lease", daemon=True)
            self.heartbeat_thread.start()
        except Exception:
            self.close()
            raise
        return {"ok": True}

    def _remote(self, method, value=None):
        with self.lock:
            self._identity()
            if not self.rcon:
                raise RuntimeError("Factorio 尚未连接。")
            request = {"protocol": 2, "id": uuid.uuid4().hex, "session": self.session, "method": method}
            if value is not None:
                request["value"] = value
            command = "/playall-bridge " + json.dumps(request, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
            output = self.rcon.command(command)
            marker = output.find("PLAYALL:")
            if marker < 0:
                raise ValueError("游戏内连接组件尚未就绪。请进入单人地图，并在模组中启用 PlayALL Local Bridge。")
            try:
                result = json.loads(output[marker + 8:].strip())
                if (result.get("protocol") != 2 or result.get("id") != request["id"]
                        or result.get("session") != self.session or type(result.get("ok")) is not bool):
                    raise ValueError()
            except (ValueError, TypeError, AttributeError) as error:
                raise RuntimeError("Factorio 回复与本次连接不匹配。") from error
            if not result["ok"]:
                raise ValueError(str(result.get("error", "游戏拒绝了本次操作。")))
            if not isinstance(result.get("result"), dict):
                raise RuntimeError("Factorio 返回了无效结果。")
            return result["result"]

    def _heartbeat(self):
        while not self.heartbeat_stop.wait(.8):
            try:
                self._remote("heartbeat")
            except (OSError, RuntimeError, ValueError):
                self.fault = "Factorio 连接已中断或存档已切换。AI 不会继续操作，请重新选择进程并连接。"
                self.heartbeat_stop.set()
                return

    def window_ready(self):
        state = self._remote("hello")
        return bool(state.get("ready"))

    def observe(self):
        state = self._remote("observe")
        for key in ("resources", "buildings", "technologies"):
            if state.get(key) == {}:
                state[key] = []
        signature = json.dumps({key: state.get(key) for key in
            ("character_id", "position", "health", "inventory", "buildings", "research", "research_progress", "iron_plates_produced")},
            ensure_ascii=False, sort_keys=True)
        if self.progress_signature is not None and signature != self.progress_signature:
            self.mutated = True
        self.progress_signature = signature
        return state

    def execute(self, action, control):
        action = validate_action(action)
        with control.action() if hasattr(control, "action") else nullcontext():
            control.check()
            result = self._remote("execute", action)
            # Acceptance is not progress: a native action may finish with
            # insufficient materials or a collision. Observe/checkpoint compare
            # actual state so failed/no-op requests cannot cause empty saves.
        if not result.get("ok") or not result.get("busy"):
            return result.get("result") or result
        deadline = time.monotonic() + 260
        paused_before = getattr(control, "paused_time", 0)
        while True:
            control.check()
            state = self.observe()
            if hasattr(control, "publish"):
                control.publish(state)
            if not state.get("busy"):
                return state.get("last_result") or {"ok": False, "error": "动作已中断，请重新观察。"}
            if time.monotonic() - (getattr(control, "paused_time", 0) - paused_before) > deadline:
                self.pause()
                return {"ok": False, "error": "动作等待超时，AI 已暂停。"}
            control.sleep(.2)

    def pause(self):
        if self.rcon:
            result = self._remote("pause")
            self.paused = True
            result["manual_control"] = True
            return result

    cancel = pause

    def resume(self):
        result = self._remote("resume")
        self.paused = False
        return result

    def checkpoint_due(self):
        return bool(self.mutated and time.monotonic() - self.last_checkpoint >= 180)

    def checkpoint(self):
        if not self.rcon:
            return {"ok": True, "saved": False}
        self.observe()
        if not self.mutated or self.saved_signature == self.progress_signature:
            return {"ok": True, "saved": bool(self.saved_signature), "unchanged": True}
        # Continue the rotation across reconnects. A new adapter must not keep
        # replacing slot 1 while leaving the two other backups permanently old.
        existing = [(path.stat().st_mtime_ns, number) for number in (1, 2, 3)
                    if (path := self.data / "saves" / f"_autosave-playall-{number}.zip").is_file()]
        previous = max(existing)[1] if existing else self.save_slot
        slot = previous % 3 + 1
        path = self.data / "saves" / f"_autosave-playall-{slot}.zip"
        old = path.stat().st_mtime_ns if path.exists() else None
        self._remote("checkpoint", slot)
        deadline = time.monotonic() + 12
        while time.monotonic() < deadline:
            self._identity()
            if path.exists() and path.stat().st_mtime_ns != old:
                try:
                    with zipfile.ZipFile(path) as archive:
                        if any(name.endswith(("/level.dat", "/level.dat0")) for name in archive.namelist()):
                            self.last_checkpoint = time.monotonic()
                            self.saved_signature = self.progress_signature
                            self.mutated = False
                            self.save_slot = slot
                            return {"ok": True, "saved": True}
                except (OSError, zipfile.BadZipFile):
                    pass
            time.sleep(.1)
        raise RuntimeError("未能确认 Factorio 自动存档，请在游戏里手动保存。游戏会保持打开。")

    def close(self):
        self.heartbeat_stop.set()
        with self.lock:
            if self.rcon:
                try:
                    self._remote("release")
                except (OSError, RuntimeError, ValueError):
                    pass  # Never close or kill the user's game to release a lease.
                self.rcon.close()
                self.rcon = None
        if self.heartbeat_thread and self.heartbeat_thread is not threading.current_thread():
            self.heartbeat_thread.join(timeout=6)
