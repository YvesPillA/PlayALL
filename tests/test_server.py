from __future__ import annotations

import http.client
import json
import threading
import unittest
from unittest.mock import patch

from playall.server import LocalServer
from tests.support import IsolatedSettingsMixin


class LocalHTTPTests(IsolatedSettingsMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.server = LocalServer(port=0, directory=self.directory)
        self.settings = self.server.settings
        self.addCleanup(self.server.server_close)
        self.thread = threading.Thread(
            target=lambda: self.server.serve_forever(poll_interval=0.01),
            daemon=True, name="Test PlayALL HTTP server",
        )
        self.thread.start()
        self.addCleanup(self.thread.join, 2)
        self.addCleanup(self.server.shutdown)

    def request(self, method, path, data=None, *, host=True, origin=None, token=None,
                content_type="application/json", raw=None, length=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=3)
        body = raw if raw is not None else (json.dumps(data or {}, ensure_ascii=False).encode("utf-8") if method == "POST" else None)
        try:
            connection.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
            if host is not None:
                value = f"127.0.0.1:{self.server.server_port}" if host is True else host
                connection.putheader("Host", value)
            if origin is not None:
                connection.putheader("Origin", origin)
            if token is not None:
                connection.putheader("X-PlayALL-Token", token)
            if method == "POST":
                connection.putheader("Content-Type", content_type)
                connection.putheader("Content-Length", str(length if length is not None else len(body)))
            connection.endheaders(body)
            response = connection.getresponse()
            payload = response.read()
            headers = dict(response.getheaders())
            return response.status, json.loads(payload), headers
        finally:
            connection.close()

    def test_bootstrap_and_settings_responses_redact_saved_and_plaintext_secrets(self):
        secret = "synthetic-http-key"
        with patch("playall.config.protect", return_value="opaque-http-ciphertext"):
            status, result, _ = self.request("POST", "/api/settings", {
                "profiles": [{"id": "deepseek", "api_key": secret}],
            }, token=self.server.token)
        self.assertEqual(status, 200)
        self.assertTrue(result["settings"]["profiles"][0]["has_key"])
        status, bootstrap, headers = self.request("GET", "/api/bootstrap")
        self.assertEqual(status, 200)
        self.assertEqual(bootstrap["token"], self.server.token)
        self.assertTrue(bootstrap["settings"]["profiles"][0]["has_key"])
        for response in (result, bootstrap):
            rendered = json.dumps(response)
            self.assertNotIn(secret, rendered)
            self.assertNotIn("opaque-http-ciphertext", rendered)
            for profile in response["settings"]["profiles"]:
                self.assertNotIn("secret", profile)
                self.assertNotIn("api_key", profile)
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
        self.assertEqual(headers["Referrer-Policy"], "no-referrer")
        self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])

    def test_host_rebinding_and_missing_host_cannot_obtain_bootstrap_token(self):
        for host in (None, "evil.invalid", f"127.0.0.1.evil.invalid:{self.server.server_port}", "127.0.0.1:1"):
            with self.subTest(host=host):
                status, result, _ = self.request("GET", "/api/bootstrap", host=host)
                self.assertEqual(status, 403)
                self.assertNotIn(self.server.token, json.dumps(result))
        status, _, _ = self.request("GET", "/api/health", host=f"localhost:{self.server.server_port}")
        self.assertEqual(status, 200)

    def test_cross_origin_read_and_write_are_rejected_even_with_correct_token(self):
        before = self.settings.snapshot()["speed"]
        for origin in ("https://evil.invalid", "null", "http://127.0.0.1:1", f"https://127.0.0.1:{self.server.server_port}"):
            with self.subTest(origin=origin):
                self.assertEqual(self.request("GET", "/api/bootstrap", origin=origin)[0], 403)
                self.assertEqual(self.request("POST", "/api/settings", {"speed": 2},
                                              origin=origin, token=self.server.token)[0], 403)
        self.assertEqual(self.settings.snapshot()["speed"], before)
        self.assertEqual(self.request("GET", "/api/bootstrap", origin=self.server.url)[0], 200)

    def test_mutation_needs_current_token_and_trusted_host(self):
        before = self.settings.snapshot()["speed"]
        for token in (None, "invalid-session-token"):
            with self.subTest(token=token):
                self.assertEqual(self.request("POST", "/api/settings", {"speed": 2}, token=token)[0], 403)
        self.assertEqual(self.request("POST", "/api/settings", {"speed": 2},
                                      host="evil.invalid", token=self.server.token)[0], 403)
        self.assertEqual(self.settings.snapshot()["speed"], before)
        status, result, _ = self.request("POST", "/api/settings", {"speed": 2}, token=self.server.token)
        self.assertEqual(status, 200)
        self.assertTrue(result["ok"])
        self.assertEqual(self.settings.snapshot()["speed"], 2)

    def test_request_size_type_and_json_errors_fail_without_mutating_settings(self):
        before = self.settings.snapshot()
        requests = (
            {"length": 65537}, {"length": -1}, {"content_type": "text/plain"},
            {"raw": b"{"}, {"raw": b"[]"}, {"raw": b"null"},
        )
        for parameters in requests:
            with self.subTest(parameters=parameters):
                status, result, _ = self.request("POST", "/api/settings", token=self.server.token, **parameters)
                self.assertEqual(status, 400)
                self.assertIn("error", result)
        self.assertEqual(self.settings.snapshot(), before)

    def test_static_path_cannot_escape_web_root_and_bad_cursor_is_handled(self):
        for path in ("/../playall/config.py", "/../data/settings.json", "/unknown-file.txt"):
            with self.subTest(path=path):
                self.assertEqual(self.request("GET", path)[0], 404)
        self.assertEqual(self.request("GET", "/api/state?after=not-a-number")[0], 400)

    def test_protected_start_endpoint_dispatches_exact_request_without_launching_game(self):
        options = {"provider": "rehearsal", "game": "sandbox", "new_world": True}
        with patch.object(self.server.runner, "start", return_value={"ok": True, "run_id": "test-run"}) as start:
            self.assertEqual(self.request("POST", "/api/start", options)[0], 403)
            start.assert_not_called()
            status, result, _ = self.request("POST", "/api/start", options, token=self.server.token)
            self.assertEqual(status, 200)
            self.assertEqual(result["run_id"], "test-run")
            start.assert_called_once_with(options)


if __name__ == "__main__":
    unittest.main()
