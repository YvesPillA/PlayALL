"""Source RCON framing, including Factorio multi-packet replies."""
from __future__ import annotations

import socket
import struct
import threading


class RconError(RuntimeError):
    pass


class Rcon:
    def __init__(self, host: str, port: int, password: str, timeout: float = 8):
        self.socket = socket.create_connection((host, port), timeout)
        self.socket.settimeout(timeout)
        self.lock = threading.RLock()
        self.sequence = 10
        try:
            self._send(1, 3, password)
            for _ in range(4):
                request_id, kind, _ = self._receive()
                if request_id == -1:
                    raise RconError("Factorio 远程控制认证失败。")
                if request_id == 1 and kind == 2:
                    break
            else:
                raise RconError("Factorio 没有确认远程控制认证。")
        except Exception:
            self.close()
            raise

    def _send(self, request_id, kind, body):
        payload = struct.pack("<ii", request_id, kind) + body.encode("utf-8") + b"\x00\x00"
        self.socket.sendall(struct.pack("<i", len(payload)) + payload)

    def _exact(self, count):
        chunks = bytearray()
        while len(chunks) < count:
            data = self.socket.recv(count - len(chunks))
            if not data:
                raise RconError("Factorio 的连接已断开。")
            chunks.extend(data)
        return bytes(chunks)

    def _receive(self):
        size = struct.unpack("<i", self._exact(4))[0]
        if not 10 <= size <= 4 * 1024 * 1024:
            raise RconError("Factorio 返回了无效的数据包。")
        data = self._exact(size)
        if data[-2:] != b"\x00\x00":
            raise RconError("Factorio 数据包不完整。")
        request_id, kind = struct.unpack("<ii", data[:8])
        return request_id, kind, data[8:-2]

    def command(self, body: str) -> str:
        with self.lock:
            self.sequence += 2
            request_id = self.sequence
            # Factorio ignores an empty command. A read-only command provides an
            # explicit response barrier after every chunk of the requested reply.
            self._send(request_id, 2, body)
            self._send(request_id + 1, 2, "/players online")
            chunks = []
            size = 0
            for _ in range(4096):
                current_id, _, data = self._receive()
                if current_id == request_id + 1:
                    return b"".join(chunks).decode("utf-8", errors="replace")
                if current_id == request_id:
                    chunks.append(data)
                    size += len(data)
                    if size > 8 * 1024 * 1024:
                        raise RconError("Factorio 回复超出大小限制。")
            raise RconError("Factorio 回复未结束。")

    def close(self):
        try:
            self.socket.close()
        except OSError:
            pass
