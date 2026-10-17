"""Bounded local diagnostics, deliberately excluding AI/game payloads."""
from __future__ import annotations

import json
import re
import threading
import time
import traceback
from collections import deque
from pathlib import Path


class DebugLog:
    def __init__(self, directory, max_bytes=2 * 1024 * 1024, backups=3):
        self.path = Path(directory) / "logs/playall.log"
        self.max_bytes, self.backups = max_bytes, backups
        self.lock = threading.RLock()
        self.secrets = set()
        self.memory = deque(maxlen=300)
        self.disk_error = ""

    def remember_secret(self, value):
        if isinstance(value, str) and value:
            with self.lock:
                self.secrets.add(value)

    def clean(self, value):
        if isinstance(value, dict):
            return {str(key): "[已隐藏]" if re.search(r"key|token|secret|password|authorization|credential", str(key), re.I)
                    else self.clean(item) for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [self.clean(item) for item in value[:100]]
        if not isinstance(value, str):
            return value
        with self.lock:
            for secret in sorted(self.secrets, key=len, reverse=True):
                value = value.replace(secret, "[已隐藏]")
        value = re.sub(r"(?i)Bearer\s+[^\s\"',;]+", "Bearer [已隐藏]", value)
        value = re.sub(r"(?i)([\"']?(?:api[_-]?key|token|secret|password|authorization)[\"']?\s*[:=]\s*)(?:[\"'][^\"']*[\"']|[^\s,;}]+)",
                       r"\1[已隐藏]", value)
        value = re.sub(r"\bsk-[A-Za-z0-9_-]{8,}\b", "[已隐藏]", value)
        value = re.sub(r"(?i)[A-Z]:[\\/]Users[\\/][^\\/\r\n\"']+", "%USERPROFILE%", value)
        return value

    def write(self, event, message, **details):
        with self.lock:
            record = {"time": time.strftime("%Y-%m-%d %H:%M:%S"), "event": str(event),
                      "message": self.clean(str(message))[:6000], "details": self.clean(details)}
            try:
                rendered = json.dumps(record["details"], ensure_ascii=False, default=str, allow_nan=False)
                if len(rendered) > 24000:
                    record["details"] = {"truncated": True, "summary": rendered[:24000]}
                line = json.dumps(record, ensure_ascii=False, default=str, allow_nan=False) + "\n"
            except (ValueError, TypeError):
                line = json.dumps({"time": record["time"], "event": record["event"], "message": record["message"],
                                   "details": "部分诊断数据无法编码，已省略。"}, ensure_ascii=False) + "\n"
            self.memory.append(line)
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                if self.path.exists() and self.path.stat().st_size + len(line.encode("utf-8")) > self.max_bytes:
                    for index in range(self.backups, 0, -1):
                        source = self.path if index == 1 else self.path.with_suffix(f".log.{index - 1}")
                        target = self.path.with_suffix(f".log.{index}")
                        if source.exists():
                            source.replace(target)
                with self.path.open("a", encoding="utf-8") as stream:
                    stream.write(line)
                self.disk_error = ""
            except OSError:
                self.disk_error = "日志暂时无法写入磁盘，最近记录保留在内存。"

    def exception(self, event, error):
        self.write(event, str(error), traceback="".join(traceback.format_exception(type(error), error, error.__traceback__)))

    def read(self):
        with self.lock:
            if self.disk_error:
                return self.disk_error + "\n" + "".join(self.memory)
            chunks = []
            budget = 256 * 1024
            for index in range(self.backups + 1):
                path = self.path if index == 0 else self.path.with_suffix(f".log.{index}")
                try:
                    with path.open("rb") as stream:
                        stream.seek(max(0, path.stat().st_size - budget))
                        raw = stream.read(budget)
                    chunks.insert(0, raw.decode("utf-8", errors="replace"))
                    budget -= len(raw)
                    if budget <= 0:
                        break
                except OSError:
                    continue
            return self.clean("".join(chunks)) or "还没有调试记录。"

    def bridge_tail(self, exe):
        """Only our plugin/loader lines; do not export the game's full log."""
        parts = []
        try:
            path = Path(exe).parent / "BepInEx/LogOutput.log"
            with path.open("rb") as stream:
                stream.seek(max(0, path.stat().st_size - 512 * 1024))
                lines = stream.read(512 * 1024).decode("utf-8", errors="replace").splitlines()
            selected = [line for line in lines if "PlayALL" in line or "Chainloader" in line or "BepInEx" in line]
            if selected:
                parts.append("\n".join(selected[-160:]))
        except OSError:
            pass
        try:
            path = Path(exe).parent / "BepInEx/plugins/PlayALL/lifecycle.log"
            with path.open("rb") as stream:
                stream.seek(max(0, path.stat().st_size - 128 * 1024))
                raw = stream.read(128 * 1024).decode("utf-8", errors="replace")
            parts.append("—— 连接组件生命周期 ——\n" + "\n".join(raw.splitlines()[-80:]))
        except OSError:
            pass
        return self.clean("\n".join(parts)) if parts else "尚未生成游戏连接组件日志。"
