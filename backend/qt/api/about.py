"""About page API: app identity / build id, plus the living docs (changelog
and roadmap) served straight from the maintained markdown files.

Rendering approach (see docs/decisions.md): the BACKEND serves the docs. The
image copies `docs/` to `/app/docs` and this module reads the markdown at
request time, so updating `docs/CHANGELOG.md` / `docs/roadmap.md` (which we do
on every change) updates the About page with no frontend rebuild. The frontend
fetches the raw markdown and renders it. This keeps the page from ever drifting
out of date behind a duplicated/hardcoded copy.
"""

import os
from pathlib import Path

from fastapi import APIRouter

from qt.services import buildinfo

router = APIRouter(prefix="/api/about", tags=["about"])


def _docs_dir() -> Path:
    """Resolve the in-repo docs folder across every runtime:
    - production container: /app/docs (set via QT_DOCS_DIR + `COPY docs`);
    - editable/dev install: the repo's docs/ four parents up from this file;
    - non-editable install run from the repo root (CI's `pytest`, local dev):
      docs/ under the current working directory — needed because a plain
      `pip install ./backend` puts qt in site-packages, where the parents[3]
      path resolves outside the repo.
    """
    raw = os.environ.get("QT_DOCS_DIR")
    candidates = [Path(raw)] if raw else []
    candidates.append(Path(__file__).resolve().parents[3] / "docs")
    candidates.append(Path.cwd() / "docs")
    for candidate in candidates:
        if candidate.is_dir():
            return candidate
    return candidates[0]


def _read_doc(filename: str) -> str:
    path = _docs_dir() / filename
    if not path.is_file():
        # Degrade gracefully — the About page must never hard-break just because
        # a particular build didn't bundle the docs.
        title = filename.removesuffix(".md").replace("_", " ").title()
        return f"# {title}\n\nThis document isn't available in this build."
    return path.read_text(encoding="utf-8")


@router.get("")
def about() -> dict:
    return buildinfo.about()


@router.get("/changelog")
def changelog() -> dict:
    return {"markdown": _read_doc("CHANGELOG.md")}


@router.get("/roadmap")
def roadmap() -> dict:
    return {"markdown": _read_doc("roadmap.md")}
