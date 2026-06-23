"""Package-level contract: public API surface, version, lazy server import."""
from __future__ import annotations

import subprocess
import sys

import pytest

import muxdesk


def test_version():
    assert muxdesk.__version__ == "0.1.0"


def test_all_exports_are_importable():
    for name in muxdesk.__all__:
        assert hasattr(muxdesk, name), f"missing export: {name}"


def test_core_import_does_not_pull_in_fastapi():
    # The core building blocks must stay zero-dependency. Run in a clean
    # subprocess so another test importing FastAPI can't mask a regression.
    code = "import muxdesk, sys; assert 'fastapi' not in sys.modules; print('ok')"
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


def test_create_app_builds_when_server_extra_present():
    pytest.importorskip("fastapi")
    from muxdesk import create_app

    app = create_app()
    paths = [r.path for r in app.routes if hasattr(r, "path")]
    assert any(p.startswith("/api/muxdesk") for p in paths)
