"""Production Lua helpers against real 2.0.77 Lua objects in private fixtures.

Creates its own base/DLC headless worlds; never opens the user's save, touches
their configuration, launches a game window or calls a paid model.
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

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from playall.config import detect_factorio
from playall.games.factorio_attach import BUNDLE, BRIDGE_VERSION, digest
from playall.games.rcon import Rcon
from playall.processes import tcp_listener_pid
from playall.storage import atomic_json


def fixture_source():
    source = (BUNDLE / "control.lua").read_text(encoding="utf-8")
    parts = []
    for start, end in (("local function contents(", "-- Protocol 2:"),
                       ("local function inventory_for(", "local function nearest("),
                       ("local function recipe_info(", "local function observe(")):
        parts.append(source[source.index(start):source.index(end)])
    return ("local status_names={}\nfor name,value in pairs(defines.entity_status) do status_names[value]=name end\n"
            + "\n".join(parts) + (ROOT / "tools/factorio_fixture.lua").read_text(encoding="utf-8"))


def run(exe, dlc):
    with tempfile.TemporaryDirectory(prefix="playall-native-qa-") as folder:
        data = Path(folder)
        mods = data / "mods"
        shutil.copytree(BUNDLE, mods / BUNDLE.name)
        fixture = mods / "playall-native-fixture_0.1.0"
        fixture.mkdir()
        atomic_json(fixture / "info.json", {"name": "playall-native-fixture", "version": "0.1.0",
            "title": "PlayALL isolated test", "author": "PlayALL", "factorio_version": "2.0", "dependencies": ["base >= 2.0.77"]})
        (fixture / "control.lua").write_text(fixture_source(), encoding="utf-8")
        atomic_json(mods / "mod-list.json", {"mods": [{"name": "base", "enabled": True},
            *[{"name": name, "enabled": dlc} for name in ("quality", "space-age", "elevated-rails")],
            {"name": "playall-bridge", "enabled": True}, {"name": "playall-native-fixture", "enabled": True}]})
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        password = uuid.uuid4().hex
        config = data / "config.ini"
        config.write_text(f"[path]\nread-data={(exe.parents[2] / 'data').as_posix()}\nwrite-data={data.as_posix()}\n"
                          "[other]\ncheck-updates=false\n", encoding="utf-8")
        save = data / "saves/fixture.zip"
        save.parent.mkdir()
        common = [str(exe), "--config", str(config), "--mod-directory", str(mods)]
        flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
        creation = subprocess.run([*common, "--create", str(save), "--map-gen-seed", "28"],
                                  capture_output=True, timeout=90, creationflags=flags)
        if creation.returncode or not save.is_file():
            raise RuntimeError("Fixture creation failed: " + creation.stdout.decode("utf-8", "replace")[-2000:])
        settings = data / "server-settings.json"
        atomic_json(settings, {"name": "PlayALL private native API test", "description": "Synthetic fixtures only",
            "visibility": {"public": False, "lan": False}, "require_user_verification": False,
            "auto_pause": False, "autosave_interval": 0, "allow_commands": "admins-only"})
        log = data / "server.log"
        with log.open("wb") as stream:
            process = subprocess.Popen([*common, "--start-server", str(save), "--bind", "127.0.0.1:0",
                "--rcon-bind", f"127.0.0.1:{port}", "--rcon-password", password, "--server-settings", str(settings)],
                stdout=stream, stderr=subprocess.STDOUT, creationflags=flags)
            rcon = None
            try:
                until = time.monotonic() + 50
                while time.monotonic() < until:
                    if process.poll() is not None:
                        raise RuntimeError(log.read_text(encoding="utf-8", errors="replace")[-2500:])
                    if tcp_listener_pid(port) == process.pid:
                        try:
                            rcon = Rcon("127.0.0.1", port, password, timeout=15)
                            raw = rcon.command("/playall-native-fixture")
                            if "FIXTURE:" in raw:
                                result = json.loads(raw.split("FIXTURE:", 1)[1].strip())
                                result["source_sha256"] = digest(BUNDLE / "control.lua")
                                result["bridge"] = BRIDGE_VERSION
                                return result
                        except (OSError, RuntimeError):
                            if rcon:
                                rcon.close()
                                rcon = None
                    time.sleep(.15)
                raise RuntimeError("Native fixture unavailable: " + log.read_text(encoding="utf-8", errors="replace")[-2500:])
            finally:
                if rcon:
                    rcon.close()
                if process.poll() is None:
                    process.terminate()  # Only this test-owned headless process.
                process.wait(timeout=15)


def main():
    exe = Path(detect_factorio())
    evidence = {"paid_ai_called": False, "user_save_opened": False, "game_window_opened": False, "runs": []}
    output = ROOT / "data/qa/factorio/native-api-fixtures.json"
    for dlc in (False, True):
        result = run(exe, dlc)
        evidence["runs"].append(result)
        atomic_json(output, evidence)
        print(json.dumps({"dlc": dlc, "ok": result["ok"], "checks": [{"name": c["name"], "status": c["status"],
            **({"detail": c["detail"]} if c["status"] != "passed" else {})} for c in result["checks"]]}, ensure_ascii=False), flush=True)
    evidence["status"] = "passed" if all(r["ok"] for r in evidence["runs"]) else "failed"
    atomic_json(output, evidence)
    return 0 if evidence["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
