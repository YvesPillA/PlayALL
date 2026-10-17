from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import secrets
import shutil
import socket
import subprocess
import tempfile
import threading
import time
import uuid

from ..config import ROOT
from ..storage import atomic_json
from ..processes import list_processes, validate_selection
from .aistalt_actions import validate_action


def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def is_supported_process(process):
    """Only name-matched processes are inspected; verify their actual game build."""
    try:
        exe = Path(process.path)
        build = json.loads((ROOT / "mods/aistalt-bridge/build.json").read_text(encoding="utf-8"))
        assembly = exe.parent / "Aistalt_Data/Managed/InfantGod.dll"
        return exe.name.casefold() == "aistalt.exe" and exe.is_file() and sha256(assembly) == build["game_sha256"]
    except (OSError, ValueError, KeyError, TypeError):
        return False


def prepare_connection(directory, path, emit):
    if any(process.name.casefold() == "aistalt.exe" for process in list_processes()):
        raise ValueError("请先在游戏里保存并关闭幼神，再准备连接组件。")
    class Preparation:
        def check(self):
            pass
    Aistalt(directory, {"aistalt_path": path}, emit).prepare(Preparation())
    return {"ok": True, "message": "连接组件已准备好。请亲自打开游戏，再回主界面选择进程。"}


def install_bridge(exe, bundle):
    """Install owned binaries and documented startup settings transactionally."""
    import re

    def startup_configuration(original):
        # Edit values in place: ConfigFile.Save would rewrite unrelated comments.
        wanted = {
            "Chainloader": {"HideManagerGameObject": "true"},
            "Preloader.Entrypoint": {"Assembly": "UnityEngine.CoreModule.dll",
                                     "Type": "MonoBehaviour", "Method": ".cctor"},
        }
        try:
            text = (original or b"").decode("utf-8-sig")
        except UnicodeDecodeError as error:
            raise RuntimeError("游戏启动配置无法按 UTF-8 读取；已保留原文件。") from error
        lines = text.splitlines(keepends=True)
        newline = "\r\n" if "\r\n" in text else "\n"
        sections, positions, values = {}, {}, {}
        section = None
        for index, line in enumerate(lines):
            body = line.rstrip("\r\n")
            clean = body.strip()
            header = re.fullmatch(r"\[([^\]]+)\]", clean)
            if header:
                section = header.group(1)
                if section in wanted:
                    if section in sections:
                        raise RuntimeError("游戏启动配置有重复的关键分组；已保留原文件。")
                    sections[section] = index
                continue
            if section not in wanted or clean.startswith(("#", ";")) or "=" not in body:
                continue
            key, value = body.split("=", 1)
            if key.strip() not in wanted[section]:
                continue
            setting = (section, key.strip())
            if setting in positions:
                raise RuntimeError("游戏启动配置有重复的关键设置；已保留原文件。")
            positions[setting] = index
            values[setting] = value.strip()
        entrypoint = tuple(values.get(("Preloader.Entrypoint", key), default)
                           for key, default in (("Assembly", "UnityEngine.CoreModule.dll"),
                                                ("Type", "Application"), ("Method", ".cctor")))
        if entrypoint not in (("UnityEngine.CoreModule.dll", "Application", ".cctor"),
                              ("UnityEngine.CoreModule.dll", "MonoBehaviour", ".cctor")):
            raise RuntimeError("游戏已有不受支持的自定义启动入口；已保留配置和游戏文件。")
        hidden = values.get(("Chainloader", "HideManagerGameObject"), "false")
        if hidden.lower() not in ("true", "false"):
            raise RuntimeError("HideManagerGameObject 设置无法识别；已保留原文件。")
        inserts = {}
        missing_sections = []
        for section, settings in wanted.items():
            missing = []
            for key, value in settings.items():
                index = positions.get((section, key))
                if index is None:
                    missing.append(f"{key} = {value}{newline}")
                    continue
                body = lines[index].rstrip("\r\n")
                ending = lines[index][len(body):]
                prefix, old_value = body.split("=", 1)
                leading = old_value[:len(old_value) - len(old_value.lstrip())]
                trailing = old_value[len(old_value.rstrip()):]
                lines[index] = prefix + "=" + leading + value + trailing + ending
            if section in sections:
                if missing:
                    index = sections[section]
                    if not lines[index].endswith(("\r", "\n")):
                        lines[index] += newline
                    inserts[index] = "".join(missing)
            else:
                missing_sections.append("[" + section + "]" + newline + "".join(missing))
        result = "".join(line + inserts.get(index, "") for index, line in enumerate(lines))
        for addition in missing_sections:
            if result and not result.endswith(("\r", "\n")):
                result += newline
            if result:
                result += newline
            result += addition
        prefix = b"\xef\xbb\xbf" if original and original.startswith(b"\xef\xbb\xbf") else b""
        return prefix + result.encode("utf-8")

    destination = exe.parent
    plugin = destination / "BepInEx/plugins/PlayALL"
    manifest_path = plugin / "install.json"
    previous = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.exists() else {}
    owned = previous.get("files", {})
    sources = {str(p.relative_to(bundle / "loader")).replace("\\", "/"): p
               for p in (bundle / "loader").rglob("*") if p.is_file()}
    sources["BepInEx/plugins/PlayALL/PlayALL.Aistalt.dll"] = bundle / "PlayALL.Aistalt.dll"
    if not sources or not sources["BepInEx/plugins/PlayALL/PlayALL.Aistalt.dll"].is_file():
        raise RuntimeError("幼神桥接包缺失，请重新构建软件。")
    # Validate every destination before writing any file.
    for relative, source in sources.items():
        target = (destination / relative).resolve()
        if not target.is_relative_to(destination.resolve()):
            raise ValueError("桥接安装路径不正确。")
        if target.exists() and sha256(target) != sha256(source) and sha256(target) != owned.get(relative):
            raise RuntimeError("游戏已有其他版本的加载器或插件，已保留原文件：" + str(target))
    # BepInEx documents these alternatives for plugins lost during early startup:
    # https://docs.bepinex.dev/articles/user_guide/troubleshooting.html
    config_relative = "BepInEx/config/BepInEx.cfg"
    config_path = (destination / config_relative).resolve()
    if not config_path.is_relative_to(destination.resolve()):
        raise ValueError("桥接配置路径不正确。")
    original_config = config_path.read_bytes() if config_path.exists() else None
    configured = startup_configuration(original_config)
    backup_path = None
    if original_config is not None and original_config != configured:
        digest = hashlib.sha256(original_config).hexdigest()[:16]
        backup_path = config_path.with_name(config_path.name + ".playall-backup-" + digest + ".bak")
        if not backup_path.resolve().is_relative_to(destination.resolve()):
            raise ValueError("桥接配置备份路径不正确。")
        if backup_path.exists() and backup_path.read_bytes() != original_config:
            raise RuntimeError("启动配置备份路径已有其他文件；已保留原文件。")
    prior_config = previous.get("startup_config", {})
    backup_relative = str(backup_path.relative_to(destination.resolve())).replace("\\", "/") if backup_path else (
        prior_config.get("backup") if isinstance(prior_config, dict) else None)
    backups, created = {}, []
    try:
        if backup_path is not None and not backup_path.exists():
            backup_path.parent.mkdir(parents=True, exist_ok=True)
            with backup_path.open("xb") as stream:
                created.append(backup_path)
                stream.write(original_config)
                stream.flush()
                os.fsync(stream.fileno())
        for relative, source in sources.items():
            target = destination / relative
            if target.exists() and sha256(target) == sha256(source):
                continue
            if target.exists():
                backups[target] = target.read_bytes()
            else:
                created.append(target)
            target.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(prefix=target.name + ".playall-", suffix=".tmp", dir=target.parent, delete=False) as stream:
                temporary = Path(stream.name)
            try:
                shutil.copy2(source, temporary)
                os.replace(temporary, target)
            finally:
                temporary.unlink(missing_ok=True)
        if original_config != configured:
            current_config = config_path.read_bytes() if config_path.exists() else None
            if current_config != original_config:
                raise RuntimeError("安装过程中游戏启动配置已变化；已停止修改。")
            if original_config is None:
                created.append(config_path)
            else:
                backups[config_path] = original_config
            config_path.parent.mkdir(parents=True, exist_ok=True)
            temporary = None
            try:
                with tempfile.NamedTemporaryFile(prefix=config_path.name + ".playall-", suffix=".tmp",
                                                 dir=config_path.parent, delete=False) as stream:
                    temporary = Path(stream.name)
                    stream.write(configured)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, config_path)
            finally:
                if temporary is not None:
                    temporary.unlink(missing_ok=True)
        if manifest_path.exists():
            backups[manifest_path] = manifest_path.read_bytes()
        else:
            created.append(manifest_path)
        atomic_json(manifest_path, {"version": 1,
                                  "files": {relative: sha256(source) for relative, source in sources.items()},
                                  "startup_config": {"path": config_relative,
                                                     "sha256": hashlib.sha256(configured).hexdigest(),
                                                     "backup": backup_relative}})
    except Exception:
        for target, content in reversed(list(backups.items())):
            if not target.exists() or target.read_bytes() != content:
                target.write_bytes(content)
        for target in reversed(created):
            target.unlink(missing_ok=True)
        raise
    return plugin


def game_is_open():
    """Check the known game process name without focusing or controlling it."""
    if os.name != "nt":
        return False
    import ctypes
    from ctypes import wintypes
    class Entry(ctypes.Structure):
        _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
                    ("th32ProcessID", wintypes.DWORD), ("th32DefaultHeapID", ctypes.c_size_t),
                    ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
                    ("th32ParentProcessID", wintypes.DWORD), ("pcPriClassBase", wintypes.LONG),
                    ("dwFlags", wintypes.DWORD), ("szExeFile", wintypes.WCHAR * 260)]
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(Entry)]
    kernel.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(Entry)]
    handle = kernel.CreateToolhelp32Snapshot(2, 0)
    if handle == ctypes.c_void_p(-1).value:
        raise OSError("无法检查游戏是否已经打开。")
    try:
        entry = Entry()
        entry.dwSize = ctypes.sizeof(entry)
        valid = kernel.Process32FirstW(handle, ctypes.byref(entry))
        while valid:
            if entry.szExeFile.lower() == "aistalt.exe":
                return True
            valid = kernel.Process32NextW(handle, ctypes.byref(entry))
        return False
    finally:
        kernel.CloseHandle(handle)


class Aistalt:
    id = "aistalt"
    label = "幼神 Demo"

    def __init__(self, directory, settings, emit):
        self.directory = Path(directory) / "aistalt"
        self.selected_process = settings.get("game_process")
        self.exe = Path(self.selected_process["path"] if isinstance(self.selected_process, dict) and self.selected_process.get("path")
                        else settings.get("aistalt_path", ""))
        self.emit = emit
        self.lock = threading.RLock()
        self.connection = self.reader = None
        self.endpoint = None
        self.controller = secrets.token_hex(24)
        self.closed = threading.Event()
        self.heartbeat_thread = None
        self.process = self.viewer = None  # The user owns this graphical game.
        self.mutated = self.saved = False
        self.bundle = ROOT / "mods/aistalt-bridge"
        self.last_connection_error = ""
        self.is_loading = False
        self.current_scene = None
        self.last_checkpoint = time.monotonic()
        self.progress_signature = None
        self.saved_signature = None

    def prepare(self, control, new_world=False):
        control.check()
        if not self.exe.is_file() or self.exe.name.lower() != "aistalt.exe":
            raise ValueError("没有找到幼神 Demo。请在设置中选择游戏 Build 目录里的 Aistalt.exe。")
        self.build = json.loads((self.bundle / "build.json").read_text(encoding="utf-8"))
        assembly = self.exe.parent / "Aistalt_Data/Managed/InfantGod.dll"
        if not assembly.is_file() or sha256(assembly) != self.build["game_sha256"]:
            raise ValueError("游戏版本与已核对的适配器不同，需要重新分析这一版本。")
        if sha256(self.bundle / "PlayALL.Aistalt.dll") != self.build["bridge_sha256"]:
            raise RuntimeError("幼神桥接文件未通过完整性检查，请重新构建软件。")
        if self.selected_process:
            validate_selection(self.selected_process, "Aistalt.exe")
            installed = self.exe.parent / "BepInEx/plugins/PlayALL/PlayALL.Aistalt.dll"
            if not installed.is_file() or sha256(installed) != self.build["bridge_sha256"]:
                raise RuntimeError("游戏连接组件需要更新。请先保存并关闭幼神，在设置中准备连接组件后，再亲自打开游戏并重新选择进程。")
        self.emit("diagnostic", "游戏与连接组件完整性检查通过。")
        self.plugin = self.exe.parent / "BepInEx/plugins/PlayALL" if self.selected_process else install_bridge(self.exe, self.bundle)
        self.endpoint_path = self.plugin / "endpoint.json"
        self.directory.mkdir(parents=True, exist_ok=True)
        self.emit("system", "幼神桥接已准备好。操作将通过游戏原有界面事件执行。")
        if new_world:
            self.emit("notice", "新游戏需通过游戏原生菜单选择空存档；适配器不会删除已有存档。")

    def _disconnect(self):
        if self.reader:
            self.reader.close()
            self.reader = None
        if self.connection:
            self.connection.close()
            self.connection = None

    def _connect(self):
        if not self.selected_process:
            raise ValueError("请先选择已经打开的幼神进程，连接不会启动游戏。")
        validate_selection(self.selected_process, "Aistalt.exe")
        if not self.endpoint_path.is_file() or self.endpoint_path.stat().st_size > 8192:
            raise RuntimeError("游戏尚未加载幼神桥接，请打开或重新启动游戏。")
        info = json.loads(self.endpoint_path.read_text(encoding="utf-8"))
        if not isinstance(info, dict):
            raise RuntimeError("幼神桥接记录格式不正确。")
        port = info.get("port")
        if (type(info.get("protocol")) is not int or info["protocol"] != 1 or info.get("game") != self.id or info.get("headless") is not False
                or info.get("game_sha256") != self.build["game_sha256"]
                or isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535
                or type(info.get("pid")) is not int or info["pid"] <= 0
                or not isinstance(info.get("token"), str) or len(info["token"]) != 64
                or not isinstance(info.get("instance"), str) or len(info["instance"]) != 32
                or any(char not in "0123456789abcdef" for char in info["token"] + info["instance"])):
            raise RuntimeError("幼神桥接身份、版本或窗口状态不匹配。")
        if info["pid"] != self.selected_process["pid"]:
            raise RuntimeError("连接记录属于另一个游戏进程，请重新选择正确的进程。")
        connection = socket.create_connection(("127.0.0.1", port), timeout=7)
        connection.settimeout(7)
        self.connection = connection
        self.reader = connection.makefile("rb")
        self.endpoint = info
        self.emit("diagnostic", "已接通所选进程的本机连接，正在核对游戏状态。")

    def _call(self, method, **payload):
        with self.lock:
            if self.connection is None:
                self._connect()
            request_id = uuid.uuid4().hex
            request = {"method": method, "id": request_id, "controller": self.controller,
                       "token": self.endpoint["token"], "instance": self.endpoint["instance"], **payload}
            try:
                self.connection.sendall(json.dumps(request, ensure_ascii=False, allow_nan=False).encode("utf-8") + b"\n")
                raw = self.reader.readline(1024 * 1024 + 1)
                if len(raw) > 1024 * 1024 or not raw.endswith(b"\n"):
                    raise RuntimeError("游戏桥接响应不完整；已停止提交后续动作。")
                result = json.loads(raw)
                if not isinstance(result, dict) or result.get("id") != request_id or result.get("instance") != self.endpoint["instance"]:
                    raise RuntimeError("游戏响应与当前操作不匹配；请重新观察后继续。")
                if not isinstance(result.get("ok"), bool):
                    raise RuntimeError("游戏桥接返回的成功标记格式不正确。")
                if not result["ok"]:
                    if method in ("execute", "review", "approve", "reject", "resume"):
                        return result
                    raise RuntimeError(result.get("error", "游戏桥接没有完成请求。"))
                if method in ("hello", "ping") and (
                    type(result.get("protocol")) is not int or result["protocol"] != 1
                    or result.get("game") != self.id or result.get("game_sha256") != self.build["game_sha256"]
                    or not isinstance(result.get("ready"), bool)):
                    raise RuntimeError("游戏桥接返回的身份、版本或窗口状态不匹配。")
                if method == "checkpoint" and (not isinstance(result.get("saved"), bool)
                        or ("pending" in result and not isinstance(result["pending"], bool))):
                    raise RuntimeError("游戏桥接返回的存档确认格式不正确。")
                if method == "review":
                    if type(result.get("requires_confirmation")) is not bool:
                        raise RuntimeError("游戏返回的确认要求格式不正确。")
                    if result["requires_confirmation"] and (not isinstance(result.get("question"), str)
                            or not 1 <= len(result["question"]) <= 2000 or not self._valid_ticket(result.get("ticket"))):
                        raise RuntimeError("重要操作的确认信息不完整。")
                if method == "approve" and not self._valid_ticket(result.get("approval")):
                    raise RuntimeError("游戏返回的确认凭据格式不正确。")
                return result
            except (OSError, ValueError, RuntimeError):
                self._disconnect()
                # A sent click is never silently retried after uncertain delivery.
                raise

    def _heartbeat(self):
        while not self.closed.wait(1):
            try:
                self._call("ping")
            except (OSError, ValueError, RuntimeError) as error:
                self.emit("diagnostic", "游戏心跳中断。", error=str(error))
                return  # Resume reconnects and starts a new heartbeat.

    def _start_heartbeat(self):
        if not self.heartbeat_thread or not self.heartbeat_thread.is_alive():
            self.heartbeat_thread = threading.Thread(target=self._heartbeat, name="PlayALL game heartbeat", daemon=True)
            self.heartbeat_thread.start()

    def window_ready(self):
        try:
            result = self._call("hello")
            self.is_loading = result.get("loading") is True
            ready = bool(result.get("ready"))
            reason = result.get("ready_reason")
            message = "" if ready else reason[:1000] if isinstance(reason, str) and reason else "连接组件正在等待游戏界面就绪。"
            if self.last_connection_error != message:
                self.emit("diagnostic", "游戏连接已就绪。" if ready else message)
            self.last_connection_error = message
            return ready
        except (OSError, ValueError, RuntimeError) as error:
            self.is_loading = False
            message = str(error)
            if message != self.last_connection_error:
                self.emit("diagnostic", "游戏连接检查未通过。", error=message)
            self.last_connection_error = message
            return False

    def watch(self):
        if self.window_ready():
            return {"ok": True, "message": "幼神 Demo 已连接。"}
        return {"ok": False, "message": self.last_connection_error or "请亲自打开幼神并选择进程。"}

    def connect_window(self, control):
        if not self.selected_process:
            raise ValueError("请先选择已经打开的幼神进程。连接不会启动游戏。")
        validate_selection(self.selected_process, "Aistalt.exe")
        if not self.window_ready():
            self.emit("system", "等待所选幼神进程的连接组件就绪，此时尚未调用 AI。")
        deadline = time.monotonic() + 90
        while not self.window_ready():
            control.check()
            if time.monotonic() >= deadline:
                raise RuntimeError("连接未完成，AI 没有开始操作。原因：" + self.last_connection_error)
            control.sleep(.25)
        self._start_heartbeat()
        return {"ok": True}

    def observe(self):
        result = self._call("observe")
        self.current_scene = result.get("scene")
        # Menus, scrolling and idle observations are not new game progress.
        signature = json.dumps({key: result.get(key) for key in ("scene", "dialogue", "stats")}, sort_keys=True, ensure_ascii=False)
        if result.get("scene") == "MainGame" and signature != self.progress_signature:
            self.mutated, self.saved = True, False
        self.progress_signature = signature
        return result

    @staticmethod
    def _valid_ticket(value):
        return isinstance(value, str) and len(value) == 32 and all(c in "0123456789abcdef" for c in value)

    def review_action(self, action):
        action = validate_action(action)
        if action["action"] != "click":
            return {"ok": True, "requires_confirmation": False}
        return self._call("review", action=action)

    def approve_action(self, ticket):
        if not self._valid_ticket(ticket):
            raise ValueError("确认凭据不正确。")
        return self._call("approve", ticket=ticket)

    def reject_action(self, ticket):
        if not self._valid_ticket(ticket):
            raise ValueError("确认凭据不正确。")
        return self._call("reject", ticket=ticket)

    def execute_approved(self, action, approval, control):
        if not self._valid_ticket(approval):
            raise ValueError("本次操作没有有效的人类确认。")
        with control.action():
            current = self.observe()
            action = {**validate_action(action), "view_revision": current["view_revision"]}
            return self._execute(action, control, approval=approval)

    def execute(self, action, control):
        action = validate_action(action)
        return self._execute(action, control)

    def _execute(self, action, control, approval=None):
        with control.action():
            control.check()
            prior = self.mutated, self.saved
            if action["action"] not in ("observe", "wait"):
                # Delivery may succeed before the socket breaks; preserve the
                # need to save even when no acknowledgement reaches the app.
                self.mutated, self.saved = True, False
            result = self._call("execute", action=action, **({"approval": approval} if approval else {}))
            if result.get("requires_confirmation"):
                self.mutated, self.saved = prior
        if action["action"] == "wait" and result.get("ok"):
            control.sleep(action["seconds"])
        elif action["action"] != "observe":
            control.sleep(.2)
        return result

    def pause(self):
        if self.connection:
            return self._call("pause")
        return {"ok": True, "replan": True, "manual_control": True}

    def resume(self):
        self._start_heartbeat()
        result = self._call("resume")
        if not result.get("ok"):
            raise ValueError(result.get("error", "游戏拒绝了继续操作。"))
        return result

    def checkpoint(self):
        if self.connection is None and not self.mutated:
            return {"ok": True, "saved": False}
        self.observe()  # Also detect progress made while the human had control.
        if self.saved and not self.mutated and self.saved_signature == self.progress_signature:
            return {"ok": True, "saved": True, "unchanged": True}
        result = self._call("checkpoint")
        self.last_checkpoint = time.monotonic()
        self.saved = result["saved"]
        if self.saved:
            self.mutated = False
            self.saved_signature = self.progress_signature
            atomic_json(self.directory / "last-save.json", result)
        return result

    def checkpoint_due(self):
        return self.current_scene == "MainGame" and self.mutated and time.monotonic() - self.last_checkpoint >= 180

    def cancel(self):
        return self.pause()

    def close(self):
        self.closed.set()
        with self.lock:
            try:
                if self.connection:
                    self._call("detach")
            finally:
                self._disconnect()
        if self.heartbeat_thread:
            self.heartbeat_thread.join(timeout=2)
        self.emit("notice", "AI 已断开，幼神 Demo 保持打开，仍可亲自操作。")
