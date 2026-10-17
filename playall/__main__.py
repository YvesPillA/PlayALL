from __future__ import annotations

import argparse
import json
import signal
import sys
import threading
import urllib.request
import webbrowser
from pathlib import Path

from .server import LocalServer


def legacy_web_main():
    parser = argparse.ArgumentParser(description="PlayALL — 本地 AI 游戏终端")
    parser.add_argument("--port", type=int, default=7860)
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--data-dir", type=Path)
    args = parser.parse_args()
    try:
        server = LocalServer(args.port, args.data_dir)
    except OSError:
        url = f"http://127.0.0.1:{args.port}"
        try:
            with urllib.request.urlopen(url + "/api/health", timeout=2) as response:
                if json.load(response).get("app") != "PlayALL":
                    raise ValueError("另一个程序正在使用此端口。")
            print("PlayALL 已在运行：" + url, flush=True)
            if not args.no_browser:
                webbrowser.open(url)
            return
        except Exception:
            print(f"端口 {args.port} 被占用。请关闭占用程序，或使用 --port 7861。", file=sys.stderr)
            raise SystemExit(1)
    print("PlayALL 已启动：" + server.url, flush=True)
    print("关闭此窗口或按 Ctrl+C 会停止 AI 并保存游戏。", flush=True)
    if not args.no_browser:
        webbrowser.open(server.url)
    def shutdown(signum, frame):
        server.runner.stop()
        threading.Thread(target=server.shutdown, daemon=True).start()
    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)
    try:
        server.serve_forever(poll_interval=0.2)
    finally:
        server.runner.stop()
        if server.runner.thread:
            server.runner.thread.join(timeout=55)
        server.server_close()


def main():
    from .desktop import main as desktop_main
    return desktop_main()


if __name__ == "__main__":
    raise SystemExit(main())
