"""Persistencia local de preferencias y claves de API.

En Windows las claves se cifran con DPAPI (`CryptProtectData`), de modo que
solo el usuario actual puede descifrarlas. Fuera de Windows se usa un
almacenamiento base64 explícitamente marcado como no cifrado; la aplicación
distribuida es Windows-only.
"""

from __future__ import annotations

import base64
import ctypes
import json
import logging
import os
import sys
from ctypes import wintypes
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from examforge.errors import ConfigurationError
from examforge.models import AppSettings, ProviderName

logger = logging.getLogger(__name__)

APP_DIR_NAME = "ExamForge"
_SETTINGS_FILE = "settings.json"
_SECRETS_FILE = "secrets.json"
_DPAPI_PREFIX = "dpapi:"
_PLAIN_PREFIX = "plain:"
_ENTROPY = b"ExamForge::local::v1"


class _DataBlob(ctypes.Structure):
    _fields_ = [
        ("cbData", wintypes.DWORD),
        ("pbData", ctypes.POINTER(ctypes.c_char)),
    ]


def _dpapi_available() -> bool:
    return sys.platform == "win32"


def _protect_windows(data: bytes) -> bytes:
    crypt32 = ctypes.windll.crypt32  # type: ignore[attr-defined]
    kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
    crypt32.CryptProtectData.argtypes = [
        ctypes.POINTER(_DataBlob),
        wintypes.LPCWSTR,
        ctypes.POINTER(_DataBlob),
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(_DataBlob),
    ]
    crypt32.CryptProtectData.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p

    blob_in = _DataBlob(len(data), ctypes.cast(ctypes.c_char_p(data), ctypes.POINTER(ctypes.c_char)))
    entropy = _DataBlob(
        len(_ENTROPY), ctypes.cast(ctypes.c_char_p(_ENTROPY), ctypes.POINTER(ctypes.c_char))
    )
    blob_out = _DataBlob()
    if not crypt32.CryptProtectData(
        ctypes.byref(blob_in),
        "ExamForge API key",
        ctypes.byref(entropy),
        None,
        None,
        0x1,  # CRYPTPROTECT_UI_FORBIDDEN
        ctypes.byref(blob_out),
    ):
        raise OSError(ctypes.get_last_error(), "CryptProtectData falló")
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        kernel32.LocalFree(blob_out.pbData)


def _unprotect_windows(data: bytes) -> bytes:
    crypt32 = ctypes.windll.crypt32  # type: ignore[attr-defined]
    kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
    crypt32.CryptUnprotectData.argtypes = [
        ctypes.POINTER(_DataBlob),
        ctypes.POINTER(wintypes.LPWSTR),
        ctypes.POINTER(_DataBlob),
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(_DataBlob),
    ]
    crypt32.CryptUnprotectData.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p

    blob_in = _DataBlob(len(data), ctypes.cast(ctypes.c_char_p(data), ctypes.POINTER(ctypes.c_char)))
    entropy = _DataBlob(
        len(_ENTROPY), ctypes.cast(ctypes.c_char_p(_ENTROPY), ctypes.POINTER(ctypes.c_char))
    )
    blob_out = _DataBlob()
    description = wintypes.LPWSTR()
    if not crypt32.CryptUnprotectData(
        ctypes.byref(blob_in),
        ctypes.byref(description),
        ctypes.byref(entropy),
        None,
        None,
        0x1,
        ctypes.byref(blob_out),
    ):
        raise OSError(ctypes.get_last_error(), "CryptUnprotectData falló")
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        kernel32.LocalFree(blob_out.pbData)


def _protect(secret: str) -> str:
    raw = secret.encode("utf-8")
    if _dpapi_available():
        return _DPAPI_PREFIX + base64.b64encode(_protect_windows(raw)).decode("ascii")
    logger.warning("DPAPI no disponible; la clave se guardará ofuscada, no cifrada.")
    return _PLAIN_PREFIX + base64.b64encode(raw).decode("ascii")


def _unprotect(blob: str) -> str:
    if blob.startswith(_DPAPI_PREFIX):
        encrypted = base64.b64decode(blob[len(_DPAPI_PREFIX) :])
        return _unprotect_windows(encrypted).decode("utf-8")
    if blob.startswith(_PLAIN_PREFIX):
        return base64.b64decode(blob[len(_PLAIN_PREFIX) :]).decode("utf-8")
    raise ConfigurationError("Formato de credencial desconocido.")


def data_dir() -> Path:
    override = os.environ.get("EXAMFORGE_DATA_DIR")
    if override:
        path = Path(override).expanduser()
    else:
        base = (
            Path(os.environ.get("APPDATA", Path.home()))
            if sys.platform == "win32"
            else Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
        )
        path = base / APP_DIR_NAME
    path.mkdir(parents=True, exist_ok=True)
    return path


def logs_dir() -> Path:
    path = data_dir() / "logs"
    path.mkdir(parents=True, exist_ok=True)
    return path


class ConfigStore:
    """Lectura y escritura atómica de ajustes y secretos."""

    def __init__(self, base_dir: Path | None = None) -> None:
        self.base = base_dir or data_dir()
        self.base.mkdir(parents=True, exist_ok=True)
        self.settings_path = self.base / _SETTINGS_FILE
        self.secrets_path = self.base / _SECRETS_FILE

    def load_settings(self) -> AppSettings:
        if not self.settings_path.exists():
            return AppSettings()
        try:
            payload = json.loads(self.settings_path.read_text(encoding="utf-8"))
            return AppSettings.model_validate(payload)
        except (OSError, json.JSONDecodeError, ValidationError) as exc:
            logger.error("No se pudieron leer los ajustes: %s", exc)
            return AppSettings()

    def save_settings(self, settings: AppSettings) -> None:
        self._write_json(self.settings_path, settings.model_dump(mode="json"))

    def load_secret(self, provider: ProviderName) -> str:
        env_name = {
            ProviderName.GROQ: "GROQ_API_KEY",
            ProviderName.GEMINI: "GEMINI_API_KEY",
        }.get(provider)
        if env_name and os.environ.get(env_name):
            return os.environ[env_name].strip()
        if not self.secrets_path.exists():
            return ""
        try:
            payload: dict[str, str] = json.loads(self.secrets_path.read_text(encoding="utf-8"))
            blob = payload.get(provider.value, "")
            return _unprotect(blob) if blob else ""
        except (OSError, json.JSONDecodeError, ConfigurationError, ValueError) as exc:
            logger.error("No se pudo descifrar la clave de %s: %s", provider.value, exc)
            return ""

    def has_secret(self, provider: ProviderName) -> bool:
        return bool(self.load_secret(provider))

    def save_secret(self, provider: ProviderName, secret: str) -> None:
        payload: dict[str, str] = {}
        if self.secrets_path.exists():
            try:
                payload = json.loads(self.secrets_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                payload = {}
        if secret.strip():
            payload[provider.value] = _protect(secret.strip())
        else:
            payload.pop(provider.value, None)
        self._write_json(self.secrets_path, payload)
        try:
            os.chmod(self.secrets_path, 0o600)
        except OSError:
            pass

    @staticmethod
    def _write_json(path: Path, payload: dict[str, Any]) -> None:
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        os.replace(temporary, path)


def public_settings(store: ConfigStore, settings: AppSettings, version: str) -> dict[str, Any]:
    return {
        **settings.model_dump(mode="json"),
        "version": version,
        "data_dir": str(store.base),
        "groq_key_set": store.has_secret(ProviderName.GROQ),
        "gemini_key_set": store.has_secret(ProviderName.GEMINI),
    }
