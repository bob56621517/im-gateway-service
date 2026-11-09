"""Tests for bootstrap paths and the control-plane token (HANDOVER §13.5)."""

from __future__ import annotations

import stat
import sys
from pathlib import Path

import pytest

from im_gateway.core import paths


def test_env_override_for_data_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("IMGW_DATA_DIR", str(tmp_path / "data"))
    assert paths.data_dir() == tmp_path / "data"
    assert paths.default_db_path() == tmp_path / "data" / "runtime.db"
    assert paths.default_media_dir() == tmp_path / "data" / "media"


def test_db_override_wins(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("IMGW_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("IMGW_DB", str(tmp_path / "custom.db"))
    assert paths.default_db_path() == tmp_path / "custom.db"


def test_default_listen_shape(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("IMGW_LISTEN", raising=False)
    listen = paths.default_listen()
    if sys.platform == "win32":
        assert listen.startswith("tcp://127.0.0.1:")
    else:
        assert listen.startswith("unix://")


def test_listen_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("IMGW_LISTEN", "unix:///tmp/x.sock")
    assert paths.default_listen() == "unix:///tmp/x.sock"


def test_token_created_once_and_read_back(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    token_file = tmp_path / "conf" / "token"
    monkeypatch.setenv("IMGW_TOKEN_FILE", str(token_file))
    first = paths.load_or_create_token()
    assert first
    second = paths.load_or_create_token()
    assert second == first
    assert token_file.read_text(encoding="utf-8").strip() == first
    if sys.platform != "win32":
        mode = token_file.stat().st_mode
        assert stat.S_IMODE(mode) == 0o600


def test_existing_token_file_is_reused(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    token_file = tmp_path / "token"
    token_file.write_text("preset-token\n", encoding="utf-8")
    monkeypatch.setenv("IMGW_TOKEN_FILE", str(token_file))
    assert paths.load_or_create_token() == "preset-token"
