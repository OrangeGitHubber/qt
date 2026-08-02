"""The built frontend must not be served from a stale browser cache.

This exists because of a real, silent failure: index.html is the ONE unhashed
file in the build and it names the hashed bundles, so a browser that reuses an
old copy loads the old bundle too — and an updated container then runs the
PREVIOUS UI against the new API, with no error to notice. Served without an
explicit Cache-Control, index.html gets heuristic caching off its Last-Modified
(the docker build time), which is exactly the situation that bit us.
"""

from fastapi import FastAPI
from fastapi.testclient import TestClient

from qt.main import mount_spa


def _build(tmp_path):
    """A minimal stand-in for `npm run build` output."""
    (tmp_path / "assets").mkdir()
    (tmp_path / "assets" / "index-abc123.js").write_text("console.log(1)")
    (tmp_path / "index.html").write_text(
        '<!doctype html><script src="/assets/index-abc123.js"></script>'
    )
    (tmp_path / "favicon.ico").write_text("x")
    app = FastAPI()
    mount_spa(app, tmp_path)
    return TestClient(app)


def test_index_is_never_cached(tmp_path):
    r = _build(tmp_path).get("/")
    assert r.status_code == 200
    assert "no-store" in r.headers["cache-control"]


def test_spa_deep_link_is_never_cached(tmp_path):
    """A deep link falls back to the shell — same file, same rule."""
    r = _build(tmp_path).get("/optimizer")
    assert r.status_code == 200
    assert "no-store" in r.headers["cache-control"]


def test_unhashed_root_file_is_never_cached(tmp_path):
    r = _build(tmp_path).get("/favicon.ico")
    assert r.status_code == 200
    assert "no-store" in r.headers["cache-control"]


def test_hashed_assets_stay_cacheable(tmp_path):
    """The point of the hash is that the URL changes when the content does, so
    these must NOT be forced no-store — that would re-download the whole bundle
    on every page load."""
    r = _build(tmp_path).get("/assets/index-abc123.js")
    assert r.status_code == 200
    assert "no-store" not in r.headers.get("cache-control", "")
