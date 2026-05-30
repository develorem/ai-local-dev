"""FastAPI app entrypoint."""

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from server.auth import AUTH_ENABLED, auth_middleware
from server.config import config
from server.db import init_db, run_migrations
from server.mcp_integration import mcp_asgi, mcp_server
from server.reaper import start_reaper, stop_reaper
from server.scheduler import start_scheduler, stop_scheduler
from server.routes import api

# Single-user local dev tool: disable browser caching on UI assets so the
# user always gets the latest build the moment we redeploy. Bandwidth cost
# is negligible (single small JS/CSS/HTML).
_NO_CACHE = {
    "Cache-Control": "no-cache, no-store, must-revalidate",
    "Pragma": "no-cache",
    "Expires": "0",
}


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    result = run_migrations()
    print(f"[orchestrai] migrations: applied={result['applied']} version={result['version']}",
          flush=True)
    reaper_task = start_reaper()
    print(f"[orchestrai] reaper started (interval={config.REAPER_INTERVAL_SEC}s)", flush=True)
    scheduler_task = start_scheduler()
    print("[orchestrai] scheduler started", flush=True)
    if AUTH_ENABLED:
        print("[orchestrai] token auth ENABLED (operator token set)", flush=True)
    else:
        print("[orchestrai] WARNING: token auth DISABLED — no operator token set. "
              "Do NOT expose this hub to an untrusted network.", flush=True)
    # Run the MCP streamable-HTTP session manager for the life of the app.
    async with mcp_server.session_manager.run():
        print("[orchestrai] MCP endpoint mounted at /mcp", flush=True)
        try:
            yield
        finally:
            await stop_reaper(reaper_task)
            await stop_scheduler(scheduler_task)


app = FastAPI(
    title="OrchestrAi Hub",
    version=config.VERSION,
    lifespan=lifespan,
)

# Token auth on every route except health / webhooks / static UI (see server.auth).
app.middleware("http")(auth_middleware)

app.include_router(api)

# Hosted MCP endpoint (streamable HTTP), mounted before the UI catch-all so it
# isn't shadowed. The mount matches /mcp/ and /mcp/...; a bare /mcp wouldn't, so
# redirect it (307 preserves method+body; MCP clients follow redirects). Net: a
# client configured with .../mcp just works.
@app.api_route("/mcp", methods=["GET", "POST", "DELETE"], include_in_schema=False)
async def _mcp_no_trailing_slash():
    return RedirectResponse("/mcp/", status_code=307)

app.mount("/mcp", mcp_asgi)

# Static UI — no-cache everywhere so a fresh deploy is visible without a hard refresh.
UI_DIR = Path(__file__).resolve().parent.parent / "ui"
if UI_DIR.exists():

    class _NoCacheStatic(StaticFiles):
        async def get_response(self, path, scope):
            response = await super().get_response(path, scope)
            for k, v in _NO_CACHE.items():
                response.headers[k] = v
            return response

    app.mount("/assets", _NoCacheStatic(directory=UI_DIR / "assets"), name="assets")

    @app.get("/")
    async def root_index():
        idx = UI_DIR / "index.html"
        if idx.exists():
            return FileResponse(idx, headers=_NO_CACHE)
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
            return FileResponse(f, headers=_NO_CACHE)
        idx = UI_DIR / "index.html"
        if idx.exists():
            return FileResponse(idx, headers=_NO_CACHE)
        return JSONResponse({"error": "not_found"}, status_code=404)
