from __future__ import annotations

import unittest

from playall.games.actions import tools_for_game, validate_action


class ActionValidationTests(unittest.TestCase):
    def test_valid_action_is_copied_and_tool_names_are_restricted(self):
        action = {"action": "build", "name": "stone-furnace", "x": 4, "y": 0, "direction": "east"}
        validated = validate_action(action)
        self.assertEqual(validated, action)
        self.assertIsNot(validated, action)
        self.assertEqual({tool["function"]["name"] for tool in tools_for_game()}, {"game_action", "report_progress"})
        for tool in tools_for_game():
            self.assertFalse(tool["function"]["parameters"]["additionalProperties"])

    def test_unknown_code_and_missing_required_action_parameters_are_rejected(self):
        invalid = (None, [], {}, {"action": "shell"},
                   {"action": "observe", "lua": "game.print('injected')"},
                   {"action": "move", "x": 0}, {"action": "build", "x": 0, "y": 0},
                   {"action": "mine", "name": ""},
                   {"action": "build", "name": "stone-furnace", "x": 0, "y": 0, "direction": "up"})
        for action in invalid:
            with self.subTest(action=action), self.assertRaises(ValueError):
                validate_action(action)

    def test_numeric_limits_reject_boolean_nonfinite_fractional_and_excessive_values(self):
        invalid = (
            {"action": "move", "x": True, "y": 0},
            {"action": "move", "x": float("nan"), "y": 0},
            {"action": "move", "x": 0, "y": float("inf")},
            {"action": "move", "x": 100001, "y": 0},
            {"action": "mine", "name": "coal", "count": 0},
            {"action": "mine", "name": "coal", "count": 1.5},
            {"action": "mine", "name": "coal", "count": 101},
            {"action": "mine", "name": "coal", "count": True},
            {"action": "wait", "seconds": 0}, {"action": "wait", "seconds": 121},
        )
        for action in invalid:
            with self.subTest(action=action), self.assertRaises(ValueError):
                validate_action(action)


if __name__ == "__main__":
    unittest.main()
