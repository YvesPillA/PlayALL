"""Idle turns and repeat pauses must not write another native quick-save."""
import copy
import time
import unittest

from tests.test_aistalt import TemporaryGameMixin, RecordingControl


class CheckpointPolicyTests(TemporaryGameMixin, unittest.TestCase):
    def bridge_with_progress(self):
        progress = {"scene": "MainGame", "dialogue": {"line_id": "line-one", "state": "Waiting"},
                    "stats": {"day": 1, "time": "08:00"}}
        bridge = self.make_bridge()
        def responder(request):
            result = bridge.reply(request)
            if request["method"] == "observe":
                result.update(copy.deepcopy(progress))
            return result
        bridge.responder = responder
        return bridge, progress

    def test_repeated_observe_and_wait_turns_do_not_trigger_native_save(self):
        bridge, progress = self.bridge_with_progress()
        adapter = self.make_adapter(bridge)
        adapter.resume()
        adapter.observe()
        self.assertFalse(adapter.checkpoint_due())
        for _ in range(8):
            adapter.observe()
            adapter.execute({"action": "wait", "seconds": .001}, RecordingControl())
            self.assertFalse(adapter.checkpoint_due())
        self.assertNotIn("checkpoint", bridge.methods())

    def test_save_interval_and_new_progress_are_both_required_and_repeat_pause_is_deduplicated(self):
        bridge, progress = self.bridge_with_progress()
        adapter = self.make_adapter(bridge)
        adapter.observe()
        adapter.last_checkpoint = time.monotonic() - 181
        self.assertTrue(adapter.checkpoint_due())
        self.assertTrue(adapter.checkpoint()["saved"])
        self.assertEqual(bridge.methods().count("checkpoint"), 1)
        self.assertFalse(adapter.checkpoint_due())
        adapter.last_checkpoint = time.monotonic() - 181
        for _ in range(3):
            adapter.observe()
            self.assertFalse(adapter.checkpoint_due(), "elapsed time without progress must not write a save")
            self.assertTrue(adapter.checkpoint()["unchanged"])
        self.assertEqual(bridge.methods().count("checkpoint"), 1)
        progress["dialogue"]["line_id"] = "line-two"
        adapter.observe()
        self.assertTrue(adapter.checkpoint_due())
        adapter.checkpoint()
        self.assertEqual(bridge.methods().count("checkpoint"), 2)

    def test_pause_save_detects_progress_made_by_human_without_waiting_for_auto_interval(self):
        bridge, progress = self.bridge_with_progress()
        adapter = self.make_adapter(bridge)
        adapter.observe()
        adapter.checkpoint()
        progress["stats"]["day"] = 2
        adapter.pause()
        adapter.checkpoint()
        self.assertEqual(bridge.methods().count("checkpoint"), 2)
        self.assertFalse(adapter.checkpoint_due())


if __name__ == "__main__":
    unittest.main()
