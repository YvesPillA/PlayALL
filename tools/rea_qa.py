"""Use REA's owned browser lifecycle for screenshots and runtime evidence."""
import base64
import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "data/qa"
OUT.mkdir(parents=True, exist_ok=True)
REA = ROOT / ".cache/rea-runtime/node_modules/rea-agents/scripts/rea.mjs"
BROWSER = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"


def run_case(name, width, height, actions):
    scenario = {"browser": {"mode": "launch", "executable_path": BROWSER, "headless": True},
                "start_url": {"url": "http://127.0.0.1:7860"},
                "environment": {"viewport": {"width": width, "height": height, "device_scale_factor": 1},
                                "locale": "zh-CN", "timezone": "Asia/Hong_Kong", "reduced_motion": "reduce"},
                "actions": [{"step_id": "ready", "action": "wait_for", "locator": {"kind": "css", "selector": "#game-note.ready"}, "state": "visible", "timeout_ms": 15000}, *actions],
                "capture": {"after_each_step": ["screenshot"], "at_end": ["screenshot", "dom", "url"], "events": ["console", "page-errors"]}}
    input_path = OUT / f"{name}-scenario.json"
    input_path.write_text(json.dumps(scenario, ensure_ascii=False, indent=2), encoding="utf-8")
    output_path = OUT / f"{name}-rea.json"
    with output_path.open("w", encoding="utf-8") as output:
        result = subprocess.run(["node", str(REA), "capture-browser-scenario", str(input_path), "--format", "json"],
                                stdout=output, stderr=subprocess.PIPE, text=True, encoding="utf-8", timeout=90, cwd=ROOT)
    if result.returncode:
        raise RuntimeError(result.stderr[-2500:] + output_path.read_text(encoding="utf-8")[:1000])
    evidence = json.loads(output_path.read_text(encoding="utf-8-sig"))
    images = []
    def visit(value, path=""):
        if isinstance(value, dict):
            image_value = value.get("base64") or value.get("data_base64") or value.get("bytes_base64")
            if isinstance(image_value, str) and image_value.startswith("iVBOR"):
                dest = OUT / f"{name}-{len(images)+1}.png"
                dest.write_bytes(base64.b64decode(image_value))
                images.append(str(dest))
            for key, item in value.items():
                visit(item, path + "/" + key)
        elif isinstance(value, list):
            for i, item in enumerate(value):
                visit(item, path + "/" + str(i))
    visit(evidence)
    print(json.dumps({"case": name, "evidence": evidence.get("evidence_id"), "result_keys": list(evidence.get("normalized_result", {})), "images": images}, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    run_case("desktop", 1440, 1050, [])
    run_case("settings", 1440, 1050, [{"step_id": "settings", "action": "click", "locator": {"kind": "css", "selector": "#settings-button"}}])
    run_case("mobile", 390, 844, [{"step_id": "below", "action": "press", "locator": {"kind": "css", "selector": "body"}, "key": "PageDown"}])
