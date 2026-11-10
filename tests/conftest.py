"""Shared test fixtures."""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest

# The CLI resolves its language once, at import time (Typer builds help strings
# then).  Force English *before* any test module imports
# ``im_gateway.cli.main`` so the existing suite is deterministic on Chinese
# Windows too; ``tests/test_i18n.py`` reloads the modules for Chinese tests.
os.environ["IMGW_LANG"] = "en"


@pytest.fixture()
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "runtime.db"


@pytest.fixture(autouse=True)
def _deterministic_cli_language() -> Iterator[None]:
    """Keep every test on the English catalog unless it opts into another one."""
    from im_gateway.cli import i18n

    i18n.set_language("en")
    yield
    i18n.set_language("en")
