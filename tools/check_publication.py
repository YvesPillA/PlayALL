"""Check the exact Git index for private/generated files and common secrets.

Only filenames, line numbers and rule names are printed. Review the staged
diff as well; this heuristic check cannot identify every kind of private data.
"""
from __future__ import annotations

import argparse
from pathlib import Path, PurePosixPath
import re
import subprocess

FORBIDDEN_ROOTS = {"data", ".cache", ".venv", "_internal", "build", "dist", "screenshots"}
FORBIDDEN_SUFFIXES = {".exe", ".dll", ".pdb", ".zip", ".dmp", ".bak", ".pem", ".key", ".p12", ".pfx", ".pyc"}
FORBIDDEN_NAMES = {"settings.json", "endpoint.json", "config.ini", "credentials.json", "secrets.json"}
PATTERNS = {
    "provider-key": re.compile(r"\bsk-[A-Za-z0-9_-]{20,}"),
    "github-token": re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})"),
    "cloud-key": re.compile(r"\b(?:AKIA[0-9A-Z]{16}|AIza[0-9A-Za-z_-]{30,}|xox[baprs]-[0-9A-Za-z-]{20,})"),
    "private-key": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    "personal-profile-path": re.compile(r"(?i)[a-z]:[\\/]+Users[\\/]+(?!Example User[\\/]|Public[\\/]|Default[\\/])[^\s\"']+"),
    "email-address": re.compile(r"[A-Za-z0-9._%+-]+@([A-Za-z0-9.-]+\.[A-Za-z]{2,})"),
}


def scan_file(relative, blob):
    path = PurePosixPath(relative)
    parts = [p.casefold() for p in path.parts]
    findings = []
    if (not parts or parts[0] in FORBIDDEN_ROOTS or path.suffix.casefold() in FORBIDDEN_SUFFIXES
            or path.name.casefold() in FORBIDDEN_NAMES or path.name.startswith(".env")
            or path.name.startswith("handoff") or path.name == "next-human-checks.md"
            or parts[:3] == ["mods", "aistalt-bridge", "loader"]
            or parts == ["mods", "aistalt-bridge", "build.json"]
            or parts[:2] == ["docs", "issues"] or "__pycache__" in parts
            or re.search(r"\.log(?:\.|$)", path.name, re.I)):
        findings.append((0, "private-or-generated-file"))
    try:
        text = blob.decode("utf-8-sig")
    except UnicodeDecodeError:
        return findings + [(0, "non-text-file-needs-review")]
    if b"\0" in blob:
        findings.append((0, "binary-file-needs-review"))
    for number, line in enumerate(text.splitlines(), 1):
        for rule, pattern in PATTERNS.items():
            for match in pattern.finditer(line):
                if rule == "email-address" and (match.group(1).endswith((".test", ".invalid"))
                        or match.group(1) in {"example.com", "users.noreply.github.com"}):
                    continue
                findings.append((number, rule))
    return findings


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    root = parser.parse_args().root.resolve()
    listed = subprocess.run(["git", "ls-files", "--stage", "-z"], cwd=root, check=True, capture_output=True).stdout
    entries = [entry for entry in listed.split(b"\0") if entry]
    if not entries:
        parser.error("Git index is empty; stage the intended source files first")
    failed = False
    for entry in entries:
        metadata, raw_path = entry.split(b"\t", 1)
        mode, _, stage = metadata.split()
        relative = raw_path.decode("utf-8")
        if stage != b"0" or mode not in (b"100644", b"100755"):
            print(f"{relative}:0: unreviewed-index-entry")
            failed = True
            continue
        blob = subprocess.run(["git", "show", ":" + relative], cwd=root, check=True, capture_output=True).stdout
        for line, rule in scan_file(relative, blob):
            print(f"{relative}:{line}: {rule}")
            failed = True
    print(("FAILED" if failed else "PASSED") + f": publication check of {len(entries)} indexed files")
    return int(failed)


if __name__ == "__main__":
    raise SystemExit(main())
