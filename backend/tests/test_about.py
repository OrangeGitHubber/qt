"""About endpoint: build identity + living docs (changelog/roadmap) served
straight from the maintained markdown files."""

from qt import __version__


def test_about_identity(client):
    body = client.get("/api/about").json()
    assert body["name"] == "QT Auto-Trader"
    assert body["version"] == __version__
    assert body["license"] == "GPLv3"
    assert body["repo_url"].startswith("https://github.com/")
    # git_sha is always something ("dev", a short SHA, or the CI-set env value).
    assert body["git_sha"]


def test_about_serves_changelog(client):
    body = client.get("/api/about/changelog").json()
    md = body["markdown"]
    # Sourced from docs/CHANGELOG.md, not hardcoded — check for stable content.
    assert "plain-English changelog" in md
    assert "Phase 0" in md


def test_about_serves_roadmap(client):
    body = client.get("/api/about/roadmap").json()
    md = body["markdown"]
    # Sourced from docs/roadmap.md — check phases render.
    assert "# Roadmap" in md
    assert "Phase 4" in md
    assert "optimizer" in md.lower()


def test_about_build_date_from_env(client, monkeypatch):
    from qt.services import buildinfo

    buildinfo.git_sha.cache_clear()
    buildinfo.build_date.cache_clear()
    monkeypatch.setenv("QT_GIT_SHA", "abcdef1234567890")
    monkeypatch.setenv("QT_BUILD_DATE", "2026-07-18")
    body = client.get("/api/about").json()
    assert body["git_sha"] == "abcdef123456"  # trimmed to 12 chars
    assert body["build_date"] == "2026-07-18"
    buildinfo.git_sha.cache_clear()
    buildinfo.build_date.cache_clear()
