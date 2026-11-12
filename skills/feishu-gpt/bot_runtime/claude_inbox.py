"""Send newline-delimited peer messages to a live Windows Claude Code inbox."""

import ctypes
import hashlib
import json
import os
import re
from ctypes import wintypes
from pathlib import Path


PIPE_PATTERN = re.compile(r"\\\\\.\\pipe\\LOCAL\\cc-msg-[0-9a-f]{32}", re.IGNORECASE)
MAX_MESSAGE_BYTES = 200_000


class InboxDeliveryUncertain(RuntimeError):
    pass


def _kernel32():
    if os.name != "nt":
        raise RuntimeError("Claude inbox 接入目前仅支持本机 Windows。")
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
    kernel.GetProcessTimes.restype = wintypes.BOOL
    kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel.GetExitCodeProcess.restype = wintypes.BOOL
    kernel.GetNamedPipeServerProcessId.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.ULONG)]
    kernel.GetNamedPipeServerProcessId.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    return kernel


def process_start(pid: int) -> str:
    kernel = _kernel32()
    handle = kernel.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        raise RuntimeError("Claude 进程已退出或无法查询。")
    try:
        times = [wintypes.FILETIME() for _ in range(4)]
        code = wintypes.DWORD()
        if not kernel.GetProcessTimes(handle, *(ctypes.byref(t) for t in times)):
            raise RuntimeError("无法核对 Claude 进程启动时间。")
        if not kernel.GetExitCodeProcess(handle, ctypes.byref(code)) or code.value != 259:
            raise RuntimeError("Claude 进程已退出。")
        return str((times[0].dwHighDateTime << 32) | times[0].dwLowDateTime)
    finally:
        kernel.CloseHandle(handle)


def _read_record(path: Path) -> dict:
    if path.is_symlink() or path.stat().st_size > 64_000:
        raise ValueError("Claude 注册记录无效。")
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("Claude 注册记录必须是 JSON 对象。")
    return data


def resolve_session(session_id: str, registry: Path | None = None) -> tuple[dict, str]:
    registry = registry or Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude") / "sessions"
    matches = []
    for path in registry.glob("*.json"):
        if not path.stem.isdigit():
            continue
        try:
            session = _read_record(path)
        except (OSError, ValueError):
            continue
        if session.get("sessionId") == session_id and str(session.get("pid")) == path.stem:
            matches.append(session)
    if not matches:
        raise RuntimeError("目标 Claude session 未运行；请在客户端打开原会话后重试。")
    if len(matches) != 1:
        raise RuntimeError("同一 Claude session 有多个注册实例，无法确定投递目标。")
    session = matches[0]
    pid = session.get("pid")
    pipe = session.get("messagingSocketPath", "")
    if type(pid) is not int or pid <= 0 or not isinstance(pipe, str) or not PIPE_PATTERN.fullmatch(pipe):
        raise RuntimeError("目标没有可用的本机 Claude inbox。")
    if session.get("peerProtocol") != 1:
        raise RuntimeError("Claude inbox 协议版本未受支持。")
    if not session.get("procStart") or process_start(pid) != session["procStart"]:
        raise RuntimeError("Claude 注册记录已过期，请重新打开原会话。")
    # Claude canonicalizes Windows pipe paths to lowercase before hashing.
    digest = hashlib.sha256(pipe.lower().encode("utf-8")).hexdigest()
    key = _read_record(registry / f"{pid}.{digest}.key")
    if (key.get("procStartFt") != session["procStart"] or not session.get("pidDomain")
            or key.get("pidDomain") != session["pidDomain"]):
        raise RuntimeError("Claude inbox 认证记录与运行实例不匹配。")
    token = key.get("peerToken")
    if not isinstance(token, str) or not re.fullmatch(r"[0-9a-f]{32,128}", token):
        raise RuntimeError("Claude inbox 认证记录无效。")
    return session, token


def _write_pipe(session: dict, payload: bytes, timeout_ms: int = 5000):
    import _winapi

    pipe = session["messagingSocketPath"]
    _winapi.WaitNamedPipe(pipe, timeout_ms)
    handle = _winapi.CreateFile(
        pipe, _winapi.GENERIC_READ | _winapi.GENERIC_WRITE, 0, 0,
        _winapi.OPEN_EXISTING, _winapi.FILE_FLAG_OVERLAPPED, 0,
    )
    try:
        kernel = _kernel32()
        server_pid = wintypes.ULONG()
        if not kernel.GetNamedPipeServerProcessId(handle, ctypes.byref(server_pid)):
            raise RuntimeError("无法核对 Claude inbox 所属进程。")
        if server_pid.value != session["pid"] or process_start(server_pid.value) != session["procStart"]:
            raise RuntimeError("Claude inbox 所属进程已变化，未投递消息。")
        # Use a raw byte write, not multiprocessing's framed/message-mode protocol.
        try:
            operation, error = _winapi.WriteFile(handle, payload, overlapped=True)
            try:
                if error == _winapi.ERROR_IO_PENDING:
                    result = _winapi.WaitForSingleObject(operation.event, timeout_ms)
                    if result != _winapi.WAIT_OBJECT_0:
                        operation.cancel()
                        operation.GetOverlappedResult(True)
                        raise TimeoutError("inbox 写入超时")
                written, error = operation.GetOverlappedResult(True)
                if error or written != len(payload):
                    raise OSError("inbox 写入不完整")
            except BaseException:
                operation.cancel()
                operation.GetOverlappedResult(True)
                raise
        except Exception as exc:
            raise InboxDeliveryUncertain("Claude inbox 投递结果不确定，请先查看原会话，勿自动重发。") from exc
    finally:
        _winapi.CloseHandle(handle)


def send_claude_message(session_id: str, text: str, registry: Path | None = None):
    if os.name != "nt":
        raise RuntimeError("Claude inbox 接入目前仅支持本机 Windows。")
    if not text.strip():
        raise ValueError("Claude 消息不能为空。")
    message = json.dumps({"type": "user", "message": {"role": "user", "content": text}}, ensure_ascii=False)
    if len(message.encode("utf-8")) > MAX_MESSAGE_BYTES:
        raise ValueError("Claude 话题消息过长，请拆分后发送。")
    session, token = resolve_session(session_id, registry)
    payload = (json.dumps({"type": "auth", "token": token}) + "\n" + message + "\n").encode("utf-8")
    _write_pipe(session, payload)
