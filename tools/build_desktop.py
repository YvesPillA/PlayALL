"""Build a portable native Windows application; never bundles user data."""
import os
import argparse
import shutil
import subprocess
import sys
from pathlib import Path

root = Path(__file__).resolve().parents[1]
parser = argparse.ArgumentParser()
parser.add_argument("--stage-only", action="store_true", help="Build without replacing the installed application")
args = parser.parse_args()
sys.path.insert(0, str(root))
from PySide6.QtWidgets import QApplication
from playall.desktop import app_icon

app = QApplication([])
assets = root / "assets"
assets.mkdir(exist_ok=True)
app_icon().pixmap(128, 128).save(str(assets / "playall.png"))
# ICO stores the same code-drawn mark at Windows' common icon sizes.
from PIL import Image
Image.open(assets / "playall.png").save(assets / "playall.ico", sizes=[(16,16),(32,32),(48,48),(64,64),(128,128)])

command = [sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--windowed", "--name", "PlayALL",
    "--icon", str(assets / "playall.ico"), "--distpath", str(root / ".cache/package"),
    "--workpath", str(root / ".cache/build"), "--specpath", str(root / ".cache"),
    "--add-data", str(root / "mods") + os.pathsep + "mods", "--collect-submodules", "playall.games",
    "--exclude-module", "PySide6.QtWebEngineWidgets", "--exclude-module", "PySide6.QtWebEngineCore",
    "--exclude-module", "PySide6.QtQml", "--exclude-module", "PySide6.QtQuick", str(root / "desktop_run.py")]
subprocess.run(command, cwd=root, check=True)
package = root / ".cache/package/PlayALL"
# Qt 6.12 uses Windows' unversioned ICU API. A different ICU on PATH (for
# example Node's ICU 78) is wrongly collected by bindepend and lacks that API.
# Leave this OS dependency to Windows' own loader instead of shadowing it.
for base in ((package / "_internal",) if args.stage_only else (package / "_internal", root / "_internal")):
    for name in ("icuuc.dll", "icudt78.dll"):
        (base / name).unlink(missing_ok=True)
if not args.stage_only:
    shutil.copy2(package / "PlayALL.exe", root / "PlayALL.exe")
    shutil.copytree(package / "_internal", root / "_internal", dirs_exist_ok=True)
print("BUILT", package / "PlayALL.exe" if args.stage_only else root / "PlayALL.exe", flush=True)
