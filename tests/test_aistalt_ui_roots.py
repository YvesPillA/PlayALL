"""Compile the actual bridge UI-root methods against isolated C# fakes.

This runs the method bodies, not a translation or source-string assertion. It
does not load any game, Unity/FairyGUI assembly, desktop UI or AI provider.
"""
from __future__ import annotations

import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
BRIDGE = ROOT / "mods/aistalt-bridge/PlayALL.Aistalt.cs"
HARNESS = ROOT / "tests/fixtures/aistalt_ui_roots_harness.cs"


def extract_method(source, name):
    """Keep production bodies verbatim; skip braces in C# comments/literals."""
    match = re.search(r"^\s*private\s+[^\n{};]+\b" + re.escape(name) + r"\s*\([^\n]*\)\s*\{", source, re.M)
    if match is None:
        raise AssertionError(f"Production method {name} was not found; update the test boundary explicitly")
    opening = source.index("{", match.start(), match.end())
    tokens = re.finditer(r'//[^\n]*|/\*[\s\S]*?\*/|@"(?:[^"]|"")*"|"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'|[{}]', source[opening:])
    depth = 0
    for token in tokens:
        if token.group() == "{":
            depth += 1
        elif token.group() == "}":
            depth -= 1
            if depth == 0:
                return source[match.start():opening + token.end()]
    raise AssertionError(f"Unterminated production method {name}")


def production_members(source):
    members = []
    for name in ("PanelUI", "StageRoot", "rootInteraction"):
        match = re.search(r"^\s*private[^\n;]*\b" + name + r"\b[^\n;]*;", source, re.M)
        if match is None:
            raise AssertionError(f"Production field {name} was not found")
        members.append(match.group())
    members.extend(extract_method(source, name) for name in ("ExistingStageRoot", "UIRoots", "UIReady", "Visible"))
    return "\n".join(members)


class AistaltUIRootBehaviorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        windows = Path(os.environ.get("WINDIR", r"C:\Windows"))
        candidates = [windows / "Microsoft.NET/Framework64/v4.0.30319/csc.exe",
                      windows / "Microsoft.NET/Framework/v4.0.30319/csc.exe"]
        command = shutil.which("csc")
        if command:
            candidates.append(Path(command))
        compiler = next((candidate for candidate in candidates if candidate.is_file()), None)
        if compiler is None or os.name != "nt":
            raise unittest.SkipTest("The isolated C# console fixture requires the Windows .NET Framework compiler")
        temporary = tempfile.TemporaryDirectory(prefix="playall-ui-roots-test-")
        cls.addClassCleanup(temporary.cleanup)
        directory = Path(temporary.name)
        # Reflection fields and all four real method bodies are copied directly.
        members = production_members(BRIDGE.read_text(encoding="utf-8-sig"))
        template = HARNESS.read_text(encoding="utf-8-sig")
        if template.count("/* PRODUCTION_MEMBERS */") != 1:
            raise AssertionError("The C# test fixture must contain exactly one production insertion point")
        source = directory / "UIRootsBehavior.cs"
        source.write_text(template.replace("/* PRODUCTION_MEMBERS */", members), encoding="utf-8-sig")
        cls.executable = directory / "UIRootsBehavior.exe"
        compiled = subprocess.run([str(compiler), "/nologo", "/codepage:65001", "/target:exe",
                                   "/out:" + str(cls.executable), str(source)], cwd=directory,
                                  capture_output=True, text=True, encoding="mbcs", errors="replace", timeout=20,
                                  creationflags=subprocess.CREATE_NO_WINDOW)
        if compiled.returncode:
            raise AssertionError("Actual bridge methods did not compile against test fakes:\n" + compiled.stdout + compiled.stderr)

    def run_scenario(self, name):
        result = subprocess.run([str(self.executable), name], cwd=self.executable.parent,
                                capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=5,
                                creationflags=subprocess.CREATE_NO_WINDOW)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(result.stdout.strip(), "PASS " + name)

    def test_displayed_native_panel_is_found_beside_empty_groot_and_main_menu_is_ready(self):
        self.run_scenario("panel_beside_empty_stage")

    def test_displayed_panel_works_without_creating_any_groot(self):
        self.run_scenario("panel_without_stage")

    def test_uncreated_panel_is_ignored_without_calling_lazy_ui_getter(self):
        self.run_scenario("uncreated_panel")

    def test_hidden_inactive_disabled_disposed_offstage_transparent_and_empty_panels_are_rejected(self):
        for name in ("panel_hidden", "panel_inactive", "panel_disabled", "panel_disposed", "panel_off_stage", "panel_alpha_zero", "panel_empty"):
            with self.subTest(scenario=name):
                self.run_scenario(name)

    def test_repeated_panels_and_stage_aliases_are_deduplicated(self):
        self.run_scenario("duplicate_roots")

    def test_existing_stage_children_and_parents_are_preserved(self):
        self.run_scenario("stage_children_preserved")

    def test_batch_mode_never_reports_ready_even_with_displayed_ui(self):
        self.run_scenario("batch")

    def test_bootstrap_or_loading_scenes_never_report_ready(self):
        for name in ("bootstrap", "loading_scene"):
            with self.subTest(scenario=name):
                self.run_scenario(name)

    def test_unloaded_scene_and_loading_manager_never_report_ready(self):
        for name in ("unloaded_scene", "loading_manager"):
            with self.subTest(scenario=name):
                self.run_scenario(name)

    def test_loaded_main_game_with_displayed_panel_is_ready(self):
        self.run_scenario("main_game_ready")

    def test_gobject_clipping_and_display_ancestry_visibility_are_respected(self):
        for name in ("hidden_gobject_ancestor", "transparent_gobject_ancestor", "clipped_by_gobject_parent",
                     "hidden_display_ancestor", "hidden_display_object", "missing_display_object"):
            with self.subTest(scenario=name):
                self.run_scenario(name)

    def test_observable_panel_retains_native_noninteractive_guard(self):
        self.run_scenario("noninteractive_panel")


if __name__ == "__main__":
    unittest.main()
