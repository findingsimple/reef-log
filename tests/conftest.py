"""Shared pytest fixtures.

The db fixture is file-backed (not :memory:) so WAL semantics, FK enforcement,
and migration behavior match production exactly. Tests that mock SQLite would
defeat the point of choosing stdlib sqlite in the first place.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from reef_log import db as db_module


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "reef.db"


@pytest.fixture
def conn(db_path: Path) -> Iterator:
    connection = db_module.connect(db_path)
    try:
        yield connection
    finally:
        connection.close()
