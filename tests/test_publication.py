import unittest

from tools.check_publication import scan_file


class PublicationTests(unittest.TestCase):
    def test_runtime_data_and_generated_binaries_are_rejected(self):
        for name in ["data/settings.json", ".cache/result.txt", ".env.production", "game.zip",
                     "PlayALL.exe", "mods/aistalt-bridge/loader/version.txt", "docs/handoff.md"]:
            with self.subTest(name=name):
                self.assertIn((0, "private-or-generated-file"), scan_file(name, b"{}"))

    def test_secret_and_personal_path_are_reported_without_echoing_values(self):
        token = "sk-" + "A" * 32
        private_path = "C:" + "\\" + "Users" + "\\" + "PrivateUser" + "\\file.txt"
        findings = scan_file("example.py", (token + "\n" + private_path).encode())
        self.assertIn((1, "provider-key"), findings)
        self.assertIn((2, "personal-profile-path"), findings)
        self.assertNotIn(token, repr(findings))
        self.assertNotIn(private_path, repr(findings))

    def test_synthetic_redaction_fixture_and_generic_source_pass(self):
        example = ('trace = r"C:' + '\\Users\\Example User\\AppData\\file.py"\n'
                   'url = "https://example.test"\napi_key = get_from_environment()')
        self.assertEqual(scan_file("tests/example.py", example.encode()), [])

    def test_non_text_and_unapproved_email_need_review(self):
        self.assertIn((0, "non-text-file-needs-review"), scan_file("image.dat", b"\xff\xfe\x00"))
        email = "person" + "@" + "private-domain.org"
        self.assertIn((1, "email-address"), scan_file("notes.md", email.encode()))


if __name__ == "__main__":
    unittest.main()
