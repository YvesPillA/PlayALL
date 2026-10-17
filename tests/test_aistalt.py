"""Offline adapter tests; the bridge is synthetic, never the installed game.

The fixture exercises real loopback TCP framing and the Python lifecycle. It
does not establish that Unity/FairyGUI events or native game saves work.
"""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
import socketserver
import threading
import time
import unittest
from contextlib import contextmanager
from unittest.mock import Mock, patch

from playall.games.aistalt import Aistalt, install_bridge, sha256
from playall.games.aistalt_actions import tools_for_game, validate_action
from playall.runner import Runner
from tests.support import IsolatedSettingsMixin
from tests.test_runner import GatedAI, response


DROP_CONNECTION = object()
GAME_BYTES = b"synthetic game assembly; not executable"
GAME_HASH = hashlib.sha256(GAME_BYTES).hexdigest()
TOKEN, INSTANCE = "a" * 64, "b" * 32


class RecordingControl:
    def __init__(self):
        self.sleeps = []
        self.stopped = False

    def check(self):
        if self.stopped:
            raise InterruptedError("synthetic stop")

    @contextmanager
    def action(self):
        self.check()
        yield

    def sleep(self, seconds):
        self.check()
        self.sleeps.append(seconds)


class LoopbackBridge:
    """A newline JSON server implementing only synthetic bridge state."""

    def __init__(self, responder=None, *, ready=True, fragment_size=None):
        self.lock = threading.RLock()
        self.requests, self.applied_actions, self.errors = [], [], []
        self.responder = responder
        self.ready, self.paused = ready, True
        self.revision = 5
        self.fragment_size = fragment_size
        self.connections = 0
        self.marker = "fixture state"
        fixture = self

        class Handler(socketserver.StreamRequestHandler):
            def handle(self):
                with fixture.lock:
                    fixture.connections += 1
                try:
                    while raw := self.rfile.readline(1024 * 1024 + 1):
                        request = json.loads(raw)
                        with fixture.lock:
                            fixture.requests.append(copy.deepcopy(request))
                            result = fixture.responder(request) if fixture.responder else fixture.reply(request)
                        if result is DROP_CONNECTION:
                            return
                        if isinstance(result, bytes):
                            encoded = result
                        else:
                            result = dict(result)
                            result.setdefault("id", request["id"])
                            result.setdefault("instance", INSTANCE)
                            encoded = json.dumps(result, ensure_ascii=False, allow_nan=False).encode("utf-8") + b"\n"
                        size = fixture.fragment_size or len(encoded)
                        for offset in range(0, len(encoded), size):
                            self.wfile.write(encoded[offset:offset + size])
                            self.wfile.flush()
                        if isinstance(result, bytes) and not encoded.endswith(b"\n"):
                            return
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                    pass  # Expected when the adapter rejects a malformed reply.
                except BaseException as error:
                    with fixture.lock:
                        fixture.errors.append(error)

        class Server(socketserver.ThreadingTCPServer):
            allow_reuse_address = True
            daemon_threads = True
            block_on_close = False

        self.server = Server(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": .01},
                                       name="Synthetic Aistalt loopback bridge", daemon=True)
        self.thread.start()

    def reply(self, request):
        method = request["method"]
        if request["token"] != TOKEN or request["instance"] != INSTANCE:
            return {"ok": False, "error": "synthetic authentication failure"}
        if method in ("hello", "ping"):
            return {"ok": True, "protocol": 1, "game": "aistalt", "game_sha256": GAME_HASH,
                    "ready": self.ready, "paused": self.paused}
        if method == "observe":
            return {"ok": True, "game": "aistalt", "view_revision": self.revision,
                    "window_connected": self.ready, "paused": self.paused, "visible_text": [self.marker],
                    "controls": [{"id": "fixture-option", "enabled": True, "text": "继续故事"}]}
        if method in ("pause", "detach", "resume"):
            self.paused = method != "resume"
            self.revision += 1
            return {"ok": True, "replan": True, "manual_control": self.paused}
        if method == "checkpoint":
            return {"ok": True, "saved": True, "message": "synthetic save only", "marker": self.marker}
        if method == "review":
            return {"ok": True, "requires_confirmation": False}
        if method == "execute":
            action = request["action"]
            if self.paused:
                return {"ok": False, "error": "synthetic bridge is paused"}
            if action["action"] not in ("observe", "wait"):
                if action["view_revision"] != self.revision:
                    return {"ok": False, "error": "synthetic stale view_revision"}
                self.applied_actions.append(copy.deepcopy(action))
                self.revision += 1
            return {"ok": True, "message": "synthetic action completed", "view_revision": self.revision}
        raise AssertionError(f"Unexpected synthetic bridge method: {method}")

    def methods(self):
        with self.lock:
            return [request["method"] for request in self.requests]

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)
        if self.thread.is_alive():
            raise AssertionError("Synthetic bridge leaked a server thread")
        if self.errors:
            raise AssertionError(f"Synthetic bridge handler failed: {self.errors!r}")


class TemporaryGameMixin(IsolatedSettingsMixin):
    def setUp(self):
        # Install detection must not even inspect the user's real game path.
        detection = patch("playall.config.detect_aistalt", return_value="", create=True)
        detection.start()
        self.addCleanup(detection.stop)
        super().setUp()
        for blocker in (
            patch("playall.games.aistalt.subprocess.Popen", side_effect=AssertionError("Do not launch any game")),
            patch("playall.games.aistalt.game_is_open", return_value=False),
            patch("playall.games.aistalt.validate_selection", side_effect=lambda value, name: value),
            patch("playall.llm.CompatibleAI._request", side_effect=AssertionError("Do not call an external AI")),
        ):
            blocker.start()
            self.addCleanup(blocker.stop)
        self.exe = self.directory / "synthetic-game" / "Aistalt.exe"
        self.exe.parent.mkdir()
        self.exe.write_bytes(b"not an executable")
        assembly = self.exe.parent / "Aistalt_Data/Managed/InfantGod.dll"
        assembly.parent.mkdir(parents=True)
        assembly.write_bytes(GAME_BYTES)
        self.bundle = self.directory / "synthetic-bundle"
        for relative, content in {
            "loader/winhttp.dll": b"synthetic loader",
            "loader/doorstop_config.ini": b"synthetic config",
            "loader/BepInEx/core/BepInEx.dll": b"synthetic runtime",
            "PlayALL.Aistalt.dll": b"synthetic plugin",
        }.items():
            target = self.bundle / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
        (self.bundle / "build.json").write_text(json.dumps({"game_sha256": GAME_HASH,
            "bridge_sha256": sha256(self.bundle / "PlayALL.Aistalt.dll")}), encoding="utf-8")
        self.events = []

    def make_bridge(self, **options):
        bridge = LoopbackBridge(**options)
        self.addCleanup(bridge.close)
        return bridge

    def make_adapter(self, bridge=None):
        install_bridge(self.exe, self.bundle)
        adapter = Aistalt(self.directory, {"aistalt_path": str(self.exe),
                          "game_process": {"pid": 1234, "name": "Aistalt.exe", "path": str(self.exe), "started": 1}},
                          lambda *args, **kwargs: self.events.append((args, kwargs)))
        adapter.bundle = self.bundle
        adapter.prepare(RecordingControl())
        self.addCleanup(adapter.close)
        if bridge is not None:
            self.write_endpoint(adapter, bridge)
        return adapter

    def write_endpoint(self, adapter, bridge, **changes):
        endpoint = {"protocol": 1, "game": "aistalt", "game_sha256": GAME_HASH, "headless": False,
                    "pid": 1234, "port": bridge.port, "token": TOKEN, "instance": INSTANCE}
        endpoint.update(changes)
        adapter.endpoint_path.write_text(json.dumps(endpoint), encoding="utf-8")

    def files(self):
        return {str(path.relative_to(self.exe.parent)): path.read_bytes()
                for path in self.exe.parent.rglob("*") if path.is_file()}

    def await_condition(self, predicate, message):
        deadline = time.monotonic() + 3
        while not predicate():
            if time.monotonic() >= deadline:
                self.fail(message)
            threading.Event().wait(.005)


class AistaltActionTests(unittest.TestCase):
    def test_supported_actions_keep_only_validated_game_fields(self):
        actions = [
            {"action": "observe"}, {"action": "wait", "seconds": .25}, {"action": "wait", "seconds": 30},
            {"action": "advance", "view_revision": 0},
            {"action": "click", "control_id": "a/b:option", "view_revision": 17},
            {"action": "text", "control_id": "name", "view_revision": 17, "text": "你好幼神", "submit": True},
            {"action": "text", "control_id": "name", "view_revision": 17, "text": ""},
            {"action": "scroll", "control_id": "list", "view_revision": 17, "axis": "x", "amount": -1},
            {"action": "scroll", "control_id": "list", "view_revision": 17, "axis": "y", "amount": .5},
            {"action": "set_value", "control_id": "volume", "view_revision": 17, "value": 42.5},
        ]
        for action in actions:
            with self.subTest(action=action):
                result = validate_action(action)
                self.assertEqual(result, action)
                self.assertIsNot(result, action)

    def test_unknown_actions_fields_and_nonobject_payloads_are_rejected(self):
        invalid = [None, [], "click", {}, {"action": []}, {"action": {}}, {"action": True},
                   {"action": "eval", "code": "arbitrary()"}, {"action": "observe", "code": "anything"},
                   {"action": "click", "control_id": "button", "view_revision": 1, "seconds": 1},
                   {"action": "wait", "seconds": 1, "view_revision": 1}]
        for action in invalid:
            with self.subTest(action=action), self.assertRaises(ValueError):
                validate_action(action)

    def test_mutations_require_exact_nonnegative_revision_and_observed_control(self):
        for revision in (None, True, False, -1, 1.0, "1"):
            for action in ("advance", "click"):
                payload = {"action": action, "view_revision": revision}
                if action == "click":
                    payload["control_id"] = "button"
                with self.subTest(payload=payload), self.assertRaises(ValueError):
                    validate_action(payload)
        for control in (None, True, 7, [], "", "x" * 129):
            with self.subTest(control=control), self.assertRaises(ValueError):
                validate_action({"action": "click", "control_id": control, "view_revision": 1})

    def test_text_has_length_and_boolean_submit_limits(self):
        base = {"action": "text", "control_id": "name", "view_revision": 1, "text": "x" * 8000}
        self.assertEqual(len(validate_action(base)["text"]), 8000)
        for change in ({"text": "x" * 8001}, {"text": None}, {"text": 4}, {"submit": 1}, {"submit": "true"}):
            with self.subTest(change=str(change)[:90]), self.assertRaises(ValueError):
                validate_action({**base, **change})

    def test_wait_scroll_and_slider_reject_nonfinite_or_boolean_numbers(self):
        for number in (None, True, False, float("nan"), float("inf"), float("-inf"), "0.5"):
            for action, key in (("wait", "seconds"), ("scroll", "amount"), ("set_value", "value")):
                payload = {"action": action, key: number}
                if action != "wait":
                    payload.update(control_id="control", view_revision=1, axis="y")
                    if action == "set_value":
                        payload.pop("axis")
                with self.subTest(action=action, number=number), self.assertRaises(ValueError):
                    validate_action(payload)
        for seconds in (-1, 0, 30.001):
            with self.subTest(seconds=seconds), self.assertRaises(ValueError):
                validate_action({"action": "wait", "seconds": seconds})
        for axis, amount in (("z", .5), (True, .5), ("x", 0), ("y", 1.001), ("x", -1.001)):
            with self.subTest(axis=axis, amount=amount), self.assertRaises(ValueError):
                validate_action({"action": "scroll", "control_id": "list", "view_revision": 1,
                                 "axis": axis, "amount": amount})

    def test_tools_expose_only_supported_game_actions_and_progress(self):
        tools = tools_for_game()
        self.assertEqual([tool["function"]["name"] for tool in tools], ["game_action", "report_progress"])
        schema = tools[0]["function"]["parameters"]
        self.assertEqual(set(schema["properties"]["action"]["enum"]),
                         {"observe", "wait", "advance", "click", "text", "scroll", "set_value"})
        self.assertFalse(schema["additionalProperties"])
        self.assertNotIn("code", schema["properties"])


class AistaltProtocolTests(TemporaryGameMixin, unittest.TestCase):
    def test_endpoint_identity_is_validated_before_opening_any_socket(self):
        bridge, adapter = self.make_bridge(), self.make_adapter()
        invalid = [
            {"protocol": True}, {"protocol": 2}, {"game": "factorio"}, {"game_sha256": "0" * 64},
            {"headless": True}, {"headless": 0}, {"headless": None},
            {"pid": True}, {"pid": 0}, {"pid": -1}, {"pid": "1234"},
            {"port": True}, {"port": 0}, {"port": 65536}, {"port": "3210"},
            {"token": "a" * 63}, {"token": "z" * 64}, {"token": None},
            {"instance": "b" * 31}, {"instance": "z" * 32}, {"instance": None},
        ]
        with patch("playall.games.aistalt.socket.create_connection") as connect:
            for change in invalid:
                self.write_endpoint(adapter, bridge, **change)
                with self.subTest(change=change), self.assertRaises((RuntimeError, ValueError)):
                    adapter.observe()
                self.assertIsNone(adapter.connection)
            for payload in ([], None, "bridge", 17):
                adapter.endpoint_path.write_text(json.dumps(payload), encoding="utf-8")
                with self.subTest(payload=payload), self.assertRaises((RuntimeError, ValueError)):
                    adapter.observe()
            adapter.endpoint_path.write_text(" " * 8193, encoding="utf-8")
            with self.assertRaises(RuntimeError):
                adapter.observe()
            adapter.endpoint_path.unlink()
            with self.assertRaises(RuntimeError):
                adapter.observe()
            connect.assert_not_called()

    def test_persistent_requests_have_controller_instance_auth_and_unique_ids(self):
        bridge = self.make_bridge(fragment_size=3)
        bridge.marker = "真实回环数据：幼神故事 🌱"
        adapter = self.make_adapter(bridge)
        self.assertTrue(adapter.window_ready())
        self.assertEqual(adapter.observe()["visible_text"], [bridge.marker])
        self.assertTrue(adapter._call("ping")["ready"])
        self.assertEqual(bridge.connections, 1)
        self.assertEqual(bridge.methods(), ["hello", "observe", "ping"])
        ids = []
        for request in bridge.requests:
            self.assertEqual(request["token"], TOKEN)
            self.assertEqual(request["instance"], INSTANCE)
            self.assertEqual(request["controller"], adapter.controller)
            self.assertEqual(len(request["id"]), 32)
            ids.append(request["id"])
        self.assertEqual(len(set(ids)), 3)

    def test_hello_and_ping_reject_other_game_version_and_nonboolean_readiness(self):
        bridge = self.make_bridge()
        adapter = self.make_adapter(bridge)
        changes = [{"protocol": True}, {"protocol": 2}, {"game": "factorio"},
                   {"game_sha256": "0" * 64}, {"ready": 1}, {"ready": "true"}, {"ready": None}]
        for method in ("hello", "ping"):
            for change in changes:
                bridge.responder = lambda request, change=change: {**bridge.reply(request), **change}
                with self.subTest(method=method, change=change), self.assertRaises(RuntimeError):
                    adapter._call(method)
                self.assertIsNone(adapter.connection)
        bridge.responder = lambda request: {**bridge.reply(request), "ready": "false"}
        self.assertFalse(adapter.window_ready())
        self.assertIsNone(adapter.connection)

    def test_mismatched_request_or_instance_and_nonboolean_ok_disconnect(self):
        bridge = self.make_bridge()
        adapter = self.make_adapter(bridge)
        invalid = [{"id": "unrelated-request"}, {"instance": "c" * 32}, {"ok": 1}, {"ok": "true"}, {"ok": None}]
        for change in invalid:
            bridge.responder = lambda request, change=change: {"ok": True, **change}
            with self.subTest(change=change), self.assertRaises(RuntimeError):
                adapter.observe()
            self.assertIsNone(adapter.connection)
            self.assertIsNone(adapter.reader)

    def test_malformed_incomplete_and_oversized_json_replies_disconnect(self):
        bridge = self.make_bridge()
        adapter = self.make_adapter(bridge)
        for raw in (b"not json\n", b"[]\n", b"null\n", b'{"ok":true}', b"x" * (1024 * 1024 + 1) + b"\n"):
            bridge.responder = lambda request, raw=raw: raw
            with self.subTest(kind=raw[:30]), self.assertRaises((RuntimeError, ValueError)):
                adapter.observe()
            self.assertIsNone(adapter.connection)
            self.assertIsNone(adapter.reader)

    def test_authentication_failure_is_not_treated_as_observation_success(self):
        bridge = self.make_bridge(responder=lambda request: {"ok": False, "error": "synthetic wrong token"})
        adapter = self.make_adapter(bridge)
        with self.assertRaisesRegex(RuntimeError, "synthetic wrong token"):
            adapter.observe()
        self.assertIsNone(adapter.connection)
        self.assertEqual(bridge.methods(), ["observe"])

    def test_disconnect_after_submitted_action_never_retries_and_still_requires_save(self):
        bridge = self.make_bridge()
        adapter = self.make_adapter(bridge)
        adapter._call("resume")
        action = {"action": "click", "control_id": "fixture-option", "view_revision": bridge.revision}

        def deliver_then_drop(request):
            result = bridge.reply(request)
            return DROP_CONNECTION if request["method"] == "execute" else result

        bridge.responder = deliver_then_drop
        with self.assertRaises((RuntimeError, OSError)):
            adapter.execute(action, RecordingControl())
        self.assertEqual(bridge.applied_actions, [action], "Submitted click was unexpectedly repeated")
        self.assertEqual(bridge.methods().count("execute"), 1)
        self.assertIsNone(adapter.connection)
        self.assertTrue(adapter.mutated, "Unacknowledged delivery lost the need to save")
        self.assertFalse(adapter.saved)
        bridge.responder = None
        saved = adapter.checkpoint()
        self.assertTrue(saved["saved"])
        self.assertEqual(bridge.methods().count("execute"), 1)
        self.assertEqual(bridge.connections, 2)
        self.assertTrue((adapter.directory / "last-save.json").is_file())

    def test_confirmation_denial_of_resume_preserves_transport_and_stays_paused(self):
        bridge = self.make_bridge()
        blocked = [True]
        def responder(request):
            if request["method"] == "resume" and blocked[0]:
                return {"ok": False, "reason": "awaiting_user", "error": "waiting fixture confirmation"}
            return bridge.reply(request)
        bridge.responder = responder
        adapter = self.make_adapter(bridge)
        self.assertTrue(adapter.window_ready())
        connection = adapter.connection
        with self.assertRaisesRegex(ValueError, "confirmation"):
            adapter.resume()
        self.assertIs(adapter.connection, connection, "An expected refusal must not release the pending native lease")
        self.assertTrue(bridge.paused)
        blocked[0] = False
        self.assertTrue(adapter.resume()["ok"])
        self.assertIs(adapter.connection, connection)

    def test_paused_and_stale_actions_return_failure_without_replay_then_resume_replans(self):
        bridge = self.make_bridge()
        adapter = self.make_adapter(bridge)
        control = RecordingControl()
        paused = adapter.execute({"action": "click", "control_id": "fixture-option", "view_revision": bridge.revision}, control)
        self.assertFalse(paused["ok"])
        self.assertEqual(bridge.applied_actions, [])
        resumed = adapter.resume()
        self.assertTrue(resumed["replan"])
        self.assertFalse(bridge.paused)
        stale = adapter.execute({"action": "click", "control_id": "fixture-option", "view_revision": 0}, control)
        self.assertFalse(stale["ok"])
        self.assertEqual(bridge.applied_actions, [])
        observation = adapter.observe()
        action = {"action": "click", "control_id": "fixture-option", "view_revision": observation["view_revision"]}
        self.assertTrue(adapter.execute(action, control)["ok"])
        paused = adapter.pause()
        self.assertTrue(paused["manual_control"])
        self.assertTrue(paused["replan"])
        self.assertTrue(bridge.paused)
        bridge.marker = "changed by a synthetic human"
        self.assertTrue(adapter.resume()["replan"])
        self.assertEqual(adapter.observe()["visible_text"], [bridge.marker])
        self.assertEqual(bridge.applied_actions, [action])
        self.assertEqual(bridge.methods().count("execute"), 3)

    def test_observe_and_wait_are_not_mutations_and_wait_uses_interruptible_control(self):
        bridge = self.make_bridge()
        adapter = self.make_adapter(bridge)
        adapter._call("resume")
        control = RecordingControl()
        self.assertTrue(adapter.execute({"action": "observe"}, control)["ok"])
        self.assertTrue(adapter.execute({"action": "wait", "seconds": 2.5}, control)["ok"])
        self.assertEqual(control.sleeps, [2.5])
        self.assertFalse(adapter.mutated)
        self.assertEqual(bridge.applied_actions, [])
        control.stopped = True
        before = len(bridge.requests)
        with self.assertRaises(InterruptedError):
            adapter.execute({"action": "wait", "seconds": 1}, control)
        self.assertEqual(len(bridge.requests), before)

    def test_invalid_action_never_crosses_the_tcp_boundary(self):
        bridge = self.make_bridge()
        adapter = self.make_adapter(bridge)
        with self.assertRaises(ValueError):
            adapter.execute({"action": "click", "control_id": "fixture-option", "view_revision": True}, RecordingControl())
        self.assertEqual(bridge.requests, [])
        self.assertFalse(adapter.mutated)

    def test_close_detaches_heartbeat_and_keeps_user_game_process_alive(self):
        bridge = self.make_bridge()
        adapter = self.make_adapter(bridge)
        viewer = adapter.viewer = Mock()
        adapter.resume()
        self.assertTrue(adapter.heartbeat_thread.is_alive())
        adapter.close()
        self.assertTrue(bridge.paused)
        self.assertEqual(bridge.methods()[-1], "detach")
        self.assertFalse(adapter.heartbeat_thread.is_alive())
        self.assertIsNone(adapter.connection)
        viewer.terminate.assert_not_called()
        viewer.kill.assert_not_called()
        before = len(bridge.requests)
        adapter.close()
        self.assertEqual(len(bridge.requests), before)

    def test_disconnected_unmodified_checkpoint_does_not_open_connection(self):
        adapter = self.make_adapter()
        with patch("playall.games.aistalt.socket.create_connection") as connect:
            self.assertEqual(adapter.checkpoint(), {"ok": True, "saved": False})
            self.assertTrue(adapter.pause()["manual_control"])
            connect.assert_not_called()

    def test_checkpoint_requires_boolean_save_confirmation_and_does_not_claim_pending_save(self):
        bridge = self.make_bridge()
        adapter = self.make_adapter(bridge)
        adapter.mutated = True
        for saved in ("false", "true", 0, 1, None):
            bridge.responder = lambda request, saved=saved: (
                {"ok": True, "saved": saved} if request["method"] == "checkpoint" else bridge.reply(request))
            with self.subTest(saved=saved), self.assertRaises(RuntimeError):
                adapter.checkpoint()
            self.assertFalse(adapter.saved, "A malformed saved flag was promoted to a confirmed game save")
            self.assertFalse((adapter.directory / "last-save.json").exists())
        bridge.responder = lambda request: (
            {"ok": True, "saved": False, "pending": True} if request["method"] == "checkpoint" else bridge.reply(request))
        self.assertEqual(adapter.checkpoint()["saved"], False)
        self.assertFalse(adapter.saved)
        self.assertFalse((adapter.directory / "last-save.json").exists())


class AistaltInstallTests(TemporaryGameMixin, unittest.TestCase):
    def test_install_records_owned_hashes_and_repeated_install_is_idempotent(self):
        plugin = install_bridge(self.exe, self.bundle)
        manifest = json.loads((plugin / "install.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["version"], 1)
        self.assertEqual(set(manifest["files"]), {"winhttp.dll", "doorstop_config.ini", "BepInEx/core/BepInEx.dll",
                                                "BepInEx/plugins/PlayALL/PlayALL.Aistalt.dll"})
        for relative, expected in manifest["files"].items():
            self.assertEqual(sha256(self.exe.parent / relative), expected)
        before = self.files()
        with patch("playall.games.aistalt.shutil.copy2", side_effect=AssertionError("Identical file was rewritten")):
            self.assertEqual(install_bridge(self.exe, self.bundle), plugin)
        self.assertEqual(self.files(), before)

    def test_late_plugin_conflict_is_found_before_any_loader_file_is_written(self):
        target = self.exe.parent / "BepInEx/plugins/PlayALL/PlayALL.Aistalt.dll"
        target.parent.mkdir(parents=True)
        target.write_bytes(b"unrecognized existing plugin")
        before = self.files()
        with patch("playall.games.aistalt.shutil.copy2") as copy_file:
            with self.assertRaisesRegex(RuntimeError, "PlayALL.Aistalt.dll"):
                install_bridge(self.exe, self.bundle)
            copy_file.assert_not_called()
        self.assertEqual(self.files(), before)

    def test_preexisting_temporary_looking_file_is_never_overwritten_or_removed(self):
        unrelated = self.exe.parent / "winhttp.dll.playall-tmp"
        unrelated.write_bytes(b"preexisting file belongs to somebody else")
        install_bridge(self.exe, self.bundle)
        self.assertTrue(unrelated.is_file(), "Installer deleted an unowned preexisting file")
        self.assertEqual(unrelated.read_bytes(), b"preexisting file belongs to somebody else")

    def test_owned_upgrade_is_allowed_but_user_modified_owned_file_is_preserved(self):
        install_bridge(self.exe, self.bundle)
        source = self.bundle / "loader/winhttp.dll"
        source.write_bytes(b"synthetic loader version 2")
        install_bridge(self.exe, self.bundle)
        self.assertEqual((self.exe.parent / "winhttp.dll").read_bytes(), source.read_bytes())
        (self.exe.parent / "winhttp.dll").write_bytes(b"user changed loader")
        source.write_bytes(b"synthetic loader version 3")
        before = self.files()
        with self.assertRaises(RuntimeError):
            install_bridge(self.exe, self.bundle)
        self.assertEqual(self.files(), before)

    def test_copy_failure_rolls_back_owned_updates_new_files_and_temporary_files(self):
        install_bridge(self.exe, self.bundle)
        before = self.files()
        (self.bundle / "loader/winhttp.dll").write_bytes(b"synthetic loader version 2")
        (self.bundle / "loader/new-loader-file.txt").write_bytes(b"synthetic new file")
        (self.bundle / "PlayALL.Aistalt.dll").write_bytes(b"synthetic plugin version 2")
        import shutil
        original_copy = shutil.copy2
        copied = []

        def fail_at_plugin(source, target, *args, **kwargs):
            copied.append(Path(source).name)
            if Path(source).name == "PlayALL.Aistalt.dll":
                Path(target).write_bytes(b"partial temporary copy")
                raise OSError("synthetic disk failure during plugin copy")
            return original_copy(source, target, *args, **kwargs)

        with patch("playall.games.aistalt.shutil.copy2", side_effect=fail_at_plugin):
            with self.assertRaisesRegex(OSError, "synthetic disk failure"):
                install_bridge(self.exe, self.bundle)
        self.assertIn("new-loader-file.txt", copied, "Failure was not injected after a new file was installed")
        self.assertEqual(self.files(), before)
        self.assertEqual(list(self.exe.parent.rglob("*.playall-tmp")), [])

    def test_manifest_commit_failure_restores_old_bytes_and_ownership_manifest(self):
        install_bridge(self.exe, self.bundle)
        before = self.files()
        (self.bundle / "loader/winhttp.dll").write_bytes(b"synthetic loader version 2")
        (self.bundle / "loader/new-loader-file.txt").write_bytes(b"synthetic new file")
        with patch("playall.games.aistalt.atomic_json", side_effect=OSError("synthetic manifest commit failure")):
            with self.assertRaisesRegex(OSError, "synthetic manifest"):
                install_bridge(self.exe, self.bundle)
        self.assertEqual(self.files(), before)
        self.assertEqual(list(self.exe.parent.rglob("*.playall-tmp")), [])

    def test_missing_plugin_or_wrong_verified_game_fails_before_installation(self):
        (self.bundle / "PlayALL.Aistalt.dll").unlink()
        before = self.files()
        with self.assertRaises(RuntimeError):
            install_bridge(self.exe, self.bundle)
        self.assertEqual(self.files(), before)
        adapter = Aistalt(self.directory, {"aistalt_path": str(self.exe)}, lambda *args: None)
        adapter.bundle = self.bundle
        (self.exe.parent / "Aistalt_Data/Managed/InfantGod.dll").write_bytes(b"different unverified game")
        before = self.files()
        with patch("playall.games.aistalt.install_bridge") as install:
            with self.assertRaises(ValueError):
                adapter.prepare(RecordingControl())
            install.assert_not_called()
        self.assertEqual(self.files(), before)

    def test_wrong_bridge_hash_fails_before_touching_game_files(self):
        (self.bundle / "PlayALL.Aistalt.dll").write_bytes(b"changed plugin after build")
        before = self.files()
        adapter = Aistalt(self.directory, {"aistalt_path": str(self.exe)}, lambda *args: None)
        adapter.bundle = self.bundle
        with patch("playall.games.aistalt.install_bridge") as install:
            with self.assertRaises(RuntimeError):
                adapter.prepare(RecordingControl())
            install.assert_not_called()
        self.assertEqual(self.files(), before)


class AistaltRunnerTests(TemporaryGameMixin, unittest.TestCase):
    def make_runner(self, adapter, ai):
        self.settings.update({"provider": "custom", "game": "aistalt", "aistalt_path": str(self.exe),
                              "step_delay": 0, "output_tokens": 256,
                              "profiles": [{"id": "custom", "base_url": "http://127.0.0.1:1"}]})
        provider = patch("playall.runner.make_ai", return_value=ai)

        def create_game(game_id, directory, settings, emit):
            self.assertEqual(game_id, "aistalt")
            adapter.emit = emit
            return adapter

        factory = patch("playall.runner.make_game", side_effect=create_game)
        provider_mock = provider.start()
        self.addCleanup(provider.stop)
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
            self.assertFalse(runner.thread.is_alive(), "Synthetic Aistalt runner leaked a thread")

    def test_real_adapter_unready_window_never_requests_ai_and_stop_closes_cleanly(self):
        bridge = self.make_bridge(ready=False)
        adapter = self.make_adapter(bridge)
        adapter.watch = Mock(return_value={"ok": True})
        ai = GatedAI()
        runner, provider = self.make_runner(adapter, ai)
        runner.start()
        self.await_condition(lambda: bridge.methods().count("hello") >= 2, "Runner never checked the synthetic window")
        provider.assert_not_called()
        self.assertEqual(ai.calls, [])
        self.assertNotIn("resume", bridge.methods())
        self.assertNotIn("execute", bridge.methods())
        self.stop_runner(runner, ai)
        self.assertEqual(runner.snapshot()["status"], "stopped")
        self.assertEqual(ai.calls, [])
        self.assertIsNone(adapter.connection)
        self.assertEqual(bridge.methods()[-1], "detach")
        session = json.loads((self.directory / "sessions/aistalt.json").read_text(encoding="utf-8"))
        self.assertEqual(session["history"], [])
        self.assertEqual(session["counters"]["turns"], 0)

    def test_real_adapter_connection_timeout_saves_session_and_detaches_without_ai(self):
        bridge = self.make_bridge(ready=False)
        adapter = self.make_adapter(bridge)
        adapter.watch = Mock(return_value={"ok": True})
        ai = GatedAI()
        runner, provider = self.make_runner(adapter, ai)
        clock = Mock()
        clock.monotonic.side_effect = [0, 91]
        with patch("playall.games.aistalt.time", clock):
            runner.start()
            runner.thread.join(3)
        self.assertFalse(runner.thread.is_alive(), "Connection timeout leaked a runner thread")
        self.assertEqual(runner.snapshot()["status"], "error")
        provider.assert_not_called()
        self.assertEqual(ai.calls, [])
        self.assertNotIn("execute", bridge.methods())
        self.assertIn("checkpoint", bridge.methods())
        self.assertEqual(bridge.methods()[-1], "detach")
        self.assertIsNone(adapter.connection)
        self.assertTrue((self.directory / "sessions/aistalt.json").is_file())

    def test_real_adapter_disconnect_during_ai_pauses_and_recovers_with_new_observation(self):
        bridge = self.make_bridge()
        adapter = self.make_adapter(bridge)
        old_click = {"action": "click", "control_id": "fixture-option", "view_revision": bridge.revision + 1}
        ai = GatedAI([response(old_click, call_id="obsolete-disconnected-option")], gates=(1, 2))
        runner, _ = self.make_runner(adapter, ai)
        runner.start()
        self.assertTrue(ai.entered[1].wait(3))
        bridge.ready = False
        ai.release[1].set()
        self.await_condition(lambda: runner.snapshot()["status"] == "paused", "Disconnected bridge did not pause AI")
        self.assertEqual(len(ai.calls), 1)
        self.assertNotIn("execute", bridge.methods())
        self.assertTrue(bridge.paused)
        self.assertTrue(runner.snapshot()["active"])
        bridge.ready = True
        bridge.marker = "synthetic state after reconnect"
        runner.resume()
        self.assertTrue(ai.entered[2].wait(3))
        observation = json.loads(ai.calls[1][0][-1]["content"])["observation"]
        self.assertEqual(observation["visible_text"], [bridge.marker])
        self.assertNotIn("obsolete-disconnected-option", json.dumps(ai.calls[1][0]))
        self.assertEqual(bridge.applied_actions, [])
        self.stop_runner(runner, ai)

    def test_actual_prepare_connect_resume_chain_allows_first_action_and_manual_replan(self):
        bridge = self.make_bridge()
        adapter = self.make_adapter(bridge)
        # Connect leaves the bridge paused; Runner must acquire control before AI.
        click = {"action": "click", "control_id": "fixture-option", "view_revision": bridge.revision + 1}
        ai = GatedAI([response(click, call_id="completed-native-option"),
                      response(click, call_id="obsolete-pending-option")], gates=(2, 3))
        runner, _ = self.make_runner(adapter, ai)
        runner.start()
        self.assertTrue(ai.entered[2].wait(3), "Runner's first synthetic native action did not complete")
        self.assertEqual(bridge.applied_actions, [click], "Initial connect failed to resume bridge control")
        methods = bridge.methods()
        self.assertLess(methods.index("resume"), methods.index("observe"))
        self.assertLess(methods.index("observe"), methods.index("execute"))
        runner.pause()
        self.assertTrue(bridge.paused)
        bridge.marker = "human changed the synthetic game while paused"
        ai.release[2].set()
        self.await_condition(lambda: not runner.snapshot()["request_in_flight"], "Pending old response did not return")
        self.assertEqual(bridge.applied_actions, [click])
        runner.resume()
        self.assertTrue(ai.entered[3].wait(3), "Runner did not re-observe after manual control")
        observation = json.loads(ai.calls[2][0][-1]["content"])["observation"]
        self.assertEqual(observation["visible_text"], [bridge.marker])
        self.assertIn("completed-native-option", json.dumps(ai.calls[2][0]))
        self.assertNotIn("obsolete-pending-option", json.dumps(ai.calls[2][0]))
        self.assertEqual(bridge.applied_actions, [click], "Manual takeover replayed the old action")
        self.stop_runner(runner, ai)
        self.assertFalse(adapter.heartbeat_thread.is_alive())


if __name__ == "__main__":
    unittest.main()
