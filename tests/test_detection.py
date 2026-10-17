import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from playall.config import detect_aistalt, detect_factorio


class GameDetectionTests(unittest.TestCase):
    def test_custom_steam_library_detects_both_games_without_fixed_drive_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            steam, library = root / "Steam", root / "Custom Library"
            vdf = steam / "steamapps/libraryfolders.vdf"
            vdf.parent.mkdir(parents=True)
            vdf.write_text('"libraryfolders" { "0" { "path" ' + json.dumps(str(library), ensure_ascii=False) + ' } }', encoding="utf-8")
            factorio = library / "steamapps/common/Factorio/bin/x64/factorio.exe"
            aistalt = library / "steamapps/common/幼神 Demo/Build/Aistalt.exe"
            for path in [factorio, aistalt]:
                path.parent.mkdir(parents=True)
                path.touch()
            with patch.dict(os.environ, {"STEAM_PATH": str(steam), "FACTORIO_PATH": "", "AISTALT_PATH": ""}):
                self.assertEqual(detect_factorio(), str(factorio))
                self.assertEqual(detect_aistalt(), str(aistalt))

    def test_explicit_game_environment_paths_take_priority(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            factorio, aistalt = root / "factorio.exe", root / "Aistalt.exe"
            factorio.touch()
            aistalt.touch()
            with patch.dict(os.environ, {"STEAM_PATH": str(root / "MissingSteam"),
                                        "FACTORIO_PATH": str(factorio), "AISTALT_PATH": str(aistalt)}):
                self.assertEqual(detect_factorio(), str(factorio))
                self.assertEqual(detect_aistalt(), str(aistalt))
