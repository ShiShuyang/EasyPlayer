from contextlib import contextmanager
import ctypes
from ctypes import wintypes
from pathlib import Path
import re
import sys


WM_COPYDATA = 0x004A
PLAYLIST_MESSAGE = 0x45505101
OPEN_MESSAGE = 0x45505102
WINDOW_PROPERTY = "EasyPlayer.Playlist.1"
MAX_BATCH_BYTES = 4 * 1024 * 1024
IS_WINDOWS = sys.platform == "win32"


class CopyData(ctypes.Structure):
    _fields_ = [
        ("kind", ctypes.c_size_t),
        ("size", wintypes.DWORD),
        ("data", ctypes.c_void_p),
    ]


if IS_WINDOWS:
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    user32.SetPropW.argtypes = (wintypes.HWND, wintypes.LPCWSTR, wintypes.HANDLE)
    user32.SetPropW.restype = wintypes.BOOL
    user32.GetPropW.argtypes = (wintypes.HWND, wintypes.LPCWSTR)
    user32.GetPropW.restype = wintypes.HANDLE
    user32.GetTopWindow.argtypes = (wintypes.HWND,)
    user32.GetTopWindow.restype = wintypes.HWND
    user32.GetWindow.argtypes = (wintypes.HWND, wintypes.UINT)
    user32.GetWindow.restype = wintypes.HWND
    user32.IsWindowVisible.argtypes = (wintypes.HWND,)
    user32.IsWindowVisible.restype = wintypes.BOOL
    user32.GetWindowThreadProcessId.argtypes = (wintypes.HWND, ctypes.POINTER(wintypes.DWORD))
    user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    user32.AllowSetForegroundWindow.argtypes = (wintypes.DWORD,)
    user32.AllowSetForegroundWindow.restype = wintypes.BOOL
    user32.SendMessageTimeoutW.argtypes = (
        wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM,
        wintypes.UINT, wintypes.UINT, ctypes.POINTER(ctypes.c_size_t),
    )
    user32.SendMessageTimeoutW.restype = wintypes.LPARAM
    kernel32.CreateMutexW.argtypes = (ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR)
    kernel32.CreateMutexW.restype = wintypes.HANDLE
    kernel32.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
    kernel32.WaitForSingleObject.restype = wintypes.DWORD
    kernel32.ReleaseMutex.argtypes = (wintypes.HANDLE,)
    kernel32.ReleaseMutex.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel32.CloseHandle.restype = wintypes.BOOL
    kernel32.GetTempPathW.argtypes = (wintypes.DWORD, wintypes.LPWSTR)
    kernel32.GetTempPathW.restype = wintypes.DWORD


def decode_paths(data: bytes) -> list[str]:
    return [path for path in data.decode("utf-8").split("\n") if path and "\0" not in path]


def read_batch(arguments: list[str]) -> list[str] | None:
    if not arguments or arguments[0] != "--enqueue-from":
        return None
    if len(arguments) != 2 or not IS_WINDOWS:
        return []
    manifest = Path(arguments[1])
    temporary_path = ctypes.create_unicode_buffer(32768)
    length = kernel32.GetTempPathW(len(temporary_path), temporary_path)
    if not 0 < length < len(temporary_path) or manifest.parent != Path(temporary_path.value):
        return []
    if not re.fullmatch(
        r"EasyPlayerQueue-[0-9a-f]{32}\.txt", manifest.name
    ):
        return []
    try:
        with manifest.open("rb") as source:
            data = source.read(MAX_BATCH_BYTES + 1)
        if len(data) <= MAX_BATCH_BYTES:
            return decode_paths(data)
    except (OSError, UnicodeError):
        pass
    finally:
        try:
            manifest.unlink(missing_ok=True)
        except OSError:
            pass
    return []


@contextmanager
def file_startup():
    mutex = kernel32.CreateMutexW(None, False, r"Local\EasyPlayer.Playlist.Startup.1")
    acquired = False
    try:
        # Both a normal acquisition and an abandoned mutex give this process ownership.
        acquired = bool(mutex) and kernel32.WaitForSingleObject(mutex, 15_000) in (0, 0x80)
        yield
    finally:
        if acquired:
            kernel32.ReleaseMutex(mutex)
        if mutex:
            kernel32.CloseHandle(mutex)


def register_window(handle: int):
    user32.SetPropW(handle, WINDOW_PROPERTY, 1)


def forward_files(paths: list[str], *, replace: bool) -> bool:
    data = "\n".join(paths).encode("utf-8")
    if not 0 < len(data) <= MAX_BATCH_BYTES:
        return False
    buffer = ctypes.create_string_buffer(data)
    kind = OPEN_MESSAGE if replace else PLAYLIST_MESSAGE
    payload = CopyData(kind, len(data), ctypes.addressof(buffer))
    # Top-level windows are listed from front to back in their current Z order.
    handle = user32.GetTopWindow(None)
    while handle:
        next_handle = user32.GetWindow(handle, 2)  # GW_HWNDNEXT
        if user32.GetPropW(handle, WINDOW_PROPERTY) and user32.IsWindowVisible(handle):
            if replace:
                process_id = wintypes.DWORD()
                if user32.GetWindowThreadProcessId(handle, ctypes.byref(process_id)):
                    user32.AllowSetForegroundWindow(process_id)
            result = ctypes.c_size_t()
            if user32.SendMessageTimeoutW(
                handle, WM_COPYDATA, 0, ctypes.addressof(payload), 0x0003, 2000,
                ctypes.byref(result),
            ) and result.value == 1:
                return True
        handle = next_handle
    return False


def receive_files(message: int) -> tuple[int, list[str]] | None:
    native = wintypes.MSG.from_address(message)
    if native.message != WM_COPYDATA or not native.lParam:
        return None
    payload = CopyData.from_address(native.lParam)
    if payload.kind not in (PLAYLIST_MESSAGE, OPEN_MESSAGE) or not 0 < payload.size <= MAX_BATCH_BYTES:
        return None
    if not payload.data:
        return None
    try:
        return payload.kind, decode_paths(ctypes.string_at(payload.data, payload.size))
    except UnicodeError:
        return None
