"""Startup compatibility installs use synthetic files, never a running game."""
from __future__ import annotations

import configparser
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from playall.games.aistalt import install_bridge


DEFAULT_CONFIG = (
    "[Chainloader]\nHideManagerGameObject = false\n\n"
    "[Preloader.Entrypoint]\nAssembly = UnityEngine.CoreModule.dll\n"
    "Type = Application\nMethod = .cctor\n"
)


class AistaltStartupConfigTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="playall-startup-config-")
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.exe = self.directory / "synthetic-game/Aistalt.exe"
        self.exe.parent.mkdir()
        self.exe.write_bytes(b"not an executable")
        self.bundle = self.directory / "synthetic-bundle"
        for name, content in {
            "loader/winhttp.dll": b"synthetic loader",
            "loader/doorstop_config.ini": b"synthetic doorstop configuration",
            "loader/BepInEx/core/BepInEx.dll": b"synthetic runtime",
            "PlayALL.Aistalt.dll": b"synthetic plugin",
        }.items():
            target = self.bundle / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
        self.config_path = self.exe.parent / "BepInEx/config/BepInEx.cfg"
        for blocker in (
            patch("subprocess.Popen", side_effect=AssertionError("Do not launch a game")),
            patch("socket.create_connection", side_effect=AssertionError("Installer must not connect to a game")),
        ):
            blocker.start()
            self.addCleanup(blocker.stop)

    def write_config(self, value):
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        self.config_path.write_bytes(value if isinstance(value, bytes) else value.encode("utf-8"))

    def files(self):
        return {str(path.relative_to(self.exe.parent)): path.read_bytes()
                for path in self.exe.parent.rglob("*") if path.is_file()}

    def assert_compatible(self):
        settings = configparser.ConfigParser(interpolation=None)
        settings.read_string(self.config_path.read_text(encoding="utf-8-sig"))
        self.assertTrue(settings.getboolean("Chainloader", "HideManagerGameObject"))
        self.assertEqual(settings["Preloader.Entrypoint"]["Assembly"], "UnityEngine.CoreModule.dll")
        self.assertEqual(settings["Preloader.Entrypoint"]["Type"], "MonoBehaviour")
        self.assertEqual(settings["Preloader.Entrypoint"]["Method"], ".cctor")

    def test_fresh_install_creates_documented_settings_and_is_idempotent(self):
        plugin = install_bridge(self.exe, self.bundle)
        self.assert_compatible()
        manifest = json.loads((plugin / "install.json").read_text(encoding="utf-8"))
        self.assertIsNone(manifest["startup_config"]["backup"])
        self.assertNotIn("BepInEx/config/BepInEx.cfg", manifest["files"],
                         "Mutable game configuration must not be treated as an immutable loader binary")
        before, modified = self.files(), self.config_path.stat().st_mtime_ns
        with patch("playall.games.aistalt.shutil.copy2", side_effect=AssertionError("Identical binaries were rewritten")):
            install_bridge(self.exe, self.bundle)
        self.assertEqual(self.files(), before)
        self.assertEqual(self.config_path.stat().st_mtime_ns, modified)

    def test_default_entrypoint_preserves_comments_other_settings_bom_and_crlf_with_backup(self):
        original = b"\xef\xbb\xbf" + (
            "# Keep this exact comment.\r\n[Chainloader]\r\n"
            "## Preserve my other chainloader preference.\r\n"
            "  HideManagerGameObject  =  false  \r\nOther = unchanged\r\n\r\n"
            "[Logging.Disk]\r\nEnabled = false\r\n# Keep my logging choice.\r\n\r\n"
            "[Preloader.Entrypoint]\r\nAssembly = UnityEngine.CoreModule.dll\r\n"
            "# Keep the entrypoint explanation.\r\nType = Application\r\nMethod = .cctor\r\n"
        ).encode("utf-8")
        self.write_config(original)
        plugin = install_bridge(self.exe, self.bundle)
        expected = original.replace(b"=  false  ", b"=  true  ").replace(b"Type = Application", b"Type = MonoBehaviour")
        self.assertEqual(self.config_path.read_bytes(), expected)
        self.assert_compatible()
        manifest = json.loads((plugin / "install.json").read_text(encoding="utf-8"))
        backup = self.exe.parent / manifest["startup_config"]["backup"]
        self.assertEqual(backup.read_bytes(), original)
        self.assertEqual(manifest["startup_config"]["sha256"], hashlib.sha256(expected).hexdigest())
        before = self.files()
        install_bridge(self.exe, self.bundle)
        self.assertEqual(self.files(), before, "Repeated installs created or replaced another backup")

    def test_missing_options_are_added_without_rewriting_unrelated_content(self):
        original = ("# user note\n[Chainloader]\nKeep = yes\n\n"
                    "[Preloader.Entrypoint]\n# assembly note\nAssembly = UnityEngine.CoreModule.dll\n"
                    "Extra = untouched\n\n[Logging.Console]\nEnabled = false")
        self.write_config(original)
        install_bridge(self.exe, self.bundle)
        self.assert_compatible()
        configured = self.config_path.read_text(encoding="utf-8")
        for preserved in ("# user note", "Keep = yes", "# assembly note", "Extra = untouched",
                          "[Logging.Console]\nEnabled = false"):
            self.assertIn(preserved, configured)

    def test_custom_entrypoints_are_rejected_before_any_file_or_backup_is_written(self):
        for key, value in (("Assembly", "CustomBootstrap.dll"), ("Type", "CustomBootstrap"),
                           ("Method", "Awake")):
            configured = DEFAULT_CONFIG.replace(
                {"Assembly": "Assembly = UnityEngine.CoreModule.dll", "Type": "Type = Application",
                 "Method": "Method = .cctor"}[key], f"{key} = {value}")
            self.write_config(configured)
            before = self.files()
            with self.subTest(key=key), patch("playall.games.aistalt.shutil.copy2") as copy_file:
                with self.assertRaisesRegex(RuntimeError, "自定义启动入口"):
                    install_bridge(self.exe, self.bundle)
                copy_file.assert_not_called()
                self.assertEqual(self.files(), before)

    def test_ambiguous_or_unreadable_configuration_is_preserved_without_installation(self):
        variants = (
            DEFAULT_CONFIG + "Type = MonoBehaviour\n",
            DEFAULT_CONFIG + "\n[Preloader.Entrypoint]\nType = Application\n",
            DEFAULT_CONFIG.replace("HideManagerGameObject = false", "HideManagerGameObject = unknown"),
            b"\xffnot UTF-8 configuration",
        )
        for configured in variants:
            self.write_config(configured)
            before = self.files()
            with self.subTest(configured=configured), self.assertRaises(RuntimeError):
                install_bridge(self.exe, self.bundle)
            self.assertEqual(self.files(), before)

    def test_backup_collision_does_not_overwrite_an_unowned_file(self):
        original = DEFAULT_CONFIG.encode("utf-8")
        self.write_config(original)
        digest = hashlib.sha256(original).hexdigest()[:16]
        backup = self.config_path.with_name(self.config_path.name + ".playall-backup-" + digest + ".bak")
        backup.write_bytes(b"unowned preexisting file")
        before = self.files()
        with self.assertRaisesRegex(RuntimeError, "备份路径"):
            install_bridge(self.exe, self.bundle)
        self.assertEqual(self.files(), before)

    def test_config_replace_failure_restores_binaries_config_and_removes_transaction_backups(self):
        self.write_config(DEFAULT_CONFIG)
        # Windows can canonicalize temporary-directory aliases. Exercise an
        # equivalent path spelling so injection does not depend on that spelling.
        self.config_path = self.config_path.parent / ".." / "config" / self.config_path.name
        before = self.files()
        replace = os.replace
        attempted = []

        def fail_configuration(source, destination):
            if Path(destination).resolve() == self.config_path.resolve():
                attempted.append(Path(source).read_bytes())
                raise OSError("synthetic startup config commit failure")
            return replace(source, destination)

        with patch("playall.games.aistalt.os.replace", side_effect=fail_configuration):
            with self.assertRaisesRegex(OSError, "startup config commit"):
                install_bridge(self.exe, self.bundle)
        self.assertEqual(len(attempted), 1)
        self.assertIn(b"Type = MonoBehaviour", attempted[0])
        self.assertEqual(self.files(), before)
        self.assertEqual(list(self.exe.parent.rglob("*.tmp")), [])

    def test_config_flush_failure_removes_the_partial_temporary_file(self):
        self.write_config(DEFAULT_CONFIG)
        before = self.files()
        fsync, flushed = os.fsync, []

        def fail_configuration_flush(descriptor):
            flushed.append(descriptor)
            if len(flushed) == 2:  # The original configuration backup was flushed first.
                raise OSError("synthetic configuration flush failure")
            return fsync(descriptor)

        with patch("playall.games.aistalt.os.fsync", side_effect=fail_configuration_flush):
            with self.assertRaisesRegex(OSError, "configuration flush"):
                install_bridge(self.exe, self.bundle)
        self.assertEqual(len(flushed), 2)
        self.assertEqual(self.files(), before)
        self.assertEqual(list(self.exe.parent.rglob("*.tmp")), [])

    def test_manifest_failure_restores_modified_config_and_owned_upgrade_with_existing_backup(self):
        self.write_config(DEFAULT_CONFIG)
        install_bridge(self.exe, self.bundle)
        original = DEFAULT_CONFIG + "\n[Logging.Disk]\nEnabled = false\n"
        self.write_config(original)
        before = self.files()
        (self.bundle / "loader/winhttp.dll").write_bytes(b"synthetic owned upgrade")
        observed_backup = []

        def fail_manifest(*args, **kwargs):
            self.assert_compatible()
            backups = list(self.config_path.parent.glob("BepInEx.cfg.playall-backup-*.bak"))
            observed_backup.extend(path.read_bytes() for path in backups)
            raise OSError("synthetic manifest commit failure")

        with patch("playall.games.aistalt.atomic_json", side_effect=fail_manifest):
            with self.assertRaisesRegex(OSError, "manifest commit"):
                install_bridge(self.exe, self.bundle)
        self.assertIn(original.encode("utf-8"), observed_backup, "Configuration was changed before its backup existed")
        self.assertEqual(self.files(), before)
        self.assertEqual(list(self.exe.parent.rglob("*.tmp")), [])


if __name__ == "__main__":
    unittest.main()
