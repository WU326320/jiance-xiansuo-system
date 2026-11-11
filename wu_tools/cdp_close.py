#!/usr/bin/env python3
"""通过 CDP WebSocket 优雅关闭本脚本自己启动的 Edge/Chrome。

背景：``wu_validate.ps1`` 原来用 PowerShell ``Add-Type`` 加载
``System.Net.WebSockets.Client`` 发 ``Browser.close``，在 WU 的
PowerShell 5.1 上会报“无法添加类型。缺少一个或多个所需程序集”。
这里改用 crawler 自带 Python 的标准库实现最小 CDP 客户端：

  1. ``GET http://127.0.0.1:<port>/json/version`` 拿 ``webSocketDebuggerUrl``；
  2. 标准库 socket 完成 RFC6455 握手（不依赖 websocket-client / websockets）；
  3. 发一帧 ``{"id":9001,"method":"Browser.close"}``；
  4. 轮询端口直到 CDP 消失。

只连接 127.0.0.1 / localhost，只发 Browser.close，不做任何进程操作
（不 taskkill / Stop-Process）。退出码：

  0  已发送，端口随后关闭
  1  已发送，但端口仍在监听（保留进程，交给调用方记录 WARN）
  2  连接 / 握手 / 帧协议错误
  3  端口没有 CDP（没有进程或不是浏览器调试端口）
  4  /json/version 没有 webSocketDebuggerUrl
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import socket
import struct
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}
_MAX_HEADER_BYTES = 64 * 1024


class CdpError(RuntimeError):
    """CDP 连接/握手/协议错误。"""


def _json_url(port: int) -> str:
    return f"http://127.0.0.1:{int(port)}/json/version"


def fetch_version(port: int, *, timeout: float = 3.0) -> dict | None:
    """读取 /json/version；端口没开或不是 CDP 时返回 None。"""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(_json_url(port), timeout=timeout) as response:
            if not 200 <= int(getattr(response, "status", 0) or 0) < 300:
                return None
            payload = response.read(1024 * 1024)
    except (urllib.error.URLError, OSError, ValueError):
        return None
    try:
        value = json.loads(payload.decode("utf-8", errors="replace"))
    except (TypeError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _recv_exact(sock: socket.socket, size: int) -> bytes:
    chunks: list[bytes] = []
    remaining = size
    while remaining > 0:
        chunk = sock.recv(remaining)
        if not chunk:
            raise CdpError("WebSocket 连接被对端关闭")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _recv_until(sock: socket.socket, marker: bytes, *, limit: int = _MAX_HEADER_BYTES) -> bytes:
    buffer = bytearray()
    while marker not in buffer:
        chunk = sock.recv(4096)
        if not chunk:
            raise CdpError("WebSocket 握手响应提前结束")
        buffer.extend(chunk)
        if len(buffer) > limit:
            raise CdpError(f"WebSocket 握手响应超过 {limit} 字节")
    return bytes(buffer)


def _validate_ws_url(ws_url: str) -> tuple[str, int, str]:
    parsed = urllib.parse.urlsplit(ws_url)
    if parsed.scheme not in {"ws", "wss"}:
        raise CdpError(f"不支持的 WebSocket scheme: {parsed.scheme}")
    host = parsed.hostname or ""
    if host not in LOOPBACK_HOSTS:
        raise CdpError(f"拒绝连接非回环 CDP 地址: {host}")
    port = parsed.port or (443 if parsed.scheme == "wss" else 80)
    path = parsed.path or "/"
    if parsed.query:
        path += "?" + parsed.query
    return host, int(port), path


def _ws_connect(ws_url: str, *, timeout: float = 5.0) -> socket.socket:
    """最小 RFC6455 客户端握手（文本帧、客户端掩码）。"""
    host, port, path = _validate_ws_url(ws_url)
    sock = socket.create_connection((host, port), timeout=timeout)
    key = base64.b64encode(os.urandom(16)).decode("ascii")
    request = (
        f"GET {path} HTTP/1.1\r\n"
        f"Host: {host}:{port}\r\n"
        "Upgrade: websocket\r\n"
        "Connection: Upgrade\r\n"
        f"Sec-WebSocket-Key: {key}\r\n"
        "Sec-WebSocket-Version: 13\r\n"
        "\r\n"
    )
    try:
        sock.sendall(request.encode("ascii"))
        response = _recv_until(sock, b"\r\n\r\n")
        header_text = response.split(b"\r\n\r\n", 1)[0].decode("latin-1")
        lines = header_text.split("\r\n")
        if not lines or " 101 " not in f" {lines[0]} ":
            raise CdpError(f"WebSocket 握手失败: {lines[0] if lines else '空响应'}")
        headers = {}
        for line in lines[1:]:
            name, sep, value = line.partition(":")
            if sep:
                headers[name.strip().lower()] = value.strip()
        expected = base64.b64encode(
            hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode("ascii")).digest()
        ).decode("ascii")
        if headers.get("sec-websocket-accept") != expected:
            raise CdpError("WebSocket 握手 Sec-WebSocket-Accept 校验失败")
        if headers.get("upgrade", "").lower() != "websocket":
            raise CdpError("WebSocket 握手缺少 Upgrade: websocket")
    except Exception:
        sock.close()
        raise
    return sock


def _send_text(sock: socket.socket, text: str) -> None:
    payload = text.encode("utf-8")
    mask = os.urandom(4)
    header = bytearray([0x81])
    length = len(payload)
    if length < 126:
        header.append(0x80 | length)
    elif length < 65536:
        header.append(0x80 | 126)
        header.extend(struct.pack("!H", length))
    else:
        header.append(0x80 | 127)
        header.extend(struct.pack("!Q", length))
    header.extend(mask)
    masked = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
    sock.sendall(bytes(header) + masked)


def _recv_frame(sock: socket.socket) -> tuple[int, bytes]:
    first, second = _recv_exact(sock, 2)
    opcode = first & 0x0F
    masked = bool(second & 0x80)
    length = second & 0x7F
    if length == 126:
        length = struct.unpack("!H", _recv_exact(sock, 2))[0]
    elif length == 127:
        length = struct.unpack("!Q", _recv_exact(sock, 8))[0]
    if length > 16 * 1024 * 1024:
        raise CdpError(f"WebSocket 帧过大: {length}")
    mask = _recv_exact(sock, 4) if masked else b""
    payload = _recv_exact(sock, length)
    if masked:
        payload = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
    return opcode, payload


def close_browser(port: int, *, timeout: float = 5.0, wait_seconds: float = 10.0) -> int:
    """对指定 CDP 端口发 Browser.close，返回退出码（见模块 docstring）。"""
    version = fetch_version(port, timeout=min(3.0, timeout))
    if version is None:
        return 3
    ws_url = str(version.get("webSocketDebuggerUrl") or "").strip()
    if not ws_url:
        return 4
    sock: socket.socket | None = None
    try:
        sock = _ws_connect(ws_url, timeout=timeout)
        _send_text(sock, '{"id":9001,"method":"Browser.close"}')
        # 浏览器可能在回包前就关闭连接；读到 close/EOF 都算发送成功。
        try:
            sock.settimeout(min(2.0, timeout))
            opcode, payload = _recv_frame(sock)
            if opcode == 0x8:
                pass
            elif payload:
                pass
        except (CdpError, OSError, socket.timeout):
            pass
    except (CdpError, OSError, socket.timeout) as exc:
        print(f"CDP close error port={port}: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    finally:
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass

    deadline = time.monotonic() + max(0.1, float(wait_seconds))
    while time.monotonic() < deadline:
        if fetch_version(port, timeout=0.5) is None:
            return 0
        time.sleep(0.5)
    return 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="通过 CDP WebSocket 优雅关闭本脚本启动的浏览器")
    parser.add_argument("--port", type=int, required=True, help="CDP 调试端口（例如 9222）")
    parser.add_argument("--timeout", type=float, default=5.0, help="连接/握手超时秒数，默认 5")
    parser.add_argument("--wait", type=float, default=10.0, help="等待端口关闭秒数，默认 10")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not 1 <= int(args.port) <= 65535:
        print(f"端口非法: {args.port}", file=sys.stderr)
        return 2
    code = close_browser(int(args.port), timeout=float(args.timeout), wait_seconds=float(args.wait))
    print(f"CDP Browser.close port={args.port} exit={code}")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
