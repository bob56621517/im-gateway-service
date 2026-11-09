"""Bootstrap paths and control-plane token (HANDOVER §13.5).

Only three things must be known before the database is opened:

1. the database path,
2. the control-plane listen address,
3. the control-plane token file.

Everything else lives in the database.
"""

from __future__ import annotations

import os
import secrets
import stat
import sys
from pathlib import Path

__all__ = [
    "DEFAULT_TCP_LISTEN",
    "data_dir",
    "default_db_path",
    "default_listen",
    "default_media_dir",
    "default_token_file",
    "ensure_private_dir",
    "load_or_create_token",
]

APP_NAME = "im-gateway"
DEFAULT_TCP_LISTEN = "127.0.0.1:8765"


def _env_path(name: str) -> Path | None:
    raw = os.environ.get(name, "").strip()
    return Path(raw).expanduser() if raw else None


def data_dir() -> Path:
    """Platform data directory (overridable with ``IMGW_DATA_DIR``)."""
    override = _env_path("IMGW_DATA_DIR")
    if override is not None:
        return override
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(base) / APP_NAME
    base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / APP_NAME


def default_db_path() -> Path:
    override = _env_path("IMGW_DB")
    return override if override is not None else data_dir() / "runtime.db"


def default_media_dir() -> Path:
    return data_dir() / "media"


def default_token_file() -> Path:
    override = _env_path("IMGW_TOKEN_FILE")
    if override is not None:
        return override
    if sys.platform == "win32":
        base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
        return Path(base) / APP_NAME / "token"
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / APP_NAME / "token"


def default_listen() -> str:
    """Default control-plane listen address.

    POSIX prefers a Unix domain socket (no port, no local firewall exposure);
    Windows uses loopback TCP.
    """
    override = os.environ.get("IMGW_LISTEN", "").strip()
    if override:
        return override
    if sys.platform == "win32":
        return f"tcp://{DEFAULT_TCP_LISTEN}"
    runtime = os.environ.get("XDG_RUNTIME_DIR") or str(data_dir())
    return f"unix://{Path(runtime) / 'imgw.sock'}"


def ensure_private_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    if sys.platform != "win32":
        path.chmod(stat.S_IRWXU)
    return path


def load_or_create_token(path: Path | None = None) -> str:
    """Read the control-plane token, creating a 0600 file on first use."""
    token_file = path or default_token_file()
    ensure_private_dir(token_file.parent)
    if token_file.exists():
        token = token_file.read_text(encoding="utf-8").strip()
        if token:
            return token
    token = secrets.token_urlsafe(32)
    token_file.write_text(token + "\n", encoding="utf-8")
    if sys.platform != "win32":
        token_file.chmod(stat.S_IRUSR | stat.S_IWUSR)
    return token
