"""Compile and run the production classification and permission bodies against fakes."""
import os
from pathlib import Path
import re
import subprocess
import tempfile
import unittest

from tests.test_aistalt_ui_roots import BRIDGE, ROOT, extract_method


class NativeConfirmationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        compiler = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Microsoft.NET/Framework64/v4.0.30319/csc.exe"
        if os.name != "nt" or not compiler.is_file():
            raise unittest.SkipTest("Isolated native permission tests require the Windows C# compiler")
        temporary = tempfile.TemporaryDirectory(prefix="playall-confirmation-test-")
        cls.addClassCleanup(temporary.cleanup)
        path = Path(temporary.name)
        code = BRIDGE.read_text(encoding="utf-8-sig")
        review = re.search(r"private sealed class Review\s*\{[^{}]*\}", code).group()
        methods = [extract_method(code, name) for name in ("Clip", "InNamedParent", "ImportantOperation",
            "ConfirmationQuestion", "ActionKey", "ReviewAction", "ApproveAction", "ConsumeApproval")]
        members = review + "\nprivate Review review;\n" + "\n".join(methods)
        template = (ROOT / "tests/fixtures/aistalt_confirmation_harness.cs").read_text(encoding="utf-8")
        source = path / "Confirmation.cs"
        source.write_text(template.replace("/* PRODUCTION_MEMBERS */", members), encoding="utf-8-sig")
        cls.executable = path / "Confirmation.exe"
        compiled = subprocess.run([str(compiler), "/nologo", "/codepage:65001", "/out:" + str(cls.executable), str(source)],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=20, creationflags=subprocess.CREATE_NO_WINDOW)
        if compiled.returncode:
            raise AssertionError(compiled.stdout + compiled.stderr)

    def test_native_classification_and_single_use_grants(self):
        for scenario in ("classify_new_load", "classify_yes_precise", "classify_taskbar_return", "stale_ui",
                         "wrong_session", "changed_action", "changed_owner", "one_use", "stale_after_approval"):
            with self.subTest(scenario=scenario):
                result = subprocess.run([str(self.executable), scenario], capture_output=True, text=True,
                    encoding="utf-8", errors="replace", timeout=5, creationflags=subprocess.CREATE_NO_WINDOW)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual(result.stdout.strip(), "PASS " + scenario)


if __name__ == "__main__":
    unittest.main()
