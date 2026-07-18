"""Which build am I running? — resolves the app's identity and the exact
build a container was made from.

The build id (git short-SHA + build date) is threaded through at image build
time: the Dockerfile takes `GIT_SHA` / `BUILD_DATE` build ARGs (set by CI to
the commit SHA and build timestamp) and exposes them as the `QT_GIT_SHA` /
`QT_BUILD_DATE` env vars, which this module reads. For local/dev where those
env vars aren't set, we fall back to asking git directly, and finally to "dev"
so the About page always shows *something* sensible.
"""

import os
import subprocess
from functools import lru_cache
from pathlib import Path

from qt import __version__

REPO_URL = "https://github.com/OrangeGitHubber/qt"
LICENSE = "GPLv3"
APP_NAME = "QT Auto-Trader"


def _repo_root() -> Path:
    # backend/qt/services/buildinfo.py -> repo root is four parents up.
    return Path(__file__).resolve().parents[3]


@lru_cache(maxsize=1)
def git_sha() -> str:
    """Short commit SHA of the build. Env (set at image build) wins; else ask
    git (dev checkout); else 'dev'."""
    env = os.environ.get("QT_GIT_SHA", "").strip()
    if env:
        # CI passes the full 40-char SHA; keep it short and tidy for the UI.
        return env[:12]
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=_repo_root(),
            capture_output=True,
            text=True,
            timeout=3,
        )
        if out.returncode == 0 and out.stdout.strip():
            return out.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        pass
    return "dev"


@lru_cache(maxsize=1)
def build_date() -> str:
    """ISO build date (set at image build), else empty string."""
    return os.environ.get("QT_BUILD_DATE", "").strip()


def about() -> dict:
    return {
        "name": APP_NAME,
        "version": __version__,
        "git_sha": git_sha(),
        "build_date": build_date(),
        "license": LICENSE,
        "repo_url": REPO_URL,
    }
