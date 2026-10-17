from __future__ import annotations

import io
import json
import threading
import unittest
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import Mock, patch

from playall.llm import CompatibleAI, ProviderError, RehearsalAI


class ProviderHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        self.respond()

    def do_POST(self):
        self.respond()

    def respond(self):
        raw = self.rfile.read(int(self.headers.get("Content-Length", "0")))
        self.server.requests.append((self.path, dict(self.headers), raw))
        status, body, headers = self.server.result(self.path, raw)
        self.send_response(status)
        for key, value in headers.items():
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class CompatibleAITests(unittest.TestCase):
    def ai(self, result):
        server = ThreadingHTTPServer(("127.0.0.1", 0), ProviderHandler)
        server.daemon_threads = True
        server.result = result
        server.requests = []
        self.addCleanup(server.server_close)
        thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=0.01), daemon=True)
        thread.start()
        self.addCleanup(thread.join, 2)
        self.addCleanup(server.shutdown)
        profile = {"id": "custom", "base_url": f"http://127.0.0.1:{server.server_port}",
                   "model": "test-local-model", "api_key": "synthetic-provider-key"}
        # Prevent machine proxy settings from routing these local-only tests elsewhere.
        with patch("urllib.request.getproxies", return_value={}):
            ai = CompatibleAI(profile)
        return ai, server

    def test_local_probe_and_tool_chat_payload_use_compatible_protocol(self):
        response = {"choices": [{"message": {"content": "下一步", "tool_calls": []}}],
                    "usage": {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18}}

        def result(path, raw):
            payload = {"data": [{"id": "test-local-model"}, {"id": 2}, None]} if path == "/models" else response
            return 200, json.dumps(payload, ensure_ascii=False).encode("utf-8"), {}

        ai, server = self.ai(result)
        self.assertTrue(ai.probe()["ok"])
        messages = [{"role": "user", "content": "开始采矿"}]
        tools = [{"type": "function", "function": {"name": "game_action"}}]
        self.assertEqual(ai.chat(messages, tools, 256), response)
        self.assertEqual([request[0] for request in server.requests], ["/models", "/chat/completions"])
        path, headers, raw = server.requests[1]
        self.assertEqual(headers["Authorization"], "Bearer synthetic-provider-key")
        payload = json.loads(raw)
        self.assertEqual(payload["messages"], messages)
        self.assertEqual(payload["tools"], tools)
        self.assertEqual(payload["max_tokens"], 256)
        self.assertFalse(payload["stream"])
        self.assertEqual(payload["tool_choice"], "auto")
        self.assertNotIn("thinking", payload)

    def test_deepseek_thinking_flag_is_applied_without_making_a_request(self):
        ai = CompatibleAI({"id": "deepseek", "base_url": "https://api.deepseek.com", "model": "test"})
        ai._request = Mock(return_value={})
        ai.chat([], [], 256)
        self.assertEqual(ai._request.call_args.args[0], "/chat/completions")
        self.assertEqual(ai._request.call_args.args[1]["thinking"], {"type": "disabled"})

    def test_redirect_is_not_followed_and_error_body_does_not_leak(self):
        def result(path, raw):
            return 302, b"synthetic-private-error-body", {"Location": "/would-receive-key"}

        ai, server = self.ai(result)
        with self.assertRaises(ProviderError) as caught:
            ai._request("/redirect")
        self.assertIn("302", str(caught.exception))
        self.assertNotIn("synthetic-private-error-body", str(caught.exception))
        self.assertNotIn("synthetic-provider-key", str(caught.exception))
        self.assertEqual([request[0] for request in server.requests], ["/redirect"])

    def test_http_authentication_error_is_actionable_and_redacts_server_body(self):
        ai, _ = self.ai(lambda path, raw: (401, b"token=synthetic-provider-key", {}))
        with self.assertRaisesRegex(ProviderError, "API Key 无效") as caught:
            ai.probe()
        self.assertNotIn("synthetic-provider-key", str(caught.exception))

    def test_invalid_json_and_utf8_are_handled(self):
        for body in (b"this is not json", b"\xff\xfe"):
            with self.subTest(body=body):
                ai, _ = self.ai(lambda path, raw, body=body: (200, body, {}))
                with self.assertRaisesRegex(ProviderError, "有效 JSON"):
                    ai.probe()

    def test_valid_json_with_invalid_response_structure_is_a_provider_error(self):
        for payload in ([], None, "unexpected scalar", {"data": None}, {"data": 17}):
            with self.subTest(payload=payload):
                ai, _ = self.ai(lambda path, raw, payload=payload: (200, json.dumps(payload).encode("utf-8"), {}))
                with self.assertRaises(ProviderError):
                    ai.probe()

    def test_response_size_is_bounded_and_network_details_are_redacted(self):
        ai = CompatibleAI({"id": "custom", "base_url": "http://127.0.0.1:1", "model": "test"})
        ai.opener = Mock()
        ai.opener.open.return_value = io.BytesIO(b"x" * (4 * 1024 * 1024 + 1))
        with self.assertRaisesRegex(ProviderError, "过大"):
            ai._request("/models")
        ai.opener.open.side_effect = urllib.error.URLError("private test network detail")
        with self.assertRaises(ProviderError) as caught:
            ai._request("/models")
        self.assertNotIn("private test network detail", str(caught.exception))
        self.assertIn("网络", str(caught.exception))

    def test_http_error_response_stream_is_closed(self):
        ai = CompatibleAI({"id": "custom", "base_url": "http://127.0.0.1:1", "model": "test"})
        body = io.BytesIO(b"private synthetic error details")
        error = urllib.error.HTTPError("http://127.0.0.1:1/models", 401, "Unauthorized", {}, body)
        self.addCleanup(error.close)
        ai.opener = Mock()
        ai.opener.open.side_effect = error
        with self.assertRaises(ProviderError):
            ai._request("/models")
        self.assertTrue(body.closed, "HTTP errors must close their response before rethrowing a safe message")


class RehearsalAITests(unittest.TestCase):
    def test_lifetime_production_does_not_cause_repeated_extraction_from_empty_furnace(self):
        ai = RehearsalAI({"id": "rehearsal"})
        state = {"inventory": {"iron-plate": 8}, "iron_plates_produced": 12,
                 "buildings": [{"name": "stone-furnace", "x": 4, "y": 0,
                                "fuel": {"coal": 5}, "input": {}, "output": {}}]}
        name, arguments, _ = ai.decide(state)
        self.assertEqual(name, "game_action")
        self.assertNotEqual(arguments.get("action"), "extract", "Empty output needs new production, not repeated extraction")


if __name__ == "__main__":
    unittest.main()
