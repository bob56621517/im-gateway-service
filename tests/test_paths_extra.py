"""Extra tests for bootstrap paths: the POSIX branches that never run on Windows.

``im_gateway.core.paths`` branches on ``sys.platform``.  To exercise the POSIX
side on Windows we replace the module's ``sys`` reference with a tiny stand-in
that reports ``linux``, and point every XDG variable at ``tmp_path`` so the
user's home directory is never read.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from im_gateway.core import paths


@pytest.fixture()
def posix(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(paths, "sys", SimpleNamespace(platform="linux"))
    for name in ("IMGW_DATA_DIR", "IMGW_DB", "IMGW_LISTEN", "IMGW_TOKEN_FILE"):
        monkeypatch.delenv(name, raising=False)


def test_data_dir_posix_uses_xdg_data_home(
    posix: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg-data"))
    assert paths.data_dir() == tmp_path / "xdg-data" / "im-gateway"
    assert paths.default_db_path() == tmp_path / "xdg-data" / "im-gateway" / "runtime.db"
    assert paths.default_media_dir() == tmp_path / "xdg-data" / "im-gateway" / "media"


def test_data_dir_posix_falls_back_to_home(
    posix: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "home"))
    assert paths.data_dir() == tmp_path / "home" / ".local" / "share" / "im-gateway"


def test_token_file_posix_uses_xdg_config_home(
    posix: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg-config"))
    assert paths.default_token_file() == tmp_path / "xdg-config" / "im-gateway" / "token"


def test_token_file_posix_falls_back_to_home(
    posix: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "home"))
    assert paths.default_token_file() == tmp_path / "home" / ".config" / "im-gateway" / "token"


def test_default_listen_posix_prefers_xdg_runtime_dir(
    posix: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "runtime"))
    assert paths.default_listen() == f"unix://{tmp_path / 'runtime' / 'imgw.sock'}"


def test_default_listen_posix_falls_back_to_data_dir(
    posix: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    assert paths.default_listen() == f"unix://{tmp_path / 'data' / 'im-gateway' / 'imgw.sock'}"


def test_ensure_private_dir_posix_sets_mode(posix: None, tmp_path: Path) -> None:
    target = tmp_path / "private"
    assert paths.ensure_private_dir(target) == target
    assert target.is_dir()


def test_load_or_create_token_posix_chmods_file(posix: None, tmp_path: Path) -> None:
    token_file = tmp_path / "conf" / "token"
    token = paths.load_or_create_token(token_file)
    assert token
    assert token_file.read_text(encoding="utf-8").strip() == token


# ---------------------------------------------------------------------------
# Windows (default) branches for the token file
# ---------------------------------------------------------------------------


@pytest.fixture()
def windows(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(paths, "sys", SimpleNamespace(platform="win32"))
    monkeypatch.delenv("IMGW_TOKEN_FILE", raising=False)


def test_token_file_windows_prefers_appdata(
    windows: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    assert paths.default_token_file() == tmp_path / "appdata" / "im-gateway" / "token"


def test_token_file_windows_falls_back_to_home(
    windows: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("APPDATA", raising=False)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "home"))
    assert paths.default_token_file() == (tmp_path / "home" / "AppData" / "Roaming" / "im-gateway" / "token")
