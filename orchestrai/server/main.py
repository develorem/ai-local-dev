"""FastAPI app entrypoint."""

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from server.config import config
from server.db import init_db, run_migrations
from server.reaper import start_reaper, stop_reaper
from server.routes import api


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    result = run_migrations()
    print(f"[orchestrai] migrations: applied={result['applied']} version={result['version']}",
          flush=True)
    reaper_task = start_reaper()
    print(f"[orchestrai] reaper started (interval={config.REAPER_INTERVAL_SEC}s)", flush=True)
    try:
        yield
    finally:
        await stop_reaper(reaper_task)


app = FastAPI(
    title="OrchestrAi Hub",
    version=config.VERSION,
    lifespan=lifespan,
)

app.include_router(api)

# Static UI
UI_DIR = Path(__file__).resolve().parent.parent / "ui"
if UI_DIR.exists():
    app.mount("/assets", StaticFiles(directory=UI_DIR / "assets"), name="assets")

    @app.get("/")
    async def root_index():
        idx = UI_DIR / "index.html"
        if idx.exists():
            return FileResponse(idx)
        return JSONResponse({"error": "UI not built"}, status_code=404)

    @app.get("/{path:path}")
    async def ui_static(path: str):
        # Don't swallow API 404s — JSON errors must stay JSON.
        if path.startswith("api/"):
            return JSONResponse({"error": "not_found"}, status_code=404)
        # Serve any file from ui/ at the root URL space; falls back to index.html
        # for unknown paths so client-side hash routes work.
        f = UI_DIR / path
        if f.exists() and f.is_file():
            return FileResponse(f)
        idx = UI_DIR / "index.html"
        if idx.exists():
            return FileResponse(idx)
        return JSONResponse({"error": "not_found"}, status_code=404)
