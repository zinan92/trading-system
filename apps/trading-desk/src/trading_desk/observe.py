"""Request records for the desk (#1364). One line per API request, and an unhandled exception becomes
a reference number the page can show, with the traceback in the log under the same number. Query
strings stay out: the path says what was asked."""
from __future__ import annotations

import logging
import time

from fastapi import Request
from fastapi.responses import JSONResponse

from services.structured_log import event, get_logger, set_request_id

LOG = get_logger("desk.http")
QUIET = ("/static/", "/newsletter-asset/")


def install(app) -> None:
    """Install it last so it runs first and sees the path before open_api rewrites it."""

    @app.middleware("http")
    async def observe(request: Request, call_next):
        ident = set_request_id()
        path, started = request.url.path, time.perf_counter()
        fields = {"method": request.method, "path": path}
        try:
            response = await call_next(request)
        except Exception:  # noqa: BLE001 - the one place that sees what no route handled
            LOG.error("unhandled_exception", exc_info=True, extra={"fields": fields})
            return JSONResponse({"detail": f"服务器出错，参考编号 {ident}", "ref": ident}, status_code=500, headers={"X-Request-Id": ident})
        if not path.startswith(QUIET):
            event(LOG, "http", level=logging.WARNING if response.status_code >= 500 else logging.INFO, **fields,
                  status=response.status_code, ms=round((time.perf_counter() - started) * 1000))
        response.headers["X-Request-Id"] = ident
        return response
