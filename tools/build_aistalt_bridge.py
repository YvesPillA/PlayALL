"""Compile our bridge against the selected game's existing managed API."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("game", type=Path)
    args = parser.parse_args()
    game = args.game.resolve()
    managed = game / "Aistalt_Data/Managed"
    if not (managed / "InfantGod.dll").is_file():
        raise ValueError("Choose the installed game's Build directory containing Aistalt_Data/Managed.")
    subprocess.run([sys.executable, str(ROOT / "tools/bootstrap_bepinex.py")], cwd=ROOT, check=True)
    loader = ROOT / ".cache/bepinex-5.4.23.5/package"
    output = ROOT / "mods/aistalt-bridge"
    compiler = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Microsoft.NET/Framework64/v4.0.30319/csc.exe"
    references = [managed / name for name in (
        "mscorlib.dll", "netstandard.dll", "System.dll", "System.Core.dll",
        "UnityEngine.dll", "UnityEngine.CoreModule.dll", "UnityEngine.InputLegacyModule.dll",
        "FairyGUI.dll", "Newtonsoft.Json.dll", "InfantGod.dll", "EasySave3.dll",
        "YarnSpinner.dll", "YarnSpinner.Unity.dll",
    )] + [loader / "BepInEx/core/BepInEx.dll"]
    command = [str(compiler), "/nologo", "/noconfig", "/utf8output", "/target:library", "/optimize+", "/nostdlib+",
               "/out:" + str(output / "PlayALL.Aistalt.dll")]
    command += ["/reference:" + str(path) for path in references]
    command.append(str(output / "PlayALL.Aistalt.cs"))
    subprocess.run(command, cwd=ROOT, check=True)
    shutil.copytree(loader, output / "loader", dirs_exist_ok=True)
    digest = lambda path: hashlib.sha256(path.read_bytes()).hexdigest()
    manifest = {"protocol": 1, "game": "aistalt", "bridge_version": "0.1.5",
                "source_sha256": digest(output / "PlayALL.Aistalt.cs"),
                "game_sha256": digest(managed / "InfantGod.dll"),
                "bridge_sha256": digest(output / "PlayALL.Aistalt.dll"),
                "loader": json.loads((ROOT / ".cache/bepinex-5.4.23.5/acquisition.json").read_text()),
                "game_assemblies_bundled": False}
    (output / "build.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(manifest, ensure_ascii=False))


if __name__ == "__main__":
    main()
