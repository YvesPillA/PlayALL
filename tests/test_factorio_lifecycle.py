from __future__ import annotations

import threading
import unittest
import zipfile
from unittest.mock import Mock

from playall.games.factorio import Factorio
from tests.support import IsolatedSettingsMixin


class FactorioLifecycleTests(IsolatedSettingsMixin, unittest.TestCase):
    def stub_game(self):
        # Bypass __init__: no ports, Windows job objects or game processes exist.
        game = object.__new__(Factorio)
        game.lock = threading.RLock()
        game.rcon = Mock()
        game.process = Mock()
        game.process.poll.return_value = None
        game.viewer = Mock()
        game.viewer.poll.return_value = None
        game.owned = Mock()
        game.log_file = Mock()
        game.cancel = Mock()
        game.save_path = self.directory / "playall-test-save.zip"
        return game

    def test_failed_checkpoint_keeps_live_process_rcon_viewer_and_ownership_for_retry(self):
        game = self.stub_game()
        rcon, process, viewer, owned, log_file = game.rcon, game.process, game.viewer, game.owned, game.log_file
        game.checkpoint = Mock(side_effect=RuntimeError("synthetic checkpoint failure"))
        with self.assertRaisesRegex(RuntimeError, "synthetic checkpoint failure"):
            game.close()
        self.assertIs(game.rcon, rcon)
        self.assertIs(game.process, process)
        self.assertIs(game.viewer, viewer)
        self.assertIs(game.owned, owned)
        rcon.close.assert_not_called()
        process.terminate.assert_not_called()
        process.wait.assert_not_called()
        process.stdin.write.assert_not_called()
        viewer.terminate.assert_not_called()
        owned.close.assert_not_called()
        log_file.close.assert_not_called()

        game.checkpoint.side_effect = None
        process.wait.side_effect = lambda **kwargs: setattr(process.poll, "return_value", 0)
        viewer.wait.side_effect = lambda **kwargs: setattr(viewer.poll, "return_value", 0)
        game.close()
        self.assertIsNone(game.rcon)
        self.assertIsNone(game.process)
        rcon.close.assert_called_once()
        process.stdin.write.assert_called_once_with(b"/quit\n")
        viewer.terminate.assert_called_once()
        owned.close.assert_called_once()
        log_file.close.assert_called_once()
        self.assertEqual(game.checkpoint.call_count, 2)

    def test_explicit_server_save_failure_does_not_replace_previous_valid_archive(self):
        game = self.stub_game()
        with zipfile.ZipFile(game.save_path, "w") as archive:
            archive.writestr("test-save/level.dat", b"synthetic old game state")
        before = game.save_path.read_bytes()
        game.rcon.command.return_value = "Cannot save: synthetic disk failure"
        with self.assertRaisesRegex(RuntimeError, "拒绝保存"):
            game.checkpoint()
        self.assertEqual(game.save_path.read_bytes(), before)
        game.rcon.command.assert_called_once_with("/server-save playall")


if __name__ == "__main__":
    unittest.main()
