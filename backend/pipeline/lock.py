"""单实例锁：data/run.lock，记录 PID；异常退出后按 PID 探活自动失效。"""

from __future__ import annotations

import json
import os
import socket
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterator


class AlreadyRunning(RuntimeError):
    def __init__(self, info: dict):
        self.info = info
        pid = info.get("pid", "?")
        started = info.get("started_at", "?")
        super().__init__(f"已有运行中的实例 (PID={pid}, 开始于 {started})")


def pid_alive(pid: int) -> bool:
    if not isinstance(pid, int) or pid <= 0:
        return False
    if os.name == "nt":  # pragma: no cover - Windows 分支由 Xuan 本机执行
        import ctypes

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return False
            return code.value == STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


@dataclass
class RunLock:
    path: Path
    token: str
    pid: int

    def release(self) -> None:
        try:
            info = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            info = {}
        if info.get("token") == self.token and info.get("pid") == self.pid:
            try:
                self.path.unlink()
            except FileNotFoundError:
                pass


def _read_info(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def acquire_lock(path: Path, *, command: str = "") -> RunLock:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    token = uuid.uuid4().hex
    payload = {
        "pid": os.getpid(),
        "token": token,
        "started_at": datetime.now().isoformat(timespec="seconds"),
        "host": socket.gethostname(),
        "command": command,
    }
    for attempt in range(2):
        try:
            fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            info = _read_info(path)
            pid = info.get("pid")
            if isinstance(pid, int) and pid_alive(pid) and pid != os.getpid():
                raise AlreadyRunning(info) from None
            if isinstance(pid, int) and pid == os.getpid() and attempt == 0:
                # 同一进程重复进入（例如测试或嵌套调用）也视为已在运行，
                # 但允许清理本进程上一轮异常遗留的锁。
                raise AlreadyRunning(info) from None
            try:
                path.unlink()
            except FileNotFoundError:
                pass
            if attempt == 0:
                continue
            raise AlreadyRunning(info) from None
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False)
                handle.flush()
                os.fsync(handle.fileno())
        except Exception:
            try:
                path.unlink()
            except FileNotFoundError:
                pass
            raise
        return RunLock(path=path, token=token, pid=os.getpid())
    raise AlreadyRunning(_read_info(path))


@contextmanager
def run_lock(path: Path, *, command: str = "") -> Iterator[RunLock]:
    lock = acquire_lock(path, command=command)
    try:
        yield lock
    finally:
        lock.release()


__all__ = ["AlreadyRunning", "RunLock", "acquire_lock", "pid_alive", "run_lock"]
