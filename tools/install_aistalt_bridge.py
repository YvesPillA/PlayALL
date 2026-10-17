"""Install the built bridge without launching or controlling the game."""
import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from playall.games.aistalt import install_bridge, sha256
from playall.storage import atomic_json
from playall.processes import list_processes


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("game", type=Path)
    args = parser.parse_args()
    exe = args.game.resolve()
    if exe.is_dir():
        exe = exe / "Aistalt.exe"
    if any(process.name.casefold() == "aistalt.exe" for process in list_processes()):
        raise RuntimeError("Please save and close Aistalt before preparing its connection component.")
    bundle = ROOT / "mods/aistalt-bridge"
    build = json.loads((bundle / "build.json").read_text(encoding="utf-8"))
    if sha256(exe.parent / "Aistalt_Data/Managed/InfantGod.dll") != build["game_sha256"]:
        raise RuntimeError("The selected game differs from the analyzed build.")
    if sha256(bundle / "PlayALL.Aistalt.dll") != build["bridge_sha256"]:
        raise RuntimeError("The built bridge failed its integrity check.")
    originals = [exe, exe.parent / "UnityPlayer.dll", exe.parent / "Aistalt_Data/Managed/InfantGod.dll",
                 exe.parent / "Aistalt_Data/Managed/Assembly-CSharp.dll"]
    before = {str(path): sha256(path) for path in originals}
    plugin = install_bridge(exe, bundle)
    after = {str(path): sha256(path) for path in originals}
    if before != after:
        raise RuntimeError("Original game file identities changed during installation.")
    result = {"status": "installed", "plugin": str(plugin), "bridge_sha256": build["bridge_sha256"],
              "original_files_unchanged": before == after, "original_files": after,
              "game_launched": False, "runtime_verified": False}
    atomic_json(ROOT / "data/qa/aistalt/installation.json", result)
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
