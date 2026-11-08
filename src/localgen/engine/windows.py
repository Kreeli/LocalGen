"""Launch a Windows process already assigned to a kill-on-close Job Object."""
import ctypes as c
import logging
import os
import socket
import subprocess
import threading
from ctypes import wintypes as w
from pathlib import Path

import psutil

logger = logging.getLogger("localgen.comfy.process")


class Startup(c.Structure):
    _fields_ = [
        ("cb", w.DWORD), ("lpReserved", w.LPWSTR), ("lpDesktop", w.LPWSTR), ("lpTitle", w.LPWSTR),
        *[(key, w.DWORD) for key in ("dwX", "dwY", "dwXSize", "dwYSize", "dwXCountChars",
                                      "dwYCountChars", "dwFillAttribute", "dwFlags")],
        ("wShowWindow", w.WORD), ("cbReserved2", w.WORD), ("lpReserved2", c.c_void_p),
        ("hStdInput", w.HANDLE), ("hStdOutput", w.HANDLE), ("hStdError", w.HANDLE),
    ]


class StartupEx(c.Structure):
    _fields_ = [("startup", Startup), ("attributes", c.c_void_p)]


class ProcessInfo(c.Structure):
    _fields_ = [("process", w.HANDLE), ("thread", w.HANDLE), ("pid", w.DWORD), ("tid", w.DWORD)]


class Tcp4Owner(c.Structure):
    _fields_ = [("state", w.DWORD), ("address", c.c_ubyte * 4), ("port", w.DWORD),
                ("remote_address", c.c_ubyte * 4), ("remote_port", w.DWORD), ("pid", w.DWORD)]


class Tcp6Owner(c.Structure):
    _fields_ = [("address", c.c_ubyte * 16), ("scope", w.DWORD), ("port", w.DWORD),
                ("remote_address", c.c_ubyte * 16), ("remote_scope", w.DWORD), ("remote_port", w.DWORD),
                ("state", w.DWORD), ("pid", w.DWORD)]


def launch(job, handles, command: list[str], cwd: Path, env: dict[str, str]):
    kernel = c.WinDLL("kernel32", use_last_error=True)
    initialize = kernel.InitializeProcThreadAttributeList
    initialize.argtypes = [c.c_void_p, w.DWORD, w.DWORD, c.POINTER(c.c_size_t)]
    update = kernel.UpdateProcThreadAttribute
    update.argtypes = [c.c_void_p, w.DWORD, c.c_size_t, c.c_void_p, c.c_size_t, c.c_void_p, c.c_void_p]
    kernel.DeleteProcThreadAttributeList.argtypes = [c.c_void_p]
    kernel.CloseHandle.argtypes = [w.HANDLE]
    create = kernel.CreateProcessW
    create.argtypes = [w.LPCWSTR, w.LPWSTR, c.c_void_p, c.c_void_p, w.BOOL, w.DWORD,
                       c.c_void_p, w.LPCWSTR, c.POINTER(StartupEx), c.POINTER(ProcessInfo)]
    size = c.c_size_t()
    initialize(None, 2, 0, c.byref(size))
    buffer = c.create_string_buffer(size.value)
    if not initialize(buffer, 2, 0, c.byref(size)):
        raise c.WinError(c.get_last_error())
    try:
        job_list = (w.HANDLE * 1)(int(job))
        inherited = (w.HANDLE * 2)(*map(int, handles))
        for key, value in ((0x2000D, job_list), (0x20002, inherited)):
            if not update(buffer, 0, key, value, c.sizeof(value), None, None):
                raise c.WinError(c.get_last_error())
        startup = StartupEx()
        startup.startup.cb = c.sizeof(StartupEx)
        startup.startup.dwFlags = 0x100
        startup.startup.hStdInput, startup.startup.hStdOutput = map(int, handles)
        startup.startup.hStdError = startup.startup.hStdOutput
        startup.attributes = c.cast(buffer, c.c_void_p)
        commandline = c.create_unicode_buffer(subprocess.list2cmdline(command))
        environment = c.create_unicode_buffer("\0".join(f"{key}={env[key]}" for key in sorted(env, key=str.upper)) + "\0\0")
        info = ProcessInfo()
        flags = 0x00080000 | 0x00000400 | 0x08000000  # Extended startup, Unicode environment, no console
        if not create(command[0], commandline, None, None, True, flags, environment,
                      str(cwd), c.byref(startup), c.byref(info)):
            raise c.WinError(c.get_last_error())
        kernel.CloseHandle(info.thread)
        kernel.CloseHandle(info.process)
        return info.pid
    finally:
        kernel.DeleteProcThreadAttributeList(buffer)


class WindowsProcess:
    def __init__(self, command: list[str], cwd: Path, env: dict[str, str], on_line=None,
                 log_file: Path | None = None):
        import msvcrt

        import win32api
        import win32job
        import win32pipe

        self.job = win32job.CreateJobObject(None, "")
        self.log_handle = None
        try:
            limits = win32job.QueryInformationJobObject(self.job, win32job.JobObjectExtendedLimitInformation)
            limits["BasicLimitInformation"]["LimitFlags"] = win32job.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            win32job.SetInformationJobObject(self.job, win32job.JobObjectExtendedLimitInformation, limits)
            if log_file is not None:
                # File-backed stdout: survives even a hard child exit, unlike a pipe.
                self.log_handle = open(log_file, "wb", buffering=0)
                stdout = msvcrt.get_osfhandle(self.log_handle.fileno())
                os.set_handle_inheritable(stdout, True)
                with open(os.devnull, "rb") as null:
                    stdin = msvcrt.get_osfhandle(null.fileno())
                    os.set_handle_inheritable(stdin, True)
                    self.pid = launch(self.job, (stdin, stdout), command, cwd, env)
                self.reader = None
            else:
                reader, writer = win32pipe.CreatePipe(None, 0)
                try:
                    with open(os.devnull, "rb") as null:
                        stdin = msvcrt.get_osfhandle(null.fileno())
                        os.set_handle_inheritable(stdin, True)
                        os.set_handle_inheritable(int(writer), True)
                        self.pid = launch(self.job, (stdin, writer), command, cwd, env)
                except BaseException:
                    reader.Close()
                    raise
                finally:
                    writer.Close()
                self.reader = threading.Thread(target=self.drain, args=(reader, on_line or logger.info),
                                               name="LocalGen engine logs", daemon=True)
            self.process = win32api.OpenProcess(0x00101000, False, self.pid)
            self.created_at = psutil.Process(self.pid).create_time()
            if self.reader is not None:
                self.reader.start()
        except BaseException:
            self.job.Close()
            if getattr(self, "log_handle", None):
                self.log_handle.close()
            if getattr(self, "process", None):
                self.process.Close()
            raise

    @staticmethod
    def drain(handle, on_line):
        import pywintypes
        import win32file

        try:
            pending = b""
            while True:
                _, chunk = win32file.ReadFile(handle, 4096)
                if not chunk:
                    break
                lines = (pending + chunk).replace(b"\r", b"\n").split(b"\n")
                pending = lines.pop()
                if len(pending) > 4096:
                    lines.append(pending[:4096])
                    pending = b""
                for line in lines:
                    if line:
                        on_line(line[:4096].decode("utf-8", errors="replace"))
        except pywintypes.error as exc:
            if exc.winerror not in (109, 232):
                logger.warning("ENGINE_LOG_PIPE error=%s", exc.winerror)
        finally:
            if pending:
                on_line(pending.decode("utf-8", errors="replace"))
            handle.Close()

    def alive(self):
        import win32event

        return win32event.WaitForSingleObject(self.process, 0) == win32event.WAIT_TIMEOUT

    def exit_code(self):
        import win32process

        return win32process.GetExitCodeProcess(self.process)

    def pids(self):
        import win32job

        return set(win32job.QueryInformationJobObject(self.job, win32job.JobObjectBasicProcessIdList))

    def terminate(self):
        import win32job

        win32job.TerminateJobObject(self.job, 1)

    def close(self):
        self.job.Close()
        self.process.Close()
        if self.reader is not None:
            self.reader.join(timeout=1)
        if getattr(self, "log_handle", None):
            self.log_handle.close()


def listeners(port: int):
    # Ask Windows for the listener-only table. Some Windows configurations
    # report state zero for listeners even in the owner PID table.
    get_table = c.WinDLL("iphlpapi").GetExtendedTcpTable
    get_table.argtypes = [c.c_void_p, c.POINTER(w.DWORD), w.BOOL, w.ULONG, w.DWORD, w.ULONG]
    get_table.restype = w.DWORD
    result = []
    for family, record in ((socket.AF_INET, Tcp4Owner), (socket.AF_INET6, Tcp6Owner)):
        size = w.DWORD(0)
        buffer = None
        for _ in range(4):
            code = get_table(buffer, c.byref(size), False, family, 3, 0)  # TCP_TABLE_OWNER_PID_LISTENER
            if code != 122:  # ERROR_INSUFFICIENT_BUFFER
                break
            buffer = c.create_string_buffer(size.value)
        if code != 0:
            raise c.WinError(code)
        if buffer is None:
            continue
        count = w.DWORD.from_buffer(buffer).value
        rows = (record * count).from_buffer(buffer, c.sizeof(w.DWORD))
        for row in rows:
            if socket.ntohs(row.port & 0xFFFF) == port:
                result.append((socket.inet_ntop(family, bytes(row.address)), row.pid))
    return result
