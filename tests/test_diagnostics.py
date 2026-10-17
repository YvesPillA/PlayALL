import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from playall.diagnostics import DebugLog


class DiagnosticsTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.log = DebugLog(self.directory)

    def test_credentials_redacted_in_nested_data_errors_and_readback(self):
        credential = "test-private-provider-credential"
        self.log.remember_secret(credential)
        self.log.write("connect", "Bearer " + credential, endpoint={"token": "aa" * 32, "api_key": credential},
                       error=f'api_key="{credential}" password=private-value secret: hidden-value',
                       trace=r"C:\Users\Example User\AppData\failed.py")
        raw = self.log.path.read_text(encoding="utf-8")
        self.assertNotIn(credential, raw)
        self.assertNotIn("aa" * 32, raw)
        self.assertNotIn("private-value", raw)
        self.assertNotIn("hidden-value", raw)
        self.assertNotIn("Example User", raw)
        self.assertNotIn(" User", raw)
        self.assertIn("已隐藏", self.log.read())
        # A credential registered after an earlier write is still hidden in export/read.
        self.log.path.write_text("late-private-value", encoding="utf-8")
        self.log.remember_secret("late-private-value")
        self.assertNotIn("late-private-value", self.log.read())

    def test_rotating_threaded_writes_remain_bounded_and_valid_utf8_json(self):
        self.log = DebugLog(self.directory, max_bytes=1500, backups=2)
        threads = [threading.Thread(target=lambda: [self.log.write("测试", "连接检查 " * 12) for _ in range(15)]) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(2)
            self.assertFalse(thread.is_alive())
        files = list((self.directory / "logs").glob("*"))
        self.assertEqual(len(files), 3)
        for path in files:
            self.assertLessEqual(path.stat().st_size, 1500)
            for line in path.read_text(encoding="utf-8").splitlines():
                self.assertEqual(json.loads(line)["event"], "测试")

    def test_disk_failure_keeps_recent_diagnostics_in_memory(self):
        with patch.object(Path, "mkdir", side_effect=OSError("synthetic disk unavailable")):
            self.log.write("connect", "连接失败，游戏没有加载组件。")
        self.assertIn("保留在内存", self.log.read())
        self.assertIn("连接失败", self.log.read())

    def test_bridge_export_excludes_unrelated_game_content(self):
        exe = self.directory / "game/Aistalt.exe"
        path = exe.parent / "BepInEx/LogOutput.log"
        path.parent.mkdir(parents=True)
        path.write_text("[Info] unrelated private story content\n[Info : PlayALL] lifecycle Start\n"
                        "[Info : BepInEx] Chainloader startup complete\n", encoding="utf-8")
        tail = self.log.bridge_tail(exe)
        self.assertIn("lifecycle Start", tail)
        self.assertNotIn("private story", tail)


if __name__ == "__main__":
    unittest.main()
