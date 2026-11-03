"""HTTP-client die elke externe call logt en rustig aan doet.

Bij 429/503 wachten we de door de server gevraagde tijd één keer af en geven
daarna op: rate limits worden gerespecteerd, nooit omzeild.
"""
from __future__ import annotations

import sqlite3
import time

import requests

from . import db


class RateLimited(RuntimeError):
    pass


class LoggedClient:
    def __init__(
        self,
        con: sqlite3.Connection,
        source: str,
        *,
        min_interval: float = 0.3,
        session: requests.Session | None = None,
        sleep=time.sleep,
    ):
        self.con = con
        self.source = source
        self.min_interval = min_interval
        self.session = session or requests.Session()
        self._sleep = sleep
        self._last = 0.0

    def _throttle(self) -> None:
        wait = self.min_interval - (time.monotonic() - self._last)
        if wait > 0:
            self._sleep(wait)
        self._last = time.monotonic()

    def request(
        self,
        method: str,
        url: str,
        *,
        log_params: dict | None = None,
        summarize=lambda r: f"{len(r.content)} bytes",
        **kwargs,
    ) -> requests.Response:
        """`log_params` is wat in het logboek komt — geef hier nooit sleutels mee."""
        kwargs.setdefault("timeout", 20)
        for attempt in (1, 2):
            self._throttle()
            try:
                resp = self.session.request(method, url, **kwargs)
            except requests.RequestException as exc:
                db.log_call(self.con, source=self.source, method=method, url=url,
                            params=log_params, status_code=None, result_summary=None,
                            error=repr(exc))
                raise
            if resp.status_code in (429, 503) and attempt == 1:
                retry_after = resp.headers.get("Retry-After", "30")
                delay = min(float(retry_after) if retry_after.isdigit() else 30.0, 120.0)
                db.log_call(self.con, source=self.source, method=method, url=url,
                            params=log_params, status_code=resp.status_code,
                            result_summary=f"rate limit, wacht {delay:.0f}s", error=None)
                self._sleep(delay)
                continue
            break
        ok = resp.status_code < 400
        db.log_call(
            self.con, source=self.source, method=method, url=url, params=log_params,
            status_code=resp.status_code,
            result_summary=summarize(resp) if ok else None,
            error=None if ok else resp.text[:500],
        )
        if resp.status_code in (429, 503):
            raise RateLimited(f"{self.source}: rate limit aangehouden, later opnieuw proberen")
        return resp
