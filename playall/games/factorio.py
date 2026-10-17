from __future__ import annotations

import json
import os
import secrets
import shutil
import socket
import subprocess
import threading
import time
import zipfile
from contextlib import nullcontext
from pathlib import Path

from ..config import ROOT
from ..processes import OwnedProcesses
from .actions import validate_action
from .rcon import Rcon


def free_port(kind=socket.SOCK_STREAM):
    with socket.socket(socket.AF_INET, kind) as connection:
        connection.bind(("127.0.0.1", 0))
        return connection.getsockname()[1]


def lua_string(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n").replace("\r", "\\r") + '"'


class Factorio:
    id = "factorio"
    label = "Factorio"

    def __init__(self, directory: Path, settings: dict, emit):
        self.directory = Path(directory) / "factorio"
        self.settings, self.emit = settings, emit
        self.exe = Path(settings.get("factorio_path", ""))
        self.process = None
        self.rcon = None
        self.lock = threading.RLock()
        self.log_file = None
        self.viewer_log = None
        self.requires_window = False
        self.owned = OwnedProcesses()
        self.viewer = None
        self.port = free_port(socket.SOCK_DGRAM)
        self.rcon_port = free_port()
        self.password = secrets.token_urlsafe(24)
        self.save_path = self.directory / "saves/playall.zip"

    def _popen(self, arguments, log_path):
        stream = open(log_path, "wb")
        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        try:
            process = subprocess.Popen([str(self.exe), *arguments], stdin=subprocess.PIPE, stdout=stream,
                                       stderr=subprocess.STDOUT, creationflags=flags, cwd=self.directory)
            self.owned.add(process)
        except Exception:
            stream.close()
            raise
        return process, stream

    def _config(self, directory):
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / "config.ini"
        data_path = self.exe.resolve().parents[2] / "data"
        path.write_text(f"[path]\nread-data={data_path.as_posix()}\nwrite-data={directory.resolve().as_posix()}\n"
                        "[graphics]\nfull-screen=false\n[other]\ncheck-updates=false\n", encoding="utf-8")
        return path

    def _tail(self):
        path = self.directory / "server.log"
        if not path.exists():
            path = self.directory / "create.log"
        return path.read_text(encoding="utf-8", errors="replace")[-2000:] if path.exists() else ""

    def prepare(self, control, new_world=False):
        if not self.exe.is_file():
            raise ValueError("没有找到 Factorio。打开设置，选择 Factorio 的 factorio.exe 文件。")
        version = subprocess.run([str(self.exe), "--version"], capture_output=True, text=True, timeout=15,
                                 creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        if "Version: 2.0." not in version.stdout:
            raise ValueError("当前适配器需要 Factorio 2.0.x，请选择对应安装。")
        self.directory.mkdir(parents=True, exist_ok=True)
        self.save_path.parent.mkdir(parents=True, exist_ok=True)
        mods = self.directory / "mods"
        mods.mkdir(exist_ok=True)
        shutil.copytree(ROOT / "mods/playall-bridge_0.1.0", mods / "playall-bridge_0.1.0", dirs_exist_ok=True)
        (mods / "mod-list.json").write_text(json.dumps({"mods": [{"name": "base", "enabled": True},
            {"name": "space-age", "enabled": False}, {"name": "quality", "enabled": False},
            {"name": "elevated-rails", "enabled": False}, {"name": "playall-bridge", "enabled": True}]}), encoding="utf-8")
        config = self._config(self.directory)
        common = ["--config", str(config), "--mod-directory", str(mods)]
        if new_world and self.save_path.exists():
            backup = self.directory / "backups"
            backup.mkdir(exist_ok=True)
            shutil.copy2(self.save_path, backup / f"playall-{time.time_ns()}.zip")
            self.save_path.unlink()
        if not self.save_path.exists():
            self.emit("system", "正在创建专用和平地图…")
            generation = self.directory / "map-gen.json"
            generation.write_text(json.dumps({"seed": 28, "peaceful_mode": True, "starting_area": 2,
                                              "autoplace_controls": {"enemy-base": {"size": 0, "frequency": 0}}}), encoding="utf-8")
            process, stream = self._popen([*common, "--create", str(self.save_path), "--map-gen-settings", str(generation)], self.directory / "create.log")
            self.process, self.log_file = process, stream
            try:
                deadline = time.monotonic() + 90
                while process.poll() is None:
                    control.check()
                    if time.monotonic() > deadline:
                        raise RuntimeError("创建地图超时。检查 data/factorio/create.log。")
                    control.sleep(0.15)
                if process.returncode or not self.save_path.exists():
                    raise RuntimeError("Factorio 创建地图失败：" + self._tail())
            finally:
                if process.poll() is None:
                    process.terminate()
                    process.wait(timeout=10)
                stream.close()
                self.process, self.log_file = None, None
        control.check()
        server_settings = self.directory / "server-settings.json"
        server_settings.write_text(json.dumps({"name": "PlayALL local", "description": "Local AI game",
            "visibility": {"public": False, "lan": False}, "require_user_verification": False,
            "max_players": 4, "auto_pause": False, "autosave_interval": 5, "autosave_slots": 3,
            "allow_commands": "admins-only", "only_admins_can_pause_the_game": True}), encoding="utf-8")
        self.emit("system", "正在载入 Factorio，准备工程师…")
        self.process, self.log_file = self._popen([*common, "--start-server", str(self.save_path),
            "--bind", f"127.0.0.1:{self.port}", "--rcon-bind", f"127.0.0.1:{self.rcon_port}",
            "--rcon-password", self.password, "--server-settings", str(server_settings)], self.directory / "server.log")
        deadline = time.monotonic() + 75
        while time.monotonic() < deadline:
            control.check()
            if self.process.poll() is not None:
                raise RuntimeError("Factorio 启动失败：" + self._tail())
            try:
                self.rcon = Rcon("127.0.0.1", self.rcon_port, self.password, timeout=5)
                break
            except (OSError, RuntimeError):
                control.sleep(0.25)
        else:
            raise RuntimeError("无法连接 Factorio，检查 data/factorio/server.log。")
        # Vanilla's first Lua command only writes a confirmation warning to the
        # server log, with an empty RCON reply. Prime with a harmless, repeatable
        # command before sending any action that could change game state.
        ready = '/silent-command rcon.print("PLAYALL_READY")'
        output = self.rcon.command(ready)
        if "PLAYALL_READY" not in output:
            output = self.rcon.command(ready)
        if "PLAYALL_READY" not in output:
            raise RuntimeError("Factorio 未能启用脚本控制：" + self._tail())
        result = self._remote("setup", {"speed": self.settings.get("speed", 4)})
        if not result.get("ok"):
            raise RuntimeError(result.get("error", "工程师初始化失败。"))
        self.emit("system", "真实 Factorio 已连接，工程师就绪。")

    def _remote(self, method, value=None):
        with self.lock:
            if not self.rcon:
                raise RuntimeError("Factorio 尚未连接。")
            argument = ", helpers.json_to_table(" + lua_string(json.dumps(value, ensure_ascii=False, allow_nan=False)) + ")" if value is not None else ""
            command = '/silent-command rcon.print("PLAYALL:" .. helpers.table_to_json(remote.call("playall", ' + lua_string(method) + argument + ')))'
            output = self.rcon.command(command)
            if "repeat the command" in output or "disable achievements" in output:
                output = self.rcon.command(command)
            marker = output.find("PLAYALL:")
            if marker < 0:
                raise RuntimeError("游戏桥接失败：" + output[:1400])
            result = json.loads(output[marker + 8:].strip())
            return result

    def observe(self):
        state = self._remote("dispatch", {"action": "observe"})
        for key in ("resources", "buildings", "technologies"):
            if state.get(key) == {}:
                state[key] = []
        if state.get("map", {}).get("resources") == {}:
            state["map"]["resources"] = []
        return state

    def execute(self, action, control):
        action = validate_action(action)
        with control.action() if hasattr(control, "action") else nullcontext():
            control.check()
            result = self._remote("dispatch", action)
        if not result.get("ok") or not result.get("busy"):
            return result.get("result") or result
        deadline = time.monotonic() + 260
        paused_before = getattr(control, "paused_time", 0)
        while True:
            control.check()
            state = self.observe()
            if self.requires_window and not state.get("window_connected"):
                self.pause()
                return {"ok": False, "reason": "window_closed", "error": "游戏窗口已断开，AI 已暂停。"}
            if hasattr(control, "publish"):
                control.publish(state)
            if not state.get("busy"):
                return state.get("last_result") or {"ok": False, "error": "动作没有返回结果。"}
            if time.monotonic() - (getattr(control, "paused_time", 0) - paused_before) > deadline:
                self.cancel()
                return {"ok": False, "error": "动作执行超时，已中止。"}
            control.sleep(0.2)

    def pause(self):
        if self.rcon:
            return self._remote("pause")

    def resume(self):
        if self.rcon:
            return self._remote("resume")

    def cancel(self):
        if self.rcon:
            self._remote("cancel")

    def watch(self):
        with self.lock:
            return self._watch()

    def _watch(self):
        if not self.process or self.process.poll() is not None:
            raise ValueError("请先开始游玩，再打开游戏。")
        if self.viewer and self.viewer.poll() is None:
            return {"ok": True, "message": "Factorio 已经打开。"}
        viewer_config = self._config(self.directory / "viewer")
        self._remote("connection", {"waiting": True})
        if self.viewer_log:
            self.viewer_log.close()
        self.viewer_log = (self.directory / "viewer" / "startup.log").open("wb")
        try:
            self.viewer = subprocess.Popen([str(self.exe), "--config", str(viewer_config), "--mod-directory", str(self.directory / "mods"),
                              "--mp-connect", f"127.0.0.1:{self.port}", "--window-size", "1280x800"],
                             stdout=self.viewer_log, stderr=subprocess.STDOUT, creationflags=0, cwd=self.directory)
            self.owned.add(self.viewer)
        except Exception:
            self.viewer_log.close()
            self.viewer_log = None
            raise
        try:
            code = self.viewer.wait(timeout=1)
            raise RuntimeError(self._viewer_error(code))
        except subprocess.TimeoutExpired:
            pass
        return {"ok": True, "message": "正在打开 Factorio。暂停 AI 后可以接手同一角色。"}

    def _viewer_error(self, code):
        path = self.directory / "viewer" / "startup.log"
        detail = path.read_text(encoding="utf-8", errors="replace")[-1600:] if path.exists() else ""
        return f"Factorio 游戏窗口未能启动（{code}）。\n{detail}\n详细记录：{path}"

    def connect_window(self, control):
        self.requires_window = True
        self._remote("connection", {"required": True})
        if self.window_ready():
            return {"ok": True}
        self.watch()
        self.emit("system", "等待 Factorio 进入地图。此时还没有开始调用 AI。")
        deadline = time.monotonic() + 90
        while True:
            control.check()
            if self.window_ready():
                self.emit("system", "游戏画面已连接；暂停 AI 后可以亲自接手角色。")
                return {"ok": True}
            if self.viewer and self.viewer.poll() is not None:
                raise RuntimeError(self._viewer_error(self.viewer.returncode))
            if time.monotonic() >= deadline:
                raise RuntimeError("游戏窗口尚未进入地图，AI 没有开始游玩。请完成游戏窗口中的本机玩家设置，再点接着玩。\n"
                                   "启动记录：" + str(self.directory / "viewer/startup.log"))
            control.sleep(.25)

    def window_ready(self):
        return bool(self._remote("connection").get("connected"))

    def checkpoint(self):
        with self.lock:
            if not self.rcon:
                return
            old = self.save_path.stat().st_mtime_ns if self.save_path.exists() else 0
            output = self.rcon.command("/server-save playall")
            if any(word in output.lower() for word in ("cannot save", "error", "failed", "unknown command")):
                raise RuntimeError("Factorio 拒绝保存：" + output[:300])
            deadline = time.monotonic() + 12
            while time.monotonic() < deadline:
                if self.save_path.exists() and self.save_path.stat().st_mtime_ns != old:
                    try:
                        with zipfile.ZipFile(self.save_path) as archive:
                            if any(name.endswith("level.dat") or name.endswith("level.dat0") for name in archive.namelist()):
                                return
                    except (OSError, zipfile.BadZipFile):
                        pass  # Saving is asynchronous; retry while the archive is written.
                time.sleep(0.1)
            raise RuntimeError("未能确认新的 Factorio 存档，之前的存档仍保留。")

    def close(self):
        save_error = None
        with self.lock:
            if self.rcon:
                try:
                    self.cancel()
                    self.checkpoint()
                except (OSError, RuntimeError) as error:
                    save_error = error
                    if self.process and self.process.poll() is None:
                        # Keep the paused live server so saving can be retried.
                        raise RuntimeError(str(error)) from None
                self.rcon.close()
                self.rcon = None
            if self.viewer and self.viewer.poll() is None:
                self.viewer.terminate()
                self.viewer.wait(timeout=5)
            if self.process and self.process.poll() is None:
                try:
                    self.process.stdin.write(b"/quit\n")
                    self.process.stdin.flush()
                    self.process.wait(timeout=4)
                except (OSError, subprocess.TimeoutExpired):
                    self.process.terminate()
                    self.process.wait(timeout=10)
            if self.log_file:
                self.log_file.close()
            if getattr(self, "viewer_log", None):
                self.viewer_log.close()
                self.viewer_log = None
            self.process = None
            self.owned.close()
        if save_error:
            raise RuntimeError(str(save_error))
