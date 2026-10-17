"""Temporary installs and protocol fixtures; never launch or control a game."""
from __future__ import annotations

from contextlib import ExitStack
import hashlib
import json
import os
from pathlib import Path
from types import SimpleNamespace
import shutil
import socket
import time
import unittest
from unittest.mock import Mock, patch
import zipfile

from playall.games import factorio_attach as adapter
from playall.processes import GameProcess, tcp_listener_pid
from playall.runner import Runner
from tests.support import IsolatedSettingsMixin


class FixtureRcon:
    """Wire boundary for the Python client, not evidence of native execution."""
    def __init__(self):
        self.calls = []
        self.paused = True
        self.closed = False
        self.ready = True
        self.corrupt = None
        self.session = None
        self.observation = {"ok": True, "game": "factorio", "window_connected": True, "tick": 12,
                            "inventory": {"iron-plate": 8}, "position": {"x": 0, "y": 0}, "busy": False}

    def command(self, value):
        assert value.startswith("/playall-bridge "), value
        request = json.loads(value.split(" ", 1)[1])
        self.calls.append(request)
        method = request["method"]
        result = {"ok": True}
        if method == "hello":
            result.update(ready=self.ready, version="2.0.77", bridge=adapter.BRIDGE_VERSION, protocol=2)
        elif method == "claim":
            self.session = request["session"]
        elif method == "observe":
            result = dict(self.observation)
        elif method == "pause":
            self.paused = True
            result["replan"] = True
        elif method == "resume":
            self.paused = False
            result["replan"] = True
        elif method == "execute":
            result.update(ok=not self.paused, error="paused" if self.paused else "")
        elif method == "release":
            self.session = None
            self.paused = True
        response = {"protocol": 2, "id": request["id"], "session": request["session"], "ok": True, "result": result}
        if self.corrupt:
            response.update(self.corrupt)
        return "PLAYALL:" + json.dumps(response)

    def close(self):
        self.closed = True


class FactorioAttachTests(IsolatedSettingsMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.installation = self.directory / "game"
        self.exe = self.installation / "bin/x64/factorio.exe"
        self.exe.parent.mkdir(parents=True)
        self.exe.write_bytes(b"synthetic factorio, never executable")
        (self.installation / "config-path.cfg").write_text("config-path=__PATH__system-write-data__/config\nuse-system-read-write-data-directories=false\n")
        self.config = self.installation / "config/config.ini"
        self.config.parent.mkdir()
        self.original_config = b"\xef\xbb\xbf[graphics]\r\nfull-screen=false\r\n[other]\r\n; preserved\r\nlocale=zh-CN\r\n"
        self.config.write_bytes(self.original_config)
        self.mods = self.installation / "mods"
        self.mods.mkdir()
        self.original_mods = {"mods": [{"name": "base", "enabled": True}, {"name": "some-other-mod", "enabled": False}], "other": 17}
        (self.mods / "mod-list.json").write_text(json.dumps(self.original_mods))
        self.bundle = self.directory / "bundle"
        shutil.copytree(adapter.BUNDLE, self.bundle)
        build = json.loads((self.bundle / "build.json").read_text())
        build["executable_sha256"] = hashlib.sha256(self.exe.read_bytes()).hexdigest()
        (self.bundle / "build.json").write_text(json.dumps(build))
        self.current = GameProcess(1234, "factorio.exe", str(self.exe), 123456)
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        for target, value in (("playall.games.factorio_attach.BUNDLE", self.bundle),
                              ("playall.games.factorio_attach.list_processes", Mock(return_value=[])),
                              ("playall.processes.inspect_process", Mock(return_value=self.current)),
                              ("playall.games.factorio_attach.tcp_listener_pid", Mock(return_value=1234))):
            self.stack.enter_context(patch(target, value))
        self.emit = Mock()

    def install(self):
        return adapter.prepare_connection(self.directory, self.exe, self.emit)

    def game(self):
        self.install()
        self.fixture = FixtureRcon()
        self.connection = self.stack.enter_context(patch("playall.games.factorio_attach.Rcon", return_value=self.fixture))
        game = adapter.Factorio(self.directory, {"game_process": self.current.selection()}, self.emit)
        self.addCleanup(game.close)
        game.prepare(Mock())
        return game

    def test_install_preserves_other_settings_mods_and_original_bytes_in_backup(self):
        self.install()
        actual = self.config.read_bytes()
        self.assertTrue(actual.startswith(b"\xef\xbb\xbf"))
        self.assertIn(b"; preserved\r\nlocale=zh-CN\r\n", actual)
        self.assertIn(b"full-screen=false\r\n", actual)
        port, password = adapter.local_endpoint(adapter.ini_values(actual))
        self.assertTrue(1 <= port <= 65535 and len(password) >= 32)
        mods = json.loads((self.mods / "mod-list.json").read_text())
        self.assertEqual(mods["mods"][:2], self.original_mods["mods"])
        self.assertEqual(mods["other"], 17)
        backups = list((self.installation / "playall/backups").glob("*/config.ini"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_bytes(), self.original_config)
        self.install()
        self.assertEqual(self.config.read_bytes(), actual)

    def test_running_game_unknown_mod_and_nonlocal_binding_never_get_overwritten(self):
        with patch.object(adapter, "list_processes", return_value=[self.current]):
            with self.assertRaisesRegex(ValueError, "关闭"):
                self.install()
        target = self.mods / adapter.MOD_NAME / "control.lua"
        target.parent.mkdir()
        target.write_text("user-owned unknown file")
        with self.assertRaisesRegex(ValueError, "未知版本"):
            self.install()
        self.assertEqual(self.config.read_bytes(), self.original_config)
        self.assertEqual(target.read_text(), "user-owned unknown file")
        target.unlink()
        self.config.write_bytes(self.original_config + b"local-rcon-socket=0.0.0.0:12345\r\nlocal-rcon-password=existing\r\n")
        before = self.config.read_bytes()
        with self.assertRaises(ValueError):
            self.install()
        self.assertEqual(self.config.read_bytes(), before)

    def test_write_failure_restores_config_mod_list_and_all_old_files(self):
        original_write = adapter.atomic_bytes
        failure = [False]
        def write(path, value):
            if Path(path).name == "control.lua" and not failure[0]:
                failure[0] = True
                raise OSError("synthetic write failure")
            original_write(path, value)
        before = (self.mods / "mod-list.json").read_bytes()
        with patch.object(adapter, "atomic_bytes", side_effect=write), self.assertRaises(OSError):
            self.install()
        self.assertEqual(self.config.read_bytes(), self.original_config)
        self.assertEqual((self.mods / "mod-list.json").read_bytes(), before)
        self.assertFalse((self.mods / adapter.MOD_NAME / "control.lua").exists())

    def test_preparation_is_read_only_and_uses_selected_path_without_launching(self):
        game = self.game()
        files = {path: path.read_bytes() for path in self.installation.rglob("*") if path.is_file()}
        with patch("subprocess.Popen", side_effect=AssertionError("must not launch")), patch("subprocess.run", side_effect=AssertionError("must not launch")):
            game.prepare(Mock())
        self.assertEqual(files, {path: path.read_bytes() for path in self.installation.rglob("*") if path.is_file()})
        self.connection.assert_not_called()
        with self.assertRaisesRegex(ValueError, "亲自"):
            game.prepare(Mock(), new_world=True)

    def test_wrong_version_pid_reuse_and_other_listener_are_rejected(self):
        game = self.game()
        with patch.object(adapter, "tcp_listener_pid", return_value=8888), self.assertRaisesRegex(ValueError, "不属于"):
            game.connect_window(Mock())
        self.connection.assert_not_called()
        with patch("playall.processes.inspect_process", return_value=GameProcess(1234,"factorio.exe",str(self.exe),123457)), self.assertRaisesRegex(ValueError, "重新选择"):
            game.prepare(Mock())
        self.exe.write_bytes(b"unknown version")
        self.assertFalse(adapter.is_supported_process(self.current))

    def test_connect_is_paused_resume_reuses_connection_and_close_only_releases(self):
        game = self.game()
        game.connect_window(Mock())
        self.assertTrue(self.fixture.paused)
        self.assertEqual({r["method"] for r in self.fixture.calls}, {"hello", "claim", "observe"})
        game.connect_window(Mock())  # Runner calls this again on every resume.
        game.resume()
        self.assertFalse(self.fixture.paused)
        result = game.pause()
        self.assertTrue(result["manual_control"] and result["replan"])
        self.assertTrue(self.fixture.paused)
        self.connection.assert_called_once()
        game.close()
        self.assertTrue(self.fixture.closed)
        self.assertIsNone(self.fixture.session)
        self.assertNotIn("checkpoint", {r["method"] for r in self.fixture.calls})

    def test_menu_not_ready_unmatched_reply_and_injected_tools_fail_closed(self):
        game = self.game()
        self.fixture.ready = False
        with self.assertRaises(ValueError):
            game.connect_window(Mock())
        self.assertNotIn("claim", {r["method"] for r in self.fixture.calls})
        game.rcon = self.fixture
        for changed in ({"protocol": 3}, {"id": "other"}, {"session": "other"}, {"ok": 1}):
            self.fixture.corrupt = changed
            with self.subTest(changed=changed), self.assertRaisesRegex(RuntimeError, "不匹配"):
                game.observe()
        self.fixture.corrupt = None
        before = len(self.fixture.calls)
        with self.assertRaises(ValueError):
            game.execute({"action": "wait", "seconds": 1, "lua": "game.player.insert(...)"}, Mock())
        self.assertEqual(before, len(self.fixture.calls))

    def test_heartbeat_fault_stops_future_commands(self):
        game = self.game()
        game.connect_window(Mock())
        game.fault = "synthetic disconnected save"
        before = len(self.fixture.calls)
        with self.assertRaisesRegex(RuntimeError, "synthetic disconnected"):
            game.execute({"action": "mine", "name": "iron-ore"}, SimpleNamespace(check=lambda: None))
        self.assertEqual(before, len(self.fixture.calls))

    def test_save_requires_progress_and_interval_then_confirms_new_game_archive(self):
        game = self.game()
        game.connect_window(Mock())
        game.last_checkpoint = time.monotonic() - 200
        self.assertFalse(game.checkpoint_due())
        game.checkpoint()
        self.assertNotIn("checkpoint", {r["method"] for r in self.fixture.calls})
        self.fixture.observation["position"] = {"x": 1, "y": 0}
        game.observe()
        self.assertTrue(game.checkpoint_due())
        original = self.fixture.command
        def save(command):
            request = json.loads(command.split(" ", 1)[1])
            if request["method"] == "checkpoint":
                path = game.data / "saves" / f"_autosave-playall-{request['value']}.zip"
                path.parent.mkdir(exist_ok=True)
                with zipfile.ZipFile(path, "w") as archive:
                    archive.writestr("test/level.dat", b"synthetic native-save result")
            return original(command)
        self.fixture.command = save
        self.assertTrue(game.checkpoint()["saved"])
        self.assertFalse(game.checkpoint_due())
        count = sum(r["method"] == "checkpoint" for r in self.fixture.calls)
        self.assertTrue(game.checkpoint()["unchanged"])
        self.assertEqual(count, sum(r["method"] == "checkpoint" for r in self.fixture.calls))

    def test_runner_connects_without_ai_or_actions_and_disconnect_preserves_game(self):
        game = self.game()
        self.settings.update({"game": "factorio"})
        runner = Runner(self.settings)
        self.addCleanup(runner.stop)
        with patch("playall.runner.make_game", return_value=game), patch("playall.runner.make_ai", side_effect=AssertionError("no AI before start")):
            runner.start({"game": "factorio", "connect_only": True, "game_process": self.current.selection()})
            deadline = time.monotonic() + 3
            while not runner.snapshot().get("connected") and runner.thread.is_alive() and time.monotonic() < deadline:
                time.sleep(.01)
            self.assertTrue(runner.snapshot().get("connected"), runner.snapshot())
            self.assertEqual(runner.snapshot()["status"], "paused")
            runner.stop()
            runner.thread.join(3)
        self.assertFalse(runner.thread.is_alive())
        self.assertNotIn("execute", {r["method"] for r in self.fixture.calls})
        self.assertNotIn("checkpoint", {r["method"] for r in self.fixture.calls})
        self.assertTrue(self.fixture.closed)

    def test_save_rotation_continues_from_existing_newest_slot_after_reconnect(self):
        game = self.game()
        game.connect_window(Mock())
        saves = game.data / "saves"
        saves.mkdir(exist_ok=True)
        for number, stamp in ((1, 1), (2, 3), (3, 2)):
            path = saves / f"_autosave-playall-{number}.zip"
            path.write_bytes(b"old fixture archive")
            os.utime(path, ns=(stamp * 1_000_000_000, stamp * 1_000_000_000))
        original = self.fixture.command
        def save(command):
            request = json.loads(command.split(" ", 1)[1])
            if request["method"] == "checkpoint":
                path = saves / f"_autosave-playall-{request['value']}.zip"
                with zipfile.ZipFile(path, "w") as archive:
                    archive.writestr("fixture/level.dat", b"saved")
            return original(command)
        self.fixture.command = save
        self.fixture.observation["inventory"] = {"iron-plate": 9}
        self.assertTrue(game.checkpoint()["saved"])
        self.assertEqual(game.save_slot, 3)
        self.assertEqual([r["value"] for r in self.fixture.calls if r["method"] == "checkpoint"], [3])

    def test_failed_mutation_without_observed_progress_does_not_save(self):
        game = self.game()
        game.connect_window(Mock())
        game.resume()
        original = self.fixture.command
        def reject(command):
            response = json.loads(original(command).split("PLAYALL:", 1)[1])
            if json.loads(command.split(" ", 1)[1])["method"] == "execute":
                response["result"] = {"ok": True, "busy": False,
                                      "result": {"ok": False, "message": "synthetic collision"}}
            return "PLAYALL:" + json.dumps(response)
        self.fixture.command = reject
        result = game.execute({"action": "build", "name": "wooden-chest", "x": 4, "y": 4},
                              SimpleNamespace(check=lambda: None))
        self.assertFalse(result["ok"])
        game.last_checkpoint = time.monotonic() - 200
        self.assertFalse(game.checkpoint_due())
        self.assertTrue(game.checkpoint()["unchanged"])
        self.assertNotIn("checkpoint", {r["method"] for r in self.fixture.calls})


@unittest.skipUnless(os.name == "nt", "Windows IP Helper API")
class NativeListenerTests(unittest.TestCase):
    def test_loopback_listener_owner_and_closed_listener(self):
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            sock.listen(1)
            port = sock.getsockname()[1]
            self.assertEqual(tcp_listener_pid(port), os.getpid())
        self.assertIsNone(tcp_listener_pid(port))
        for invalid in (None, True, 0, -1, 65536, "1234"):
            self.assertIsNone(tcp_listener_pid(invalid))


if __name__ == "__main__":
    unittest.main()
