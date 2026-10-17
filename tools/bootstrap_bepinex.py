"""Download the fixed upstream loader for local bridge builds, never game files."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path, PurePosixPath
import shutil
import stat
import tempfile
import urllib.request
import zipfile

ROOT = Path(__file__).resolve().parents[1]
VERSION = "5.4.23.5"
NAME = f"BepInEx_win_x64_{VERSION}.zip"
URL = f"https://github.com/BepInEx/BepInEx/releases/download/v{VERSION}/{NAME}"
SHA256 = "82f9878551030f54657792c0740d9d51a09500eeae1fba21106b0c441e6732c4"


def main():
    cache = ROOT / f".cache/bepinex-{VERSION}"
    cache.mkdir(parents=True, exist_ok=True)
    archive = cache / NAME
    if not archive.is_file() or hashlib.sha256(archive.read_bytes()).hexdigest() != SHA256:
        with tempfile.NamedTemporaryFile(dir=cache, suffix=".download", delete=False) as stream:
            temporary = Path(stream.name)
        try:
            request = urllib.request.Request(URL, headers={"User-Agent": "PlayALL-source-bootstrap"})
            with urllib.request.urlopen(request, timeout=45) as response, temporary.open("wb") as output:
                shutil.copyfileobj(response, output)
            if hashlib.sha256(temporary.read_bytes()).hexdigest() != SHA256:
                raise RuntimeError("Upstream loader digest does not match the pinned release")
            temporary.replace(archive)
        finally:
            temporary.unlink(missing_ok=True)
    package = cache / "package"
    package.mkdir(exist_ok=True)
    with zipfile.ZipFile(archive) as zipped:
        for item in zipped.infolist():
            relative = PurePosixPath(item.filename.replace("\\", "/"))
            if (relative.is_absolute() or ".." in relative.parts or ":" in item.filename
                    or stat.S_ISLNK(item.external_attr >> 16)):
                raise RuntimeError("Loader archive contains an unsafe entry")
            target = package.joinpath(*relative.parts)
            if not target.resolve().is_relative_to(package.resolve()):
                raise RuntimeError("Loader archive entry escapes the cache")
            if item.is_dir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                with zipped.open(item) as source, target.open("wb") as output:
                    shutil.copyfileobj(source, output)
    if not (package / "BepInEx/core/BepInEx.dll").is_file():
        raise RuntimeError("Pinned loader archive is missing BepInEx.dll")
    acquisition = {"name": NAME, "version": "v" + VERSION, "sha256": SHA256,
                   "upstream_digest": "sha256:" + SHA256, "source": URL}
    (cache / "acquisition.json").write_text(json.dumps(acquisition, indent=2), encoding="utf-8")
    print("Verified pinned BepInEx loader in the local ignored cache.")
    return package


if __name__ == "__main__":
    main()
