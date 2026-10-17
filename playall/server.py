from __future__ import annotations

import json
import mimetypes
import re
import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import __version__
from .config import ROOT, Settings
from .llm import make_ai
from .runner import Runner


class LocalServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, port=7860, directory=None):
        self.settings = Settings(directory)
        self.runner = Runner(self.settings)
        self.token = secrets.token_urlsafe(32)
        self.dialog_lock = threading.Lock()
        super().__init__(("127.0.0.1", port), Handler)

    @property
    def url(self):
        return f"http://127.0.0.1:{self.server_port}"


class Handler(BaseHTTPRequestHandler):
    server: LocalServer
    protocol_version = "HTTP/1.1"

    def log_message(self, format, *args):
        # Access paths never need to be persisted alongside private settings.
        pass

    def setup(self):
        super().setup()
        self.connection.settimeout(55)

    def reply(self, data, status=200, content_type="application/json; charset=utf-8"):
        payload = json.dumps(data, ensure_ascii=False, allow_nan=False).encode("utf-8") if not isinstance(data, bytes) else data
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
        self.end_headers()
        try:
            self.wfile.write(payload)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def trusted(self):
        allowed = {f"127.0.0.1:{self.server.server_port}", f"localhost:{self.server.server_port}"}
        if self.headers.get("Host") not in allowed:
            self.reply({"error": "只允许通过本机地址访问。"}, 403)
            return False
        origin = self.headers.get("Origin")
        if origin and origin not in {"http://" + host for host in allowed}:
            self.reply({"error": "请求来源不受信任。"}, 403)
            return False
        return True

    def do_GET(self):
        if not self.trusted():
            return
        request = urlparse(self.path)
        try:
            if request.path == "/api/health":
                self.reply({"app": "PlayALL", "version": __version__})
            elif request.path == "/api/bootstrap":
                self.reply({"token": self.server.token, "settings": self.server.settings.public(),
                            "state": self.server.runner.snapshot(), "history": self.server.runner.history()})
            elif request.path == "/api/state":
                cursor = int(parse_qs(request.query).get("after", ["0"])[0])
                self.reply(self.server.runner.snapshot(cursor))
            elif request.path == "/api/history":
                self.reply({"runs": self.server.runner.history()})
            elif request.path == "/api/diagnostics":
                settings = self.server.settings.public()
                self.reply({"version": __version__, "factorio_detected": settings["factorio_detected"],
                            "save_exists": (self.server.settings.directory / "factorio/saves/playall.zip").exists(),
                            "data_directory": str(self.server.settings.directory), "rea_available": (ROOT / ".cache/rea-runtime/node_modules/rea-agents/scripts/rea.mjs").exists()})
            else:
                relative = "index.html" if request.path == "/" else request.path.lstrip("/")
                path = (ROOT / "web" / relative).resolve()
                if not path.is_relative_to((ROOT / "web").resolve()) or not path.is_file():
                    self.reply({"error": "没有找到此页面。"}, 404)
                    return
                media = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
                if path.suffix in (".html", ".css", ".js", ".svg"):
                    media += "; charset=utf-8"
                self.reply(path.read_bytes(), content_type=media)
        except (ValueError, OSError) as error:
            self.reply({"error": str(error)[:500]}, 400)

    def do_POST(self):
        if not self.trusted():
            return
        if not secrets.compare_digest(self.headers.get("X-PlayALL-Token", ""), self.server.token):
            self.reply({"error": "会话已过期，请刷新页面。"}, 403)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 <= length <= 65536:
                raise ValueError("请求内容过大。")
            if "application/json" not in self.headers.get("Content-Type", ""):
                raise ValueError("请求需要使用 JSON。")
            data = json.loads(self.rfile.read(length) or b"{}")
            if not isinstance(data, dict):
                raise ValueError("请求内容格式不正确。")
            path = urlparse(self.path).path
            runner = self.server.runner
            if path == "/api/settings":
                result = {"ok": True, "settings": self.server.settings.update(data)}
            elif path == "/api/start":
                result = runner.start(data)
            elif path == "/api/pause":
                result = runner.pause()
            elif path == "/api/resume":
                result = runner.resume()
            elif path == "/api/stop":
                result = runner.stop()
            elif path == "/api/probe":
                profile = self.server.settings.profile(data.get("provider", "deepseek"))
                result = make_ai(profile).probe()
            elif path == "/api/watch":
                if not runner.game:
                    raise ValueError("请先开始一局。")
                result = runner.game.watch()
            elif path == "/api/pick-factorio":
                with self.server.dialog_lock:
                    try:
                        import tkinter as tk
                        from tkinter import filedialog
                        root = tk.Tk()
                        root.withdraw()
                        root.attributes("-topmost", True)
                        try:
                            filename = filedialog.askopenfilename(parent=root, title="选择 Factorio 的 factorio.exe", filetypes=[("Factorio", "factorio.exe"), ("应用程序", "*.exe")])
                        finally:
                            root.destroy()
                    except ImportError:
                        raise ValueError("系统没有文件选择器，请直接粘贴 factorio.exe 的完整路径。") from None
                result = {"ok": True, "path": filename}
            else:
                self.reply({"error": "没有找到此操作。"}, 404)
                return
            self.reply(result)
        except (ValueError, OSError, RuntimeError, TypeError, KeyError) as error:
            self.reply({"error": str(error)[:3000]}, 400)
