"""Starlette application: serves the frontend, one command endpoint, one state WebSocket.

Workspaces: without an explicit db path the app runs in multi-workspace
mode — every subfolder of the data root (``./data`` or
``$CANOPEN_BENCH_DATA``, e.g. a mounted Docker volume) is one workspace
holding its own db, EDS files, traces and flows. The active workspace is
remembered in ``<root>/active-workspace`` and can be switched from the
Setup page at runtime; switching swaps the whole Bench instance. An
explicit ``--db``/``$CANOPEN_BENCH_DB`` path keeps the old single-file
behaviour (tests, embedding, experts).
"""
from __future__ import annotations

import asyncio
import contextlib
import os
from datetime import datetime
from pathlib import Path

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, Response
from starlette.routing import Mount, Route, WebSocketRoute
from starlette.staticfiles import StaticFiles
from starlette.websockets import WebSocket, WebSocketDisconnect

from .bus.interface import BusInterface
from .core import Bench
from .db import Db
from .plugin import BenchPlugin

STATIC = Path(__file__).parent / "static"
DEFAULT_WORKSPACE = "default"


def _active_workspace(root: Path) -> str:
    try:
        name = (root / "active-workspace").read_text(encoding="utf-8").strip()
    except OSError:
        return DEFAULT_WORKSPACE
    return name if name and (root / name).is_dir() else DEFAULT_WORKSPACE


def create_app(db_path: str | None = None, bus: BusInterface | None = None,
               plugins: list[BenchPlugin] | None = None) -> Starlette:
    explicit = db_path or os.environ.get("CANOPEN_BENCH_DB")
    if explicit:
        root: Path | None = None
        path = Path(explicit)
    else:
        root = Path(os.environ.get("CANOPEN_BENCH_DATA", "data"))
        path = root / _active_workspace(root) / "canopen-bench.db"

    clients: set[WebSocket] = set()
    holder: dict = {"bench": None, "ticker": None}

    async def broadcast() -> None:
        if not clients:
            return
        snap = holder["bench"].snapshot()
        dead = []
        for ws in clients:
            try:
                await ws.send_json({"type": "state", "state": snap})
            except Exception:
                dead.append(ws)
        for ws in dead:
            clients.discard(ws)

    def _build_bench(db: Db) -> Bench:
        bench = Bench(db, bus=bus, plugins=plugins, workspaces_root=root)
        bench.set_notifier(broadcast)
        if root is not None:
            bench.on_workspace_switch = lambda name: asyncio.ensure_future(_switch(name))
            bench.on_plugin_reload = lambda: asyncio.ensure_future(_reload_plugins())
        return bench

    async def _rebuild(next_db: Db, new_workspace: str | None) -> None:
        """Shared teardown/boot for both workspace switching and a plugin
        reload: stop the tick loop, tear down the bus, hand off to a fresh
        Bench (fresh plugin discovery included — Bench.__init__ always
        re-runs load_plugins()), restart the tick loop. `new_workspace` is
        the workspace name to persist as active, or None to stay put (a
        plugin reload never changes which workspace is active)."""
        old: Bench = holder["bench"]
        holder["ticker"].cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await holder["ticker"]
        old.shutdown()
        if new_workspace is not None:
            old.db.close()
            (root / "active-workspace").write_text(new_workspace, encoding="utf-8")
        bench = _build_bench(next_db)
        holder["bench"] = bench
        app.state.bench = bench
        bench.startup()
        holder["ticker"] = asyncio.create_task(bench.tick_loop())
        await broadcast()

    async def _switch(name: str) -> None:
        """Swap the whole Bench for another workspace: closes the old db,
        opens the new workspace's."""
        await _rebuild(Db(root / name / "canopen-bench.db"), new_workspace=name)

    async def _reload_plugins() -> None:
        """Rebuild the Bench after a plugin install/removal, same workspace
        and the same live db connection — only the plugin set changes."""
        await _rebuild(holder["bench"].db, new_workspace=None)

    holder["bench"] = _build_bench(Db(path))

    @contextlib.asynccontextmanager
    async def lifespan(app: Starlette):
        holder["bench"].startup()
        holder["ticker"] = asyncio.create_task(holder["bench"].tick_loop())
        yield
        holder["ticker"].cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await holder["ticker"]
        holder["bench"].shutdown()
        holder["bench"].db.close()

    async def index(request: Request) -> FileResponse:
        return FileResponse(STATIC / "index.html")

    async def state(request: Request) -> JSONResponse:
        return JSONResponse(holder["bench"].snapshot())

    async def action(request: Request) -> JSONResponse:
        """Parsed by hand rather than through a validation library: one
        endpoint, two fields. `dispatch` validates the action name and its
        params anyway, and raises ValueError with a message for the UI."""
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"ok": False, "error": "body must be JSON"}, status_code=400)
        if not isinstance(body, dict) or not isinstance(body.get("action"), str):
            return JSONResponse({"ok": False, "error": "missing 'action'"}, status_code=400)
        params = body.get("params") or {}
        if not isinstance(params, dict):
            return JSONResponse({"ok": False, "error": "'params' must be an object"},
                                status_code=400)
        try:
            holder["bench"].dispatch(body["action"], params)
        except ValueError as e:
            return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
        return JSONResponse({"ok": True})

    # Plain GET downloads, deliberately outside the action/WebSocket state
    # machine: a browser download is a different concern (Content-Disposition,
    # not JSON) and doesn't mutate any state, so it doesn't need dispatch().
    def _download(body: str, media_type: str, suffix: str) -> Response:
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        return Response(content=body, media_type=media_type, headers={
            "Content-Disposition": f'attachment; filename="trace_{stamp}.{suffix}"'})

    async def export_csv(request: Request) -> Response:
        return _download(holder["bench"]._trace_csv(), "text/csv", "csv")

    async def export_candump(request: Request) -> Response:
        return _download(holder["bench"]._trace_candump(), "text/plain", "candump.log")

    async def ws(websocket: WebSocket) -> None:
        await websocket.accept()
        clients.add(websocket)
        try:
            await websocket.send_json({"type": "state", "state": holder["bench"].snapshot()})
            while True:
                await websocket.receive_text()  # client sends pings only
        except WebSocketDisconnect:
            pass
        finally:
            clients.discard(websocket)

    app = Starlette(lifespan=lifespan, routes=[
        Route("/", index),
        Route("/api/state", state),
        Route("/api/action", action, methods=["POST"]),
        Route("/api/trace/export.csv", export_csv),
        Route("/api/trace/export/candump", export_candump),
        WebSocketRoute("/ws", ws),
        Mount("/static", StaticFiles(directory=STATIC), name="static"),
    ])
    app.state.bench = holder["bench"]
    return app
