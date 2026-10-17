from __future__ import annotations

import socket
import struct
import threading
import time
import unittest

from playall.games.rcon import Rcon, RconError


def packet(request_id, kind, body=b""):
    payload = struct.pack("<ii", request_id, kind) + body + b"\0\0"
    return struct.pack("<i", len(payload)) + payload


def read_exact(connection, size):
    result = bytearray()
    while len(result) < size:
        chunk = connection.recv(size - len(result))
        if not chunk:
            raise EOFError("The test client disconnected before completing its packet")
        result.extend(chunk)
    return bytes(result)


def read_packet(connection):
    length = struct.unpack("<i", read_exact(connection, 4))[0]
    payload = read_exact(connection, length)
    if payload[-2:] != b"\0\0":
        raise AssertionError("Client packet was missing terminators")
    return (*struct.unpack("<ii", payload[:8]), payload[8:-2])


def send_fragmented(connection, payload):
    # Split the header, IDs, terminators and body over independent TCP writes.
    while payload:
        connection.sendall(payload[:3])
        payload = payload[3:]
        time.sleep(0.001)


class ScriptedRconServer:
    def __init__(self, script):
        self.listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.listener.settimeout(3)
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen(1)
        self.port = self.listener.getsockname()[1]
        self.connection = None
        self.error = None

        def run():
            try:
                self.connection, _ = self.listener.accept()
                self.connection.settimeout(3)
                script(self.connection)
            except BaseException as error:
                self.error = error
            finally:
                if self.connection:
                    self.connection.close()

        self.thread = threading.Thread(target=run, daemon=True, name="Test RCON server")
        self.thread.start()

    def finish(self):
        self.thread.join(3)
        if self.thread.is_alive():
            raise AssertionError("Test RCON server did not finish")
        if self.error:
            raise self.error

    def close(self):
        self.listener.close()
        if self.connection:
            try:
                self.connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            self.connection.close()
        self.thread.join(3)


class FragmentedMemorySocket:
    def __init__(self, wire):
        self.wire = bytearray(wire)

    def recv(self, count):
        count = min(count, 2)
        result = bytes(self.wire[:count])
        del self.wire[:count]
        return result


class RconTests(unittest.TestCase):
    def server(self, script):
        server = ScriptedRconServer(script)
        self.addCleanup(server.close)
        return server

    def test_fragmented_multipacket_utf8_response_and_barrier_do_not_mix_commands(self):
        expected = "铁矿 × 12；煤炭 × 8 🐱\n冶炼完成"

        def script(connection):
            self.assertEqual(read_packet(connection), (1, 3, b"test-password"))
            send_fragmented(connection, packet(1, 0) + packet(1, 2))
            for wanted, answer in (("first-command", expected), ("second-command", "第二轮正确")):
                request_id, kind, body = read_packet(connection)
                self.assertEqual((kind, body), (2, wanted.encode("utf-8")))
                self.assertEqual(read_packet(connection), (request_id + 1, 2, b"/players online"))
                raw = answer.encode("utf-8")
                # Index 1 intentionally bisects the first three-byte Chinese character.
                replies = packet(request_id, 0, raw[:1]) + packet(request_id, 0, raw[1:8])
                replies += packet(request_id, 0, raw[8:])
                replies += packet(request_id + 1, 0, b"Players online (0):")
                send_fragmented(connection, replies)

        server = self.server(script)
        client = Rcon("127.0.0.1", server.port, "test-password", timeout=3)
        self.addCleanup(client.close)
        self.assertEqual(client.command("first-command"), expected)
        self.assertEqual(client.command("second-command"), "第二轮正确")
        server.finish()

    def test_authentication_failure_raises_and_closes_connection(self):
        def script(connection):
            self.assertEqual(read_packet(connection), (1, 3, b"wrong-test-password"))
            send_fragmented(connection, packet(-1, 2))
            self.assertEqual(connection.recv(1), b"", "Failed authentication must close its socket")

        server = self.server(script)
        with self.assertRaisesRegex(RconError, "认证失败"):
            Rcon("127.0.0.1", server.port, "wrong-test-password", timeout=3)
        server.finish()

    def test_exact_reads_fragmented_headers_and_rejects_invalid_sizes_and_terminators(self):
        client = object.__new__(Rcon)
        client.socket = FragmentedMemorySocket(packet(42, 0, "完整数据".encode("utf-8")))
        self.assertEqual(client._receive(), (42, 0, "完整数据".encode("utf-8")))
        for wire in (struct.pack("<i", 9), struct.pack("<i", 4 * 1024 * 1024 + 1), packet(2, 0, b"body")[:-2] + b"xx"):
            with self.subTest(wire=wire), self.assertRaises(RconError):
                client.socket = FragmentedMemorySocket(wire)
                client._receive()

    def test_disconnect_in_the_middle_of_packet_is_explicit(self):
        client = object.__new__(Rcon)
        client.socket = FragmentedMemorySocket(packet(2, 0, b"body")[:-1])
        with self.assertRaisesRegex(RconError, "断开"):
            client._receive()


if __name__ == "__main__":
    unittest.main()
