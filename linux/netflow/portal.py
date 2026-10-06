"""HTTP portal: the same page and JSON API as the ESP32 edition, served on both
the TV network and the home LAN (nftables decides who may reach it)."""
from __future__ import annotations

import json
import logging
from pathlib import Path

from aiohttp import web

from .app import ApiError, Controller

log = logging.getLogger(__name__)

PAGE = (Path(__file__).parent / "page.html").read_bytes()


def make_app(ctl: Controller) -> web.Application:
    async def page(_req: web.Request) -> web.Response:
        return web.Response(body=PAGE, content_type="text/html", charset="utf-8")

    def getter(fn):
        async def handler(_req: web.Request) -> web.Response:
            return web.json_response(fn())

        return handler

    def poster(fn):
        async def handler(req: web.Request) -> web.Response:
            try:
                body = json.loads(await req.text() or "{}")
                if not isinstance(body, dict):
                    raise ApiError(400, "bad json")
                fn(body)
            except ApiError as e:
                return web.Response(status=e.status, text=e.text)
            except (ValueError, TypeError) as e:  # bad JSON or a non-numeric field
                return web.Response(status=400, text=f"bad request: {e}")
            return web.json_response({"ok": True})

        return handler

    async def reset_usage(_req: web.Request) -> web.Response:
        ctl.reset_usage()
        return web.json_response({"ok": True})

    async def elsewhere(_req: web.Request) -> web.Response:
        raise web.HTTPFound("/")

    app = web.Application(client_max_size=64 * 1024)
    app.router.add_get("/", page)
    app.router.add_get("/api/status", getter(ctl.status))
    app.router.add_get("/api/devices", getter(ctl.devices))
    app.router.add_post("/api/device", poster(ctl.update_device))
    app.router.add_post("/api/global", poster(ctl.update_global))
    app.router.add_post("/api/extend", poster(ctl.extend))
    app.router.add_post("/api/reset-usage", reset_usage)
    app.router.add_route("*", "/{tail:.*}", elsewhere)
    return app


async def serve(ctl: Controller, port: int) -> None:
    runner = web.AppRunner(make_app(ctl), access_log=None)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", port).start()
    log.info("portal on :%d", port)
