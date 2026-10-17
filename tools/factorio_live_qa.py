"""Read-only check of an explicitly selected Factorio single-player process.

Never starts/closes the game, submits game actions, saves or requests AI.
Run after the user has opened the map, with PlayALL disconnected.
"""
import argparse
import json
from pathlib import Path
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from playall.games.factorio_attach import Factorio, BUNDLE, BRIDGE_VERSION, digest
from playall.processes import inspect_process
from playall.storage import atomic_json


class Control:
    def check(self):
        pass


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("pid", type=int)
    args = parser.parse_args()
    selected = inspect_process(args.pid)
    if not selected or selected.name.casefold() != "factorio.exe":
        raise ValueError("The selected Factorio process is no longer running")
    with tempfile.TemporaryDirectory(prefix="playall-readonly-factorio-") as directory:
        game = Factorio(Path(directory), {"game_process": selected.selection()}, lambda *args: None)
        try:
            game.prepare(Control())
            game.connect_window(Control())
            connection = game.rcon
            before = game.observe()
            assert before["paused"] and before["manual_control"]
            game.connect_window(Control())
            assert game.rcon is connection
            time.sleep(.25)
            after = game.observe()
            assert before["character_id"] == after["character_id"]
            evidence = {"kind": "final adapter, actual single-player, read-only", "status": "passed",
                "pid": selected.pid, "version": "2.0.77", "bridge": BRIDGE_VERSION, "source_sha256": digest(BUNDLE / "control.lua"),
                "same_character_preserved": True, "character_id": after["character_id"],
                "position": after["position"], "inventory": after["inventory"], "resource_types": len(after["resources"]),
                "manual_control": after["manual_control"], "ticks": [before["tick"], after["tick"]],
                "connection_reused": True, "game_action_submitted": False, "save_requested": False, "ai_requested": False}
            atomic_json(ROOT / "data/qa/factorio/final-readonly.json", evidence)
            print(json.dumps(evidence, ensure_ascii=False, indent=2))
        finally:
            game.close()


if __name__ == "__main__":
    main()
