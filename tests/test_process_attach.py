"""Synthetic process identities and loopback-only process attachment tests.

No Windows process is inspected, launched, closed or injected. Native query
calls are replaced with fakes; game behavior is the test_aistalt TCP fixture.
"""
from __future__ import annotations

import ctypes
import json
import os
from pathlib import Path
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from playall.games.aistalt import Aistalt, install_bridge
from playall.processes import GameProcess, inspect_process, list_processes, validate_selection
from playall.runner import Runner
from tests.test_aistalt import RecordingControl, TemporaryGameMixin
from tests.test_runner import GatedAI, response


class ProcessSelectionTests(unittest.TestCase):
    def setUp(self):
        self.path = str(Path("synthetic-processes") / "Aistalt.exe")
        self.current = GameProcess(1234, "Aistalt.exe", self.path, 123456789)
        query = patch("playall.processes.inspect_process", return_value=self.current)
        self.query = query.start()
        self.addCleanup(query.stop)

    def test_selection_roundtrip_is_detached_and_returns_fresh_verified_identity(self):
        selection = self.current.selection()
        self.assertEqual(set(selection), {"pid", "name", "path", "started"})
        verified = validate_selection(selection, "Aistalt.exe")
        self.assertIs(verified, self.current)
        self.query.assert_called_once_with(1234)
        selection["pid"] = 4321
        self.assertEqual(self.current.pid, 1234)

    def test_wrong_shape_pid_creation_time_or_executable_is_rejected_before_inspection(self):
        valid = self.current.selection()
        invalid = [None, [], "1234", {}, {key: value for key, value in valid.items() if key != "started"},
                   {**valid, "command_line": "must not be accepted"}]
        invalid.extend({**valid, **change} for change in (
            {"pid": True}, {"pid": 0}, {"pid": -1}, {"pid": "1234"}, {"pid": 1234.0},
            {"started": True}, {"started": 0}, {"started": -1}, {"started": "123456789"},
            {"path": ""}, {"path": None}, {"path": str(Path("synthetic-processes") / "other.exe")},
            {"name": "other.exe"}, {"name": None},
        ))
        for selection in invalid:
            with self.subTest(selection=selection), self.assertRaises(ValueError):
                validate_selection(selection, "Aistalt.exe")
        self.query.assert_not_called()

    def test_pid_reuse_or_executable_path_change_cannot_reuse_old_selection(self):
        selection = self.current.selection()
        changed = [None, GameProcess(1234, "Aistalt.exe", self.path, self.current.started + 1),
                   GameProcess(1234, "Aistalt.exe", str(Path("different-folder") / "Aistalt.exe"), self.current.started)]
        for current in changed:
            self.query.return_value = current
            with self.subTest(current=current), self.assertRaisesRegex(ValueError, "重新选择"):
                validate_selection(selection, "Aistalt.exe")

    def test_equivalent_path_spelling_and_executable_case_are_accepted(self):
        selection = self.current.selection()
        selection["name"] = "AISTALT.EXE"
        selection["path"] = str(Path("synthetic-processes") / "unused" / ".." / "Aistalt.exe")
        self.assertIs(validate_selection(selection, "Aistalt.exe"), self.current)


class LimitedInformationKernel:
    """Expose only the four identity queries used by inspect_process."""

    def __init__(self):
        self.image = str(Path("synthetic-processes") / "Aistalt.exe")
        self.OpenProcess = Mock(return_value=77)
        self.CloseHandle = Mock(return_value=True)
        self.GetExitCodeProcess = Mock(side_effect=self.exit_code)
        self.QueryFullProcessImageNameW = Mock(side_effect=self.image_name)
        self.GetProcessTimes = Mock(side_effect=self.creation_times)

    def exit_code(self, handle, target):
        target._obj.value = 259
        return True

    def image_name(self, handle, flags, target, size):
        target.value = self.image
        size._obj.value = len(self.image)
        return True

    def creation_times(self, handle, created, exited, kernel, user):
        created._obj.dwHighDateTime = 123
        created._obj.dwLowDateTime = 456
        return True


class ProcessQueryBoundaryTests(unittest.TestCase):
    def setUp(self):
        # Replace the module's platform object, without affecting pathlib/os globally.
        platform = patch("playall.processes.os", SimpleNamespace(name="nt", path=os.path))
        platform.start()
        self.addCleanup(platform.stop)
        self.kernel = LimitedInformationKernel()
        api = patch("playall.processes._kernel", return_value=self.kernel)
        self.api = api.start()
        self.addCleanup(api.stop)

    def test_inspection_requests_only_limited_information_and_closes_handle(self):
        result = inspect_process(1234)
        self.assertEqual(result, GameProcess(1234, "Aistalt.exe", self.kernel.image, (123 << 32) | 456))
        self.kernel.OpenProcess.assert_called_once_with(0x1000, False, 1234)
        self.kernel.CloseHandle.assert_called_once_with(77)
        self.assertEqual(self.kernel.QueryFullProcessImageNameW.call_args.args[1], 0)

    def test_invalid_pids_never_open_any_native_process(self):
        for pid in (None, True, False, 0, -1, "1234", 12.0):
            with self.subTest(pid=pid):
                self.assertIsNone(inspect_process(pid))
        self.api.assert_not_called()
        self.kernel.OpenProcess.assert_not_called()

    def test_denied_access_returns_none_without_closing_an_unowned_handle(self):
        self.kernel.OpenProcess.return_value = None
        self.assertIsNone(inspect_process(1234))
        self.kernel.CloseHandle.assert_not_called()

    def test_exited_process_or_failed_identity_queries_always_release_handle(self):
        for failing_query in ("GetExitCodeProcess", "QueryFullProcessImageNameW", "GetProcessTimes"):
            kernel = LimitedInformationKernel()
            getattr(kernel, failing_query).side_effect = None
            getattr(kernel, failing_query).return_value = False
            with self.subTest(query=failing_query), patch("playall.processes._kernel", return_value=kernel):
                self.assertIsNone(inspect_process(1234))
                kernel.CloseHandle.assert_called_once_with(77)
        kernel = LimitedInformationKernel()

        def exited(handle, code):
            code._obj.value = 0
            return True

        kernel.GetExitCodeProcess.side_effect = exited
        with patch("playall.processes._kernel", return_value=kernel):
            self.assertIsNone(inspect_process(1234))
            kernel.QueryFullProcessImageNameW.assert_not_called()
            kernel.CloseHandle.assert_called_once_with(77)

    def test_process_list_queries_names_and_pids_without_opening_processes(self):
        rows = [(0, "System"), (7, "Zeta.exe"), (8, "alpha.exe"), (6, "ALPHA.exe")]
        cursor = [-1]

        def read_row(handle, entry):
            cursor[0] += 1
            if cursor[0] >= len(rows):
                return False
            entry._obj.th32ProcessID, entry._obj.szExeFile = rows[cursor[0]]
            return True

        kernel = SimpleNamespace(CreateToolhelp32Snapshot=Mock(return_value=88),
                                 Process32FirstW=Mock(side_effect=read_row), Process32NextW=Mock(side_effect=read_row),
                                 CloseHandle=Mock(), OpenProcess=Mock(side_effect=AssertionError("Do not inspect every process")))
        with patch("playall.processes._kernel", return_value=kernel), \
             patch("playall.processes.inspect_process", side_effect=AssertionError("Do not inspect every process")):
            result = list_processes()
        self.assertEqual([(process.pid, process.name) for process in result], [(6, "ALPHA.exe"), (8, "alpha.exe"), (7, "Zeta.exe")])
        self.assertTrue(all(process.path == "" and process.started == 0 for process in result))
        kernel.CreateToolhelp32Snapshot.assert_called_once_with(2, 0)
        kernel.OpenProcess.assert_not_called()
        kernel.CloseHandle.assert_called_once_with(88)

    def test_failed_process_snapshot_is_explicit_and_never_closed_as_a_valid_handle(self):
        kernel = SimpleNamespace(CreateToolhelp32Snapshot=Mock(return_value=ctypes.c_void_p(-1).value),
                                 Process32FirstW=Mock(), Process32NextW=Mock(), CloseHandle=Mock())
        with patch("playall.processes._kernel", return_value=kernel), self.assertRaises(OSError):
            list_processes()
        kernel.Process32FirstW.assert_not_called()
        kernel.CloseHandle.assert_not_called()


class ProcessAttachTests(TemporaryGameMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.identity = GameProcess(1234, "Aistalt.exe", str(self.exe), 1)
        self.current_identity = self.identity
        query = patch("playall.processes.inspect_process", side_effect=lambda pid: self.current_identity if pid == 1234 else None)
        query.start()
        self.addCleanup(query.stop)
        validation = patch("playall.games.aistalt.validate_selection", side_effect=validate_selection)
        validation.start()
        self.addCleanup(validation.stop)
        native = patch("playall.processes._kernel", side_effect=AssertionError("No native processes may be inspected"))
        native.start()
        self.addCleanup(native.stop)

    def selected_adapter(self, bridge):
        # Only fake bytes in a temporary game directory; no actual installation.
        install_bridge(self.exe, self.bundle)
        adapter = Aistalt(self.directory, {"game_process": self.identity.selection(),
                                          "aistalt_path": str(self.directory / "ignored-default" / "Aistalt.exe")},
                          lambda *args, **kwargs: self.events.append((args, kwargs)))
        adapter.bundle = self.bundle
        adapter.prepare(RecordingControl())
        self.addCleanup(adapter.close)
        self.write_endpoint(adapter, bridge)
        return adapter

    def runner_for(self, adapter, ai, *, remote_without_key=False):
        endpoint = "https://example.invalid/v1" if remote_without_key else "http://127.0.0.1:1"
        self.settings.update({"provider": "custom", "game": "aistalt", "aistalt_path": str(self.exe),
                              "step_delay": 0, "output_tokens": 256,
                              "profiles": [{"id": "custom", "base_url": endpoint}]})
        provider = patch("playall.runner.make_ai", return_value=ai)
        provider_mock = provider.start()
        self.addCleanup(provider.stop)

        def create(game_id, directory, config, emit):
            self.assertEqual(config["game_process"], self.identity.selection())
            adapter.emit = emit
            return adapter

        factory = patch("playall.runner.make_game", side_effect=create)
        factory.start()
        self.addCleanup(factory.stop)
        runner = Runner(self.settings, connect_game_window=True)
        self.addCleanup(self.stop_runner, runner, ai)
        return runner, provider_mock

    def stop_runner(self, runner, ai):
        if runner.thread and runner.thread.is_alive():
            runner.stop()
            ai.release_all()
            runner.thread.join(3)
            self.assertFalse(runner.thread.is_alive(), "Attachment test leaked a runner thread")

    def connect_only(self, runner):
        runner.start({"connect_only": True, "game_process": self.identity.selection()})
        self.await_condition(lambda: runner.snapshot()["status"] in ("paused", "error"), "Connect-only did not settle")
        self.assertEqual(runner.snapshot()["status"], "paused", runner.snapshot()["message"])
        self.assertTrue(runner.snapshot()["connected"])
        self.assertFalse(runner.snapshot()["ai_started"])
        self.assertFalse(runner.resume_event.is_set())

    def assert_no_process_metadata(self, messages):
        forbidden = {"pid", "started", "game_process", "process_list", "local_processes", "aistalt_path"}
        private_paths = [str(self.exe), str(self.directory / "private-tool" / "secret.exe")]

        def inspect(value):
            if isinstance(value, dict):
                self.assertFalse(forbidden.intersection(value), f"Process identity leaked into model data: {value.keys()}")
                for item in value.values():
                    inspect(item)
            elif isinstance(value, list):
                for item in value:
                    inspect(item)
            elif isinstance(value, str):
                for path in private_paths:
                    self.assertNotIn(path.casefold(), value.casefold())
                self.assertNotIn("private-tool.exe", value)
                try:
                    decoded = json.loads(value)
                except ValueError:
                    return
                if isinstance(decoded, (dict, list)):
                    inspect(decoded)

        inspect(messages)

    def test_selected_process_path_wins_and_preparation_never_modifies_live_game_install(self):
        bridge = self.make_bridge()
        adapter = self.selected_adapter(bridge)
        before = self.files()
        with patch("playall.games.aistalt.install_bridge", side_effect=AssertionError("Do not modify a running game")):
            adapter.prepare(RecordingControl())
        self.assertEqual(adapter.exe, self.exe)
        self.assertEqual(self.files(), before)

    def test_endpoint_pid_mismatch_is_rejected_before_any_network_connection(self):
        bridge = self.make_bridge()
        adapter = self.selected_adapter(bridge)
        self.write_endpoint(adapter, bridge, pid=4321)
        with patch("playall.games.aistalt.socket.create_connection") as connect:
            with self.assertRaisesRegex(RuntimeError, "另一个游戏进程"):
                adapter.observe()
            connect.assert_not_called()
        self.assertEqual(bridge.requests, [])
        self.assertIsNone(adapter.connection)

    def test_process_reuse_or_exit_is_rechecked_before_socket_connection(self):
        bridge = self.make_bridge()
        adapter = self.selected_adapter(bridge)
        changed = [None, GameProcess(1234, "Aistalt.exe", str(self.exe), 2),
                   GameProcess(1234, "Aistalt.exe", str(self.directory / "other" / "Aistalt.exe"), 1)]
        with patch("playall.games.aistalt.socket.create_connection") as connect:
            for identity in changed:
                self.current_identity = identity
                with self.subTest(identity=identity), self.assertRaises(ValueError):
                    adapter.observe()
            connect.assert_not_called()
        self.assertEqual(bridge.requests, [])

    def test_connect_only_stays_paused_without_ai_resume_or_actions_and_stop_preserves_game(self):
        bridge = self.make_bridge()
        adapter = self.selected_adapter(bridge)
        adapter.viewer = viewer = Mock()
        ai = GatedAI()
        runner, provider = self.runner_for(adapter, ai)
        self.connect_only(runner)
        provider.assert_not_called()
        self.assertEqual(ai.calls, [])
        self.assertNotIn("resume", bridge.methods())
        self.assertNotIn("execute", bridge.methods())
        self.assertTrue(bridge.paused)
        self.stop_runner(runner, ai)
        self.assertEqual(runner.snapshot()["status"], "stopped")
        provider.assert_not_called()
        self.assertEqual(bridge.methods()[-1], "detach")
        self.assertIsNone(adapter.connection)
        viewer.terminate.assert_not_called()
        viewer.kill.assert_not_called()

    def test_connect_only_needs_no_ai_key_and_explicit_resume_checks_key_before_control(self):
        bridge = self.make_bridge()
        adapter = self.selected_adapter(bridge)
        ai = GatedAI()
        runner, provider = self.runner_for(adapter, ai, remote_without_key=True)
        self.connect_only(runner)
        with self.assertRaisesRegex(ValueError, "API Key"):
            runner.resume()
        self.assertEqual(runner.snapshot()["status"], "paused")
        provider.assert_not_called()
        self.assertNotIn("resume", bridge.methods())
        self.assertNotIn("execute", bridge.methods())

    def test_explicit_resume_starts_ai_without_executable_identity_or_process_list_in_messages(self):
        bridge = self.make_bridge()
        adapter = self.selected_adapter(bridge)
        ai = GatedAI([response({"action": "wait", "seconds": .01})], gates=(2,))
        runner, provider = self.runner_for(adapter, ai)
        self.connect_only(runner)
        provider.assert_not_called()
        # Local selection data remains available to reconnect; it must never be
        # serialized wholesale into model messages, including unrelated processes.
        runner.config["process_list"] = [self.identity.selection(),
            GameProcess(9876, "private-tool.exe", str(self.directory / "private-tool" / "secret.exe"), 9).selection()]
        runner.resume()
        self.assertTrue(ai.entered[2].wait(3), "AI did not start after explicit Resume")
        self.assertTrue(runner.snapshot()["ai_started"])
        self.assertEqual(bridge.methods().count("resume"), 1)
        self.assertEqual(bridge.methods().count("execute"), 1)
        for messages, tools, output_tokens in ai.calls:
            self.assert_no_process_metadata(messages)
        self.assertEqual(runner.config["game_process"], self.identity.selection())
        self.stop_runner(runner, ai)
        session = json.loads((self.directory / "sessions/aistalt.json").read_text(encoding="utf-8"))
        self.assert_no_process_metadata(session["history"])

    def test_resume_rejects_reused_pid_even_when_existing_socket_still_replies(self):
        bridge = self.make_bridge()
        adapter = self.selected_adapter(bridge)
        ai = GatedAI()
        runner, provider = self.runner_for(adapter, ai)
        self.connect_only(runner)
        self.current_identity = GameProcess(1234, "Aistalt.exe", str(self.exe), 2)
        with self.assertRaisesRegex(ValueError, "重新选择"):
            runner.resume()
        self.assertEqual(runner.snapshot()["status"], "paused")
        self.assertFalse(runner.resume_event.is_set())
        provider.assert_not_called()
        self.assertNotIn("resume", bridge.methods())
        self.assertNotIn("execute", bridge.methods())


if __name__ == "__main__":
    unittest.main()
