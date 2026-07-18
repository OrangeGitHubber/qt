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

from fastapi import APIRouter, HTTPException

from qt.services import buildinfo

router = APIRouter(prefix="/api/about", tags=["about"])


def _docs_dir() -> Path:
    """Resolve the in-repo docs folder. In the container it's /app/docs (set
    via QT_DOCS_DIR + `COPY docs`); in dev it's the repo's docs/ next to the
    backend source."""
    raw = os.environ.get("QT_DOCS_DIR")
    candidates = [Path(raw)] if raw else []
    # backend/qt/api/about.py -> repo root is four parents up.
    candidates.append(Path(__file__).resolve().parents[3] / "docs")
    for candidate in candidates:
        if candidate.is_dir():
            return candidate
    # Return the first candidate anyway so the error message is informative.
    return candidates[0]


def _read_doc(filename: str) -> str:
    path = _docs_dir() / filename
    if not path.is_file():
        raise HTTPException(status_code=404, detail=f"{filename} not found in docs")
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
