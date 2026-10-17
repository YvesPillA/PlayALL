from __future__ import annotations

import unittest
from unittest.mock import patch

from playall.games import make_game
from playall.games.registry import GameSpec, game_specs, register_game
from tests.support import IsolatedSettingsMixin


class DemoAdapter:
    def __init__(self, directory, settings, emit):
        self.directory, self.settings, self.emit = directory, settings, emit


def demo_tools():
    return [{"type": "function", "function": {"name": "game_action", "parameters": {
        "type": "object", "properties": {"action": {"enum": ["advance"]}},
    }}}]


def demo_validator(action):
    if action != {"action": "advance"}:
        raise ValueError("Only this demo game's action is permitted")
    return dict(action)


class GameRegistryTests(IsolatedSettingsMixin, unittest.TestCase):
    def test_installed_game_supplies_its_own_factory_rules_tools_and_validator(self):
        with patch.dict("playall.games.registry._games", {}, clear=False):
            spec = GameSpec("test-demo", "独立测试游戏", f"{__name__}:DemoAdapter", "Demo-specific rules",
                            f"{__name__}:demo_tools", f"{__name__}:demo_validator")
            register_game(spec)
            public = self.settings.update({"game": "test-demo"})
            self.assertEqual(public["game"], "test-demo")
            adapter = make_game("test-demo", self.directory, self.settings.snapshot(), lambda *args: None)
            self.assertIsInstance(adapter, DemoAdapter)
            self.assertEqual(adapter.directory, self.directory)
            self.assertEqual(game_specs()["test-demo"].rules, "Demo-specific rules")
            self.assertEqual(spec.tools(), demo_tools())
            action = {"action": "advance"}
            self.assertEqual(spec.validate(action), action)
            self.assertIsNot(spec.validate(action), action)
            with self.assertRaises(ValueError):
                spec.validate({"action": "mine", "name": "iron-ore"})
        self.assertNotIn("test-demo", game_specs())

    def test_registry_snapshot_is_detached_and_duplicate_or_invalid_ids_are_rejected(self):
        detached = game_specs()
        detached.clear()
        self.assertIn("factorio", game_specs())
        with self.assertRaises(ValueError):
            register_game(game_specs()["factorio"])
        with self.assertRaises(ValueError):
            register_game(GameSpec("../unsafe", "Bad", "tests.test_registry:DemoAdapter", ""))
        with self.assertRaises(ValueError):
            make_game("missing-adapter", self.directory, {}, lambda *args: None)


if __name__ == "__main__":
    unittest.main()
