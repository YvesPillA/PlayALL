"""Read-only local process selection. No command lines, windows or injection."""
from __future__ import annotations

import ctypes
import os
import socket
from dataclasses import asdict, dataclass
from pathlib import Path
from ctypes import wintypes


@dataclass(frozen=True)
class GameProcess:
    pid: int
    name: str
    path: str = ""
    started: int = 0

    def selection(self):
        return asdict(self)


def _kernel():
    from ctypes import wintypes
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
    kernel.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
    kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    return kernel


def inspect_process(pid):
    """Query only executable identity and creation time, with limited access."""
    if os.name != "nt" or type(pid) is not int or pid <= 0:
        return None
    from ctypes import wintypes
    kernel = _kernel()
    handle = kernel.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return None
    try:
        code = wintypes.DWORD()
        if not kernel.GetExitCodeProcess(handle, ctypes.byref(code)) or code.value != 259:
            return None
        buffer, size = ctypes.create_unicode_buffer(32768), wintypes.DWORD(32768)
        if not kernel.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
            return None
        times = [wintypes.FILETIME() for _ in range(4)]
        if not kernel.GetProcessTimes(handle, *(ctypes.byref(value) for value in times)):
            return None
        created = (times[0].dwHighDateTime << 32) | times[0].dwLowDateTime
        return GameProcess(pid, Path(buffer.value).name, buffer.value, created)
    finally:
        kernel.CloseHandle(handle)


def list_processes():
    if os.name != "nt":
        return []
    from ctypes import wintypes
    class Entry(ctypes.Structure):
        _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
                    ("th32ProcessID", wintypes.DWORD), ("th32DefaultHeapID", ctypes.c_size_t),
                    ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
                    ("th32ParentProcessID", wintypes.DWORD), ("pcPriClassBase", wintypes.LONG),
                    ("dwFlags", wintypes.DWORD), ("szExeFile", wintypes.WCHAR * 260)]
    kernel = _kernel()
    kernel.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    kernel.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(Entry)]
    kernel.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(Entry)]
    handle = kernel.CreateToolhelp32Snapshot(2, 0)
    if handle == ctypes.c_void_p(-1).value:
        raise OSError("无法读取本机进程列表，请稍后刷新。")
    result = []
    try:
        entry = Entry()
        entry.dwSize = ctypes.sizeof(entry)
        valid = kernel.Process32FirstW(handle, ctypes.byref(entry))
        while valid:
            if entry.th32ProcessID:
                result.append(GameProcess(entry.th32ProcessID, entry.szExeFile))
            valid = kernel.Process32NextW(handle, ctypes.byref(entry))
    finally:
        kernel.CloseHandle(handle)
    return sorted(result, key=lambda value: (value.name.casefold(), value.pid))


def validate_selection(value, executable_name):
    if not isinstance(value, dict) or set(value) != {"pid", "name", "path", "started"}:
        raise ValueError("请先选择已经打开的游戏进程。")
    if (type(value["pid"]) is not int or value["pid"] <= 0 or type(value["started"]) is not int
            or value["started"] <= 0 or not isinstance(value["path"], str) or not value["path"]
            or not isinstance(value["name"], str) or value["name"].casefold() != executable_name.casefold()
            or Path(value["path"]).name.casefold() != executable_name.casefold()):
        raise ValueError("所选进程与游戏适配器不匹配，请重新选择。")
    current = inspect_process(value["pid"])
    if (not current or current.started != value["started"]
            or os.path.normcase(os.path.abspath(current.path)) != os.path.normcase(os.path.abspath(value["path"]))):
        raise ValueError("所选游戏已退出或重新启动，请重新选择进程。")
    return current


def tcp_listener_pid(port):
    """Read the owner of one IPv4 loopback listener; no process command lines."""
    if os.name != "nt" or type(port) is not int or not 1 <= port <= 65535:
        return None
    class Row(ctypes.Structure):
        _fields_ = [(name, wintypes.DWORD) for name in
                    ("state", "local_addr", "local_port", "remote_addr", "remote_port", "pid")]
    api = ctypes.WinDLL("iphlpapi", use_last_error=True).GetExtendedTcpTable
    api.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.DWORD), wintypes.BOOL,
                    wintypes.ULONG, ctypes.c_int, wintypes.ULONG]
    api.restype = wintypes.DWORD
    size = wintypes.DWORD()
    code = api(None, ctypes.byref(size), False, 2, 3, 0)  # AF_INET, OWNER_PID_LISTENER
    if code not in (0, 122) or not 4 <= size.value <= 16 * 1024 * 1024:
        return None
    buffer = ctypes.create_string_buffer(size.value)
    if api(buffer, ctypes.byref(size), False, 2, 3, 0):
        return None
    count = ctypes.cast(buffer, ctypes.POINTER(wintypes.DWORD)).contents.value
    if 4 + count * ctypes.sizeof(Row) > size.value:
        return None
    loopback = int.from_bytes(socket.inet_aton("127.0.0.1"), "little")
    for index in range(count):
        row = Row.from_buffer(buffer, 4 + index * ctypes.sizeof(Row))
        if row.local_addr == loopback and socket.ntohs(row.local_port & 0xffff) == port:
            return row.pid
    return None


class OwnedProcesses:
    def __init__(self):
        self.handle = None
        if os.name != "nt":
            return
        class BasicLimits(ctypes.Structure):
            _fields_ = [("process_time", ctypes.c_int64), ("job_time", ctypes.c_int64),
                        ("flags", wintypes.DWORD), ("min_working", ctypes.c_size_t),
                        ("max_working", ctypes.c_size_t), ("active_limit", wintypes.DWORD),
                        ("affinity", ctypes.c_size_t), ("priority", wintypes.DWORD), ("scheduling", wintypes.DWORD)]
        class IO(ctypes.Structure):
            _fields_ = [(name, ctypes.c_uint64) for name in ("read_ops", "write_ops", "other_ops", "read_bytes", "write_bytes", "other_bytes")]
        class ExtendedLimits(ctypes.Structure):
            _fields_ = [("basic", BasicLimits), ("io", IO), ("process_memory", ctypes.c_size_t),
                        ("job_memory", ctypes.c_size_t), ("peak_process_memory", ctypes.c_size_t), ("peak_job_memory", ctypes.c_size_t)]
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self.kernel.CreateJobObjectW.argtypes = (ctypes.c_void_p, wintypes.LPCWSTR)
        self.kernel.CreateJobObjectW.restype = wintypes.HANDLE
        self.kernel.SetInformationJobObject.argtypes = (wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD)
        self.kernel.AssignProcessToJobObject.argtypes = (wintypes.HANDLE, wintypes.HANDLE)
        self.kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
        self.handle = self.kernel.CreateJobObjectW(None, None)
        if not self.handle:
            raise ctypes.WinError(ctypes.get_last_error())
        limits = ExtendedLimits()
        limits.basic.flags = 0x2000
        if not self.kernel.SetInformationJobObject(self.handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
            self.close()
            raise ctypes.WinError(ctypes.get_last_error())

    def add(self, process):
        if self.handle and not self.kernel.AssignProcessToJobObject(self.handle, wintypes.HANDLE(int(process._handle))):
            error = ctypes.WinError(ctypes.get_last_error())
            process.terminate()
            process.wait(timeout=10)
            raise error

    def close(self):
        if self.handle:
            self.kernel.CloseHandle(self.handle)
            self.handle = None
