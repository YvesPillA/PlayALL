"""Check the specific verification save after the human loads it normally."""
import argparse
import json
from pathlib import Path
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from playall.games.factorio_attach import Factorio, digest, profile_paths
from playall.processes import inspect_process
from playall.storage import atomic_json


class Control:
    def check(self):
        pass


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("pid", type=int)
    parser.add_argument("evidence", type=Path)
    parser.add_argument("--output", type=Path, default=ROOT / "data/qa/factorio/reload.json")
    args = parser.parse_args()
    prior = json.loads(args.evidence.read_text(encoding="utf-8"))
    expected = prior["expected_reload"]
    selected = inspect_process(args.pid)
    if not selected or selected.name.casefold() != "factorio.exe":
        raise ValueError("Selected game is no longer running")
    _, data = profile_paths(Path(selected.path))
    save = data / "saves" / prior["restore_save_name"]
    assert digest(save) == prior["restore_save_sha256"], "The requested restore archive has changed"
    with tempfile.TemporaryDirectory(prefix="playall-reload-check-") as directory:
        game = Factorio(Path(directory), {"game_process": selected.selection()}, lambda *a: None)
        try:
            game.prepare(Control())
            game.connect_window(Control())
            actual = game.observe()
            assert actual["paused"] and actual["manual_control"], "Reload unexpectedly started AI"
            assert actual["character_id"] == expected["character_id"], "Different character"
            assert actual["inventory"] == expected["inventory"], "Inventory did not match saved state"
            assert actual["position"] == expected["position"], "Position did not match saved state"
            assert actual["iron_plates_produced"] == expected["iron_plates_produced"], "Production statistic changed"
            if expected.get("production"):
                assert actual["production"] == expected["production"], "Saved ore or plate production changed"
            machines = {b.get("id"): b for b in actual["buildings"]}
            for before in expected["buildings"]:
                after = machines.get(before.get("id"))
                if not after:
                    response = game.execute({"action": "inspect", "x": before["x"], "y": before["y"]}, Control())
                    after = response.get("entity") if response.get("ok") else None
                    assert after and after.get("id") == before.get("id"), "Saved entity outside observation area was not found"
                assert after and all(after.get(k) == before.get(k) for k in ("name", "x", "y", "direction")), "A saved building was lost or changed"
                if before.get("recipe"):
                    assert after.get("recipe") == before["recipe"], "A saved recipe changed"
                if prior.get("quiescent_inventory_check"):
                    assert all(after.get(k, {}) == before.get(k, {}) for k in ("contents", "fuel", "input", "output", "lab", "ammo", "modules")), "An idle machine inventory changed"
            for name, researched in expected.get("research_status", {}).items():
                response = game.execute({"action": "inspect", "name": name}, Control())
                assert response.get("ok") and response["technology"]["researched"] == researched, "Saved research changed"
            result = {"status": "passed", "kind": "actual GUI save/reload round trip, human performed load",
                "restore_save_name": prior["restore_save_name"], "source_gameplay_evidence": str(args.evidence),
                "character_preserved": True, "inventory_preserved": True, "position_preserved": True,
                "buildings_preserved": True, "production_preserved": True, "ai_remained_paused": True,
                "research_and_recipes_preserved": bool(expected.get("research_status")),
                "assistant_loaded_game": False, "assistant_sent_game_actions": False, "paid_ai_called": False}
            atomic_json(args.output, result)
            print(json.dumps(result, ensure_ascii=False, indent=2))
        finally:
            game.close()


if __name__ == "__main__":
    main()
