"""Install REA locally in .cache; leaves global agent settings untouched."""
import base64
import hashlib
import io
import json
import subprocess
import tarfile
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
cache = ROOT / ".cache"
cache.mkdir(exist_ok=True)
npm = cache / "npm/package/bin/npm-cli.js"
if not npm.exists():
    with urllib.request.urlopen("https://registry.npmjs.org/npm/latest", timeout=30) as response:
        metadata = json.load(response)
    with urllib.request.urlopen(metadata["dist"]["tarball"], timeout=60) as response:
        raw = response.read()
    expected = metadata["dist"]["integrity"]
    actual = "sha512-" + base64.b64encode(hashlib.sha512(raw).digest()).decode("ascii")
    if actual != expected:
        raise RuntimeError("npm integrity check failed")
    with tarfile.open(fileobj=io.BytesIO(raw), mode="r:gz") as archive:
        archive.extractall(cache / "npm", filter="data")
result = subprocess.run(["node", str(npm), "install", "--prefix", str(cache / "rea-runtime"),
                         "--ignore-scripts", "--no-audit", "--no-fund", "rea-agents@6.4.0"])
raise SystemExit(result.returncode)
