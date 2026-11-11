from __future__ import annotations

import base64
import hashlib
import importlib.util
import json
import socket
import struct
import threading
from pathlib import Path

MODULE_PATH = Path(__file__).resolve().parents[3] / "wu_tools" / "cdp_close.py"
SPEC = importlib.util.spec_from_file_location("cdp_close", MODULE_PATH)
assert SPEC and SPEC.loader
cdp_close = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(cdp_close)

GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


def _recv_until(conn: socket.socket, marker: bytes) -> bytes:
    data = b""
    while marker not in data:
        chunk = conn.recv(4096)
        if not chunk:
            break
        data += chunk
    return data


def _recv_exact(conn: socket.socket, size: int) -> bytes:
    data = b""
    while len(data) < size:
        chunk = conn.recv(size - len(data))
        if not chunk:
            raise ConnectionError("closed")
        data += chunk
    return data


class _FakeCdpServer:
    """同一端口先应答 /json/version，再完成一次 WebSocket 握手并收帧。"""

    def __init__(self) -> None:
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", 0))
        self.port = self.sock.getsockname()[1]
        self.sock.listen(4)
        self.payload: dict | None = None
        self.done = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True)

    def start(self) -> "_FakeCdpServer":
        self._thread.start()
        return self

    def _serve(self) -> None:
        try:
            for _ in range(2):
                conn, _ = self.sock.accept()
                with conn:
                    request = _recv_until(conn, b"\r\n\r\n")
                    head = request.split(b"\r\n\r\n", 1)[0].decode("latin-1")
                    if head.startswith("GET /json/version"):
                        body = json.dumps(
                            {
                                "Browser": "Edg/130.0",
                                "webSocketDebuggerUrl": f"ws://127.0.0.1:{self.port}/devtools/browser/test",
                            }
                        ).encode()
                        conn.sendall(
                            b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                            + f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode()
                            + body
                        )
                        continue
                    headers = {}
                    for line in head.split("\r\n")[1:]:
                        name, _, value = line.partition(":")
                        headers[name.strip().lower()] = value.strip()
                    key = headers.get("sec-websocket-key", "")
                    accept = base64.b64encode(
                        hashlib.sha1((key + GUID).encode()).digest()
                    ).decode()
                    conn.sendall(
                        (
                            "HTTP/1.1 101 Switching Protocols\r\n"
                            "Upgrade: websocket\r\n"
                            "Connection: Upgrade\r\n"
                            f"Sec-WebSocket-Accept: {accept}\r\n\r\n"
                        ).encode()
                    )
                    first, second = _recv_exact(conn, 2)
                    length = second & 0x7F
                    if length == 126:
                        length = struct.unpack("!H", _recv_exact(conn, 2))[0]
                    elif length == 127:
                        length = struct.unpack("!Q", _recv_exact(conn, 8))[0]
                    mask = _recv_exact(conn, 4)
                    raw = _recv_exact(conn, length)
                    payload = bytes(byte ^ mask[index % 4] for index, byte in enumerate(raw))
                    assert first & 0x0F == 0x1
                    self.payload = json.loads(payload.decode("utf-8"))
                    conn.sendall(b"\x88\x00")  # close frame
        finally:
            try:
                self.sock.close()
            except OSError:
                pass
            self.done.set()

    def stop(self) -> None:
        try:
            self.sock.close()
        except OSError:
            pass


def test_close_browser_sends_browser_close_over_stdlib_websocket():
    server = _FakeCdpServer().start()
    try:
        code = cdp_close.close_browser(server.port, timeout=2.0, wait_seconds=2.0)
        assert server.done.wait(2.0)
    finally:
        server.stop()
    assert code == 0
    assert server.payload == {"id": 9001, "method": "Browser.close"}


def test_close_browser_without_cdp_port_returns_3():
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    assert cdp_close.fetch_version(port, timeout=0.2) is None
    assert cdp_close.close_browser(port, timeout=0.2, wait_seconds=0.2) == 3


def test_close_browser_main_exit_codes(capsys):
    server = _FakeCdpServer().start()
    try:
        code = cdp_close.main(["--port", str(server.port), "--timeout", "2", "--wait", "2"])
        assert server.done.wait(2.0)
    finally:
        server.stop()
    assert code == 0
    assert "CDP Browser.close" in capsys.readouterr().out
