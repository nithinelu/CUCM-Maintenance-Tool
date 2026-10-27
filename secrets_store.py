"""Keep saved passwords encrypted for the current Windows user (DPAPI)."""

import base64
import ctypes
import ctypes.wintypes as wt


class _Blob(ctypes.Structure):
    _fields_ = [("cbData", wt.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]


def _call(fn, data):
    buf = ctypes.create_string_buffer(data, len(data))
    src = _Blob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    dst = _Blob()
    if not fn(ctypes.byref(src), None, None, None, None, 0, ctypes.byref(dst)):
        raise OSError("DPAPI call failed")
    try:
        return ctypes.string_at(dst.pbData, dst.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(dst.pbData)


def protect(text):
    if not text:
        return ""
    raw = _call(ctypes.windll.crypt32.CryptProtectData, text.encode("utf-8"))
    return "dpapi:" + base64.b64encode(raw).decode("ascii")


def unprotect(value):
    if not value or not value.startswith("dpapi:"):
        return value or ""
    try:
        raw = base64.b64decode(value[6:])
        fn = ctypes.windll.crypt32.CryptUnprotectData
        return _call(fn, raw).decode("utf-8")
    except Exception:
        return ""
