"""Read publisher credentials from the environment; Elsevier also has a legacy store."""
from __future__ import annotations

import base64
import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import sys
import tempfile

from .settings import settings_path


class _Blob(ctypes.Structure):
    _fields_ = [("size", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_ubyte))]


def credentials_path() -> Path:
    return settings_path().with_name("elsevier.credentials.json")


def _crypt(data: bytes, *, decrypt: bool) -> bytes:
    if sys.platform != "win32":
        raise ValueError("本地加密保存仅支持 Windows；其他系统请使用环境变量或本次输入。")
    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    buffer = ctypes.create_string_buffer(data)
    incoming = _Blob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    outgoing = _Blob()
    flags = 0x01  # CRYPTPROTECT_UI_FORBIDDEN; binds to current Windows user.
    blob_pointer = ctypes.POINTER(_Blob)
    if decrypt:
        function = crypt32.CryptUnprotectData
        function.argtypes = [blob_pointer, ctypes.c_void_p, ctypes.c_void_p,
                             ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, blob_pointer]
        arguments = (ctypes.byref(incoming), None, None, None, None, flags, ctypes.byref(outgoing))
    else:
        function = crypt32.CryptProtectData
        function.argtypes = [blob_pointer, wintypes.LPCWSTR, ctypes.c_void_p,
                             ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, blob_pointer]
        arguments = (ctypes.byref(incoming), "PaperDownloader Elsevier", None,
                     None, None, flags, ctypes.byref(outgoing))
    function.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p
    if not function(*arguments):
        raise ValueError("无法读取或保存当前 Windows 用户的加密凭据，请重新输入 API Key。")
    try:
        return ctypes.string_at(outgoing.data, outgoing.size)
    finally:
        kernel32.LocalFree(ctypes.cast(outgoing.data, ctypes.c_void_p))


def load_elsevier_credentials() -> dict[str, str | bool]:
    values: dict[str, str | bool] = {"api_key": "", "insttoken": "", "remembered": False}
    try:
        document = json.loads(credentials_path().read_text(encoding="utf-8"))
        if document.get("version") != 1 or document.get("protection") != "windows-dpapi-current-user":
            raise ValueError("Unsupported credential store")
        payload = base64.b64decode(document["ciphertext"], validate=True)
        stored = json.loads(_crypt(payload, decrypt=True).decode("utf-8"))
        for name in ("api_key", "insttoken"):
            if isinstance(stored.get(name), str):
                values[name] = stored[name]
        values["remembered"] = bool(values["api_key"] or values["insttoken"])
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        pass
    for field, environment in (("api_key", "ELSEVIER_API_KEY"), ("insttoken", "ELSEVIER_INSTTOKEN")):
        if value := os.environ.get(environment, "").strip():
            values[field] = value
    # User-selected variable takes precedence over legacy names and saved values.
    if api_key := os.environ.get("ElsevierKey", "").strip():
        values["api_key"] = api_key
    return values


def save_elsevier_credentials(api_key: str, insttoken: str = "") -> None:
    api_key, insttoken = api_key.strip(), insttoken.strip()
    if not api_key and not insttoken:
        clear_elsevier_credentials()
        return
    payload = json.dumps({"api_key": api_key, "insttoken": insttoken}, ensure_ascii=False).encode("utf-8")
    encrypted = _crypt(payload, decrypt=False)
    document = {"version": 1, "protection": "windows-dpapi-current-user",
                "ciphertext": base64.b64encode(encrypted).decode("ascii")}
    destination = credentials_path()
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=destination.parent,
                                         prefix="credentials_", suffix=".tmp", delete=False) as handle:
            temporary = Path(handle.name)
            json.dump(document, handle, indent=2)
            handle.write("\n")
        temporary.replace(destination)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def clear_elsevier_credentials() -> None:
    credentials_path().unlink(missing_ok=True)


def load_wiley_credentials() -> dict[str, str]:
    """Wiley uses only the user-selected environment variable, never a local key file."""
    return {"api_key": os.environ.get("WileyKey", "").strip()}


def load_springer_meta_credentials() -> dict[str, str]:
    """Read the metadata-search key separately from publisher download credentials."""
    return {"api_key": os.environ.get("SpringerMetaKey", "").strip()}
