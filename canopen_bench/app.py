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
        # over a snapshot of the set, not the set itself: sending is an
        # await, and a browser connecting or a tab closing in that window
        # mutates `clients` mid-iteration. That raised "Set changed size
        # during iteration" *out of the whole broadcast*, so every client
        # after the one being sent to missed that update — and since this
        # runs as a fire-and-forget task, the only trace was an unretrieved
        # exception on the console while a screen quietly went stale.
        for ws in tuple(clients):
            try:
                await ws.send_json({"type": "state", "state": snap})
            except Exception:
                dead.append(ws)
        for ws in dead:
            clients.discard(ws)

    async def safe_broadcast() -> None:
        """Push state, and never let failing to push break anything else.

        The tick loop awaits this every tick and the Bench fires it after
        every action. A raise out of here used to take the whole tick loop
        with it — and since nothing closes the WebSocket, the browser kept
        a healthy socket that simply never received another message. No
        reconnect, no error on screen: a run that had finished still read
        "Running…".
        """
        try:
            await broadcast()
        except Exception as exc:      # noqa: BLE001 — the point is to catch all
            bench = holder.get("bench")
            if bench is not None:
                bench.log(f"APP  state not pushed — {type(exc).__name__}: {exc}", "emcy0")

    def _build_bench(db: Db) -> Bench:
        bench = Bench(db, bus=bus, plugins=plugins, workspaces_root=root)
        bench.set_notifier(safe_broadcast)
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
        # Close the state channels rather than waiting for the browsers to
        # do it. uvicorn's graceful shutdown waits for open connections,
        # and a tab left open on another screen never closes this one — so
        # Ctrl+C started a shutdown that never finished, the process stayed
        # alive holding the supply's serial port, and the next start found
        # no power supply. __main__ caps that wait as well; this is what
        # makes the normal case end at once instead of at the cap.
        for ws in tuple(clients):
            with contextlib.suppress(Exception):
                await ws.close()
        clients.clear()
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

    #: Most rows one request may ask for. A window is what fits on a screen
    #: plus a little either side; anything larger is someone asking for the
    #: whole buffer a page at a time, which is what the exports are for.
    TRACE_PAGE_MAX = 2000

    async def trace_rows(request: Request) -> JSONResponse:
        """The slice of the trace the panel is scrolled to, newest first.

        A GET rather than an action, for the same reason as the exports: it
        reads, it does not mutate, and the answer belongs to the one panel
        that asked rather than in the snapshot every browser receives ten
        times a second.
        """
        def _int(name: str, default: int, lo: int, hi: int) -> int:
            try:
                return max(lo, min(hi, int(request.query_params[name])))
            except (KeyError, TypeError, ValueError):
                return default

        return JSONResponse(holder["bench"]._trace_page(
            _int("end", 0, 0, 100_000_000), _int("n", 200, 1, TRACE_PAGE_MAX)))

    async def export_csv(request: Request) -> Response:
        return _download(holder["bench"]._trace_csv(), "text/csv", "csv")

    async def export_candump(request: Request) -> Response:
        return _download(holder["bench"]._trace_candump(), "text/plain", "candump.log")

    async def report(request: Request) -> Response:
        """One file out of the results folder.

        The reports are written as HTML that links to each other and to a
        stylesheet by bare file name, so they are served under one prefix
        and those relative links keep working — a summary opened here can
        still reach its per-case pages.

        Only a plain name is accepted, and only from that folder: the name
        arrives from the URL, and a results folder is a path the operator
        chose, so "whatever is under it" must not become "whatever is on
        the disk".
        """
        name = request.path_params["name"]
        folder = holder["bench"]._results_dir().resolve()
        target = (folder / name).resolve()
        if (Path(name).name != name or target.parent != folder
                or target.suffix.lower() not in (".html", ".json", ".css")
                or not target.is_file()):
            return Response("not found", status_code=404)
        return FileResponse(target)

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
        Route("/api/trace/rows", trace_rows),
        Route("/api/trace/export.csv", export_csv),
        Route("/api/trace/export/candump", export_candump),
        Route("/api/report/{name}", report),
        WebSocketRoute("/ws", ws),
        Mount("/static", StaticFiles(directory=STATIC), name="static"),
    ])
    app.state.bench = holder["bench"]
    app.state.ws_clients = clients   # for tests; the app itself closes over it
    return app
