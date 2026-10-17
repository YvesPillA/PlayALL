"""Verify native confirmation barriers on an explicitly selected MainMenu process.

No confirmed important action is executed, no new game/save is loaded, and no
model is called. This script never starts or closes the user's game process.
"""
import argparse
import json
from pathlib import Path
import sys
import tempfile
import time
from contextlib import contextmanager

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from playall.games.aistalt import Aistalt
from playall.processes import inspect_process
from playall.storage import atomic_json


class TestControl:
    def check(self):
        pass

    def sleep(self, seconds):
        time.sleep(min(seconds, .2))

    @contextmanager
    def action(self):
        yield


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("pid", type=int)
    args = parser.parse_args()
    selected = inspect_process(args.pid)
    if not selected or selected.name.casefold() != "aistalt.exe":
        raise RuntimeError("The explicitly selected game is no longer running")
    evidence = {"kind": "real Unity MainMenu confirmation barriers", "model_called": False,
        "important_game_action_executed": False, "game_save_requested": False, "checks": []}
    with tempfile.TemporaryDirectory(prefix="playall-native-confirmation-") as directory:
        game = Aistalt(Path(directory), {"game_process": selected.selection()}, lambda *args, **kwargs: None)
        control = TestControl()
        try:
            game.prepare(control)
            game.connect_window(control)
            assert game._call("hello").get("confirmations") is True
            game.resume()
            initial = game.observe()
            if initial["scene"] != "MainMenu":
                raise RuntimeError("This test only operates on MainMenu; it will not touch gameplay")
            new = next(c for c in initial["controls"] if c["name"] == "Btn_NewGame" and c["enabled"])
            settings = next(c for c in initial["controls"] if c["name"] == "Btn_Settings" and c["enabled"])
            action = {"action": "click", "control_id": new["id"], "view_revision": initial["view_revision"]}
            review = game.review_action(action)
            assert review["requires_confirmation"] and review["operation"] == "new_game_menu"
            game.pause()
            try:
                game.resume()
            except (ValueError, RuntimeError) as error:
                assert "确认" in str(error) or "回复" in str(error)
            else:
                raise AssertionError("Native Resume bypassed pending confirmation")
            assert not game.execute({"action": "wait", "seconds": .001}, control)["ok"]
            assert game.reject_action(review["ticket"])["ok"]
            game.resume()
            current = game.observe()
            assert current["scene"] == "MainMenu" and [c["id"] for c in current["controls"]] == [c["id"] for c in initial["controls"]]
            evidence["checks"].append("pending review blocks native Resume and subsequent Execute; rejection preserves MainMenu")
            action["view_revision"] = current["view_revision"]
            review = game.review_action(action)
            game.pause()
            grant = game.approve_action(review["ticket"])
            assert grant["ok"] and grant["approval"] != review["ticket"]
            game.resume()
            current = game.observe()
            mismatch = {"action": "click", "control_id": settings["id"], "view_revision": current["view_revision"]}
            result = game.execute_approved(mismatch, grant["approval"], control)
            assert not result["ok"] and result.get("reason") == "stale_confirmation"
            final = game.observe()
            assert final["scene"] == "MainMenu" and [c["id"] for c in final["controls"]] == [c["id"] for c in initial["controls"]]
            evidence["checks"].append("an approval cannot click a different control; no button was actuated")
            evidence.update(status="passed", bridge_version=game.build["bridge_version"],
                bridge_sha256=game.build["bridge_sha256"], scene=final["scene"],
                controls=len(final["controls"]), confirmation_controls=[c["name"] for c in final["controls"] if c.get("requires_confirmation")])
        finally:
            game.pause()
            game.close()
    atomic_json(ROOT / "data/qa/aistalt/native-confirmations.json", evidence)
    print(json.dumps(evidence, ensure_ascii=False))


if __name__ == "__main__":
    main()
