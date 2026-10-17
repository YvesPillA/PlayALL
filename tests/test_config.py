from __future__ import annotations

import json
import os
import unittest
from unittest.mock import patch

from playall.config import Settings, protect, unprotect, validate_endpoint
from tests.support import IsolatedSettingsMixin


class EndpointTests(unittest.TestCase):
    def test_https_and_local_http_are_allowed_and_trailing_slash_is_removed(self):
        for endpoint in (
            "https://api.deepseek.com", "https://example.invalid/v1/",
            "http://127.0.0.1:11434/v1/", "http://localhost:8000",
            "http://[::1]:11434/v1/",
        ):
            with self.subTest(endpoint=endpoint):
                self.assertEqual(validate_endpoint(endpoint), endpoint.rstrip("/"))

    def test_remote_http_credentials_query_fragment_and_missing_host_are_rejected(self):
        for endpoint in (
            "http://example.invalid/v1", "http://127.0.0.1.evil.invalid",
            "https://user:password@example.invalid", "https://user@example.invalid",
            "https://example.invalid?key=private", "https://example.invalid/#private",
            "ftp://localhost", "https:///v1", "api.deepseek.com", "",
            "https://api.deepseek.com:not-a-port", "https://api.deepseek.com:65536",
        ):
            with self.subTest(endpoint=endpoint), self.assertRaises(ValueError):
                validate_endpoint(endpoint)


class SettingsTests(IsolatedSettingsMixin, unittest.TestCase):
    def test_snapshot_is_detached_and_invalid_update_does_not_change_disk(self):
        self.settings.update({"speed": 2})
        before = self.settings.path.read_bytes()
        snapshot = self.settings.snapshot()
        snapshot["profiles"][0]["model"] = "changed-outside-settings"
        self.assertNotEqual(self.settings.snapshot()["profiles"][0]["model"], snapshot["profiles"][0]["model"])
        with self.assertRaises(ValueError):
            self.settings.update({"speed": 3, "profiles": [{"id": "custom", "base_url": "http://remote.invalid"}]})
        self.assertEqual(self.settings.path.read_bytes(), before)
        self.assertEqual(self.settings.snapshot()["speed"], 2)
        self.assertFalse(self.settings.path.with_suffix(".tmp").exists())

    def test_public_settings_remove_ciphertext_and_only_expose_key_presence(self):
        with patch("playall.config.protect", return_value="opaque-encrypted-value"):
            public = self.settings.update({"profiles": [{"id": "deepseek", "api_key": "synthetic-test-key"}]})
        self.assertTrue(public["profiles"][0]["has_key"])
        self.assertFalse(public["profiles"][1]["has_key"])
        rendered = json.dumps(public)
        self.assertNotIn("synthetic-test-key", rendered)
        self.assertNotIn("opaque-encrypted-value", rendered)
        for profile in public["profiles"]:
            self.assertNotIn("api_key", profile)
            self.assertNotIn("secret", profile)

    def test_environment_key_takes_precedence_and_is_never_persisted(self):
        with patch("playall.config.protect", return_value="encrypted-test-value"):
            self.settings.update({"profiles": [{"id": "deepseek", "api_key": "saved-test-value"}]})
        with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "synthetic-environment-key"}), patch("playall.config.unprotect") as decrypt:
            self.assertEqual(self.settings.profile("deepseek")["api_key"], "synthetic-environment-key")
            decrypt.assert_not_called()
            self.assertTrue(self.settings.public()["profiles"][0]["has_key"])
        self.assertNotIn("synthetic-environment-key", self.settings.path.read_text(encoding="utf-8"))

    def test_clearing_key_removes_saved_secret(self):
        with patch("playall.config.protect", return_value="encrypted-test-value"):
            self.settings.update({"profiles": [{"id": "deepseek", "api_key": "saved-test-value"}]})
        public = self.settings.update({"profiles": [{"id": "deepseek", "clear_key": True}]})
        self.assertFalse(public["profiles"][0]["has_key"])
        self.assertNotIn("secret", self.settings.snapshot()["profiles"][0])
        self.assertEqual(self.settings.profile("deepseek")["api_key"], "")

    @unittest.skipUnless(os.name == "nt", "Real Windows DPAPI is only available on Windows")
    def test_real_dpapi_roundtrip_ciphertext_persistence_and_reload(self):
        secret = "synthetic-test-only-密钥-123456789"
        encrypted = protect(secret)
        self.assertNotEqual(encrypted, secret)
        self.assertNotIn(secret, encrypted)
        self.assertEqual(unprotect(encrypted), secret)
        public = self.settings.update({"profiles": [{"id": "deepseek", "api_key": secret}]})
        self.assertNotIn(secret, json.dumps(public, ensure_ascii=False))
        stored = self.settings.path.read_text(encoding="utf-8")
        self.assertNotIn(secret, stored)
        self.assertNotIn("api_key", json.loads(stored)["profiles"][0])
        reloaded = Settings(self.directory)
        self.assertEqual(reloaded.profile("deepseek")["api_key"], secret)
        self.assertNotIn("secret", reloaded.profile("deepseek"))
        self.assertNotIn(secret, json.dumps(reloaded.public(), ensure_ascii=False))

    def test_bounds_and_nonfinite_values_are_rejected(self):
        invalid = ({"speed": 0}, {"speed": float("nan")}, {"speed": float("inf")},
                   {"output_tokens": 4097}, {"game": "unknown"},
                   {"profiles": [{"id": "custom", "input_price": float("nan")}]})
        for changes in invalid:
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.settings.update(changes)

    def test_long_term_prompt_is_saved_and_legacy_goals_and_limits_are_removed(self):
        prompt = "先观察世界，再长期建设工厂。达到阶段目标后自行继续。"
        self.settings.path.write_text(json.dumps({"system_prompt": prompt, "goal": "旧单局目标",
                                                 "max_turns": 1, "max_tokens": 1000, "max_cost": 0.01}), encoding="utf-8")
        reloaded = Settings(self.directory)
        self.assertEqual(reloaded.snapshot()["system_prompt"], prompt)
        for key in ("goal", "max_turns", "max_tokens", "max_cost"):
            self.assertNotIn(key, reloaded.snapshot())
            self.assertNotIn(key, reloaded.public())
        updated_prompt = "新长期提示词\n继续观察、采矿和研究。"
        reloaded.update({"system_prompt": updated_prompt})
        self.assertEqual(Settings(self.directory).snapshot()["system_prompt"], updated_prompt)

    def test_failed_atomic_replace_keeps_previous_settings_and_memory(self):
        self.settings.update({"speed": 2})
        before = self.settings.path.read_bytes()
        with patch("playall.config.os.replace", side_effect=OSError("synthetic replace failure")):
            with self.assertRaises(OSError):
                self.settings.update({"speed": 3})
        self.assertEqual(self.settings.path.read_bytes(), before)
        self.assertEqual(self.settings.snapshot()["speed"], 2)


if __name__ == "__main__":
    unittest.main()
