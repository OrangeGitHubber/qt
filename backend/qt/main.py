import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from qt import __version__
from qt.api import market, setup, status
from qt.db import init_db


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    yield


app = FastAPI(title="QT Auto-Trader", version=__version__, lifespan=lifespan)
app.include_router(setup.router)
app.include_router(status.router)
app.include_router(market.router)


def _static_dir() -> Path | None:
    raw = os.environ.get("QT_STATIC_DIR")
    candidates = [Path(raw)] if raw else []
    candidates.append(Path(__file__).resolve().parent.parent.parent / "frontend" / "dist")
    for candidate in candidates:
        if candidate.is_dir():
            return candidate
    return None


static = _static_dir()
if static:
    app.mount("/assets", StaticFiles(directory=static / "assets"), name="assets")

    @app.get("/{full_path:path}", include_in_schema=False)
    def spa(full_path: str) -> FileResponse:
        candidate = static / full_path
        if full_path and candidate.is_file():
            return FileResponse(candidate)
        return FileResponse(static / "index.html")
