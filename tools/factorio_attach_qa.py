"""Real Factorio CLI check in a temporary world, without a GUI or paid AI.

This proves native mod loading, local RCON and refusal with no human player.
It does not claim to verify single-player keyboard handover.
"""
from __future__ import annotations

import json
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from playall.config import detect_factorio
from playall.games.factorio_attach import BUNDLE, BRIDGE_VERSION
from playall.games.rcon import Rcon
from playall.processes import tcp_listener_pid
from playall.storage import atomic_json


def main():
    root = Path(__file__).resolve().parents[1]
    evidence = {"game": "Factorio", "mode": "temporary headless server", "paid_ai_called": False,
                "user_saves_opened": False, "game_window_opened": False}
    exe = Path(detect_factorio())
    output = root / "data/qa/factorio/native-local-interface.json"
    with tempfile.TemporaryDirectory(prefix="playall-factorio-") as work:
        data = Path(work)
        mods = data / "mods"
        shutil.copytree(BUNDLE, mods / BUNDLE.name)
        atomic_json(mods / "mod-list.json", {"mods": [{"name": "base", "enabled": True},
                    {"name": "space-age", "enabled": False}, {"name": "quality", "enabled": False},
                    {"name": "elevated-rails", "enabled": False}, {"name": "playall-bridge", "enabled": True}]})
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        password = uuid.uuid4().hex
        config = data / "config.ini"
        config.write_text(f"[path]\nread-data={(exe.parents[2] / 'data').as_posix()}\nwrite-data={data.as_posix()}\n"
                          f"[other]\ncheck-updates=false\nlocal-rcon-socket=127.0.0.1:{port}\nlocal-rcon-password={password}\n", encoding="utf-8")
        save = data / "saves/qa.zip"
        save.parent.mkdir()
        common = [str(exe), "--config", str(config), "--mod-directory", str(mods)]
        flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
        creation = subprocess.run([*common, "--create", str(save), "--map-gen-seed", "28"],
                                  capture_output=True, timeout=90, creationflags=flags)
        if creation.returncode or not save.exists():
            raise RuntimeError("独立测试地图创建失败：" + creation.stdout.decode("utf-8", "replace")[-1500:])
        log = data / "server.log"
        server_settings = data / "server-settings.json"
        atomic_json(server_settings, {"name": "PlayALL isolated component check", "description": "Temporary native bridge check",
            "visibility": {"public": False, "lan": False}, "require_user_verification": False, "max_players": 4,
            "auto_pause": False, "autosave_interval": 5, "autosave_slots": 3, "allow_commands": "admins-only",
            "only_admins_can_pause_the_game": True})
        with log.open("wb") as stream:
            process = subprocess.Popen([*common, "--start-server", str(save), "--bind", "127.0.0.1:0",
                "--rcon-bind", f"127.0.0.1:{port}", "--rcon-password", password, "--server-settings", str(server_settings)],
                stdout=stream, stderr=subprocess.STDOUT, creationflags=flags)
            rcon = None
            try:
                deadline = time.monotonic() + 50
                while time.monotonic() < deadline:
                    if process.poll() is not None:
                        raise RuntimeError("Factorio 测试提前退出：" + log.read_text(encoding="utf-8", errors="replace")[-1500:])
                    if tcp_listener_pid(port) == process.pid:
                        try:
                            rcon = Rcon("127.0.0.1", port, password, timeout=3)
                            break
                        except (OSError, RuntimeError):
                            pass
                    time.sleep(.15)
                if not rcon:
                    raise RuntimeError("原生本地接口未启动：" + log.read_text(encoding="utf-8", errors="replace")[-1500:])
                session = uuid.uuid4().hex
                def call(method, **extra):
                    request = {"protocol": 2, "id": uuid.uuid4().hex, "session": session, "method": method, **extra}
                    raw = rcon.command("/playall-bridge " + json.dumps(request))
                    marker = raw.find("PLAYALL:")
                    if marker < 0:
                        raise RuntimeError("游戏模组还未就绪：" + raw[:300])
                    return json.loads(raw[marker + 8:].strip())
                # The listener can precede game loading; wait using only hello.
                deadline = time.monotonic() + 20
                while True:
                    try:
                        hello = call("hello")
                        break
                    except RuntimeError:
                        if time.monotonic() > deadline:
                            raise
                        time.sleep(.15)
                assert hello["ok"] and hello["result"]["bridge"] == BRIDGE_VERSION, hello
                evidence["hello"] = hello["result"]
                evidence["scope"] = "Headless test verifies mod/RCON/refusal only; the GUI local-rcon setting and human handover are checked separately in a single-player map."
                evidence["loopback_listener_belongs_to_test_pid"] = True
                claim = call("claim")
                evidence["headless_claim_refused"] = not claim["ok"]
                execute = call("execute", value={"action": "mine", "name": "iron-ore"})
                evidence["unclaimed_action_refused"] = not execute["ok"]
                assert evidence["headless_claim_refused"] and evidence["unclaimed_action_refused"]
                evidence["status"] = "passed"
            finally:
                if rcon:
                    rcon.close()
                if process.poll() is None:
                    process.terminate()  # Only this temporary, test-owned CLI.
                process.wait(timeout=15)
                evidence["test_process_stopped"] = process.poll() is not None
    atomic_json(output, evidence)
    print(json.dumps(evidence, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
