"""frontend/apiclient.py — sync HTTP-over-UDS client for the daemon (plan 08 §6).

The only "logic" left in the frontend.  Tk's mainloop is synchronous, so we use
**sync httpx** over the daemon's UNIX domain socket (or optional TCP) — no second
asyncio loop in the GUI.  Network calls are kept **off the Tk thread** by the
caller (``app.py`` runs each request in a worker thread and marshals the result
back with ``widget.after(0, …)``); this module is just the transport + typed
return shapes.

Returns lightweight dataclasses so views don't index raw dicts.  A
``DaemonUnavailable`` is raised when the socket is missing/refused so the UI can
show the "daemon not running" banner (08 §7).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

import httpx

log = logging.getLogger("dovw.fe.api")


class DaemonUnavailable(Exception):
    """The daemon socket/endpoint is unreachable (08 §7)."""


@dataclass
class VDesktop:
    index: int | None = None
    name: str | None = None


@dataclass
class Hit:
    field: str
    excerpt: str | None = None


@dataclass
class Window:
    window_uid: int
    x_window_id: str
    wm_class: str | None
    app_name: str | None
    current_title: str | None
    vdesktop: VDesktop | None
    alive: bool
    jumpable: bool
    last_access: float | None
    window_capture_url: str | None
    window_capture_ts: float | None
    usage_5m: float | None = None
    usage_10m: float | None = None
    usage_30m: float | None = None
    usage_1d: float | None = None
    usage_total: float | None = None
    focus_score: float | None = None
    hits: list[Hit] = field(default_factory=list)

    @property
    def display_label(self) -> str:
        """Label text combining app name and title (mirrors reference_v2 hover)."""
        title = self.current_title or "(no title)"
        if self.app_name:
            return f"[{self.app_name}] {title}"
        return title

    @property
    def desktop_badge(self) -> str:
        if not self.vdesktop or self.vdesktop.index is None:
            return ""
        name = self.vdesktop.name or "?"
        return f"[{self.vdesktop.index}: {name}]"

    @classmethod
    def from_json(cls, d: dict) -> "Window":
        vd = d.get("vdesktop")
        return cls(
            window_uid=d["window_uid"], x_window_id=d.get("x_window_id", ""),
            wm_class=d.get("wm_class"), app_name=d.get("app_name"),
            current_title=d.get("current_title"),
            vdesktop=(VDesktop(vd.get("index"), vd.get("name")) if vd else None),
            alive=bool(d.get("alive")), jumpable=bool(d.get("jumpable")),
            last_access=d.get("last_access"),
            window_capture_url=d.get("window_capture_url"),
            window_capture_ts=d.get("window_capture_ts"),
            usage_5m=d.get("usage_5m"), usage_10m=d.get("usage_10m"), usage_30m=d.get("usage_30m"),
            usage_1d=d.get("usage_1d"),
            usage_total=d.get("usage_total"),
            focus_score=d.get("focus_score"),
            hits=[Hit(h["field"], h.get("excerpt")) for h in d.get("hits", [])],
        )


@dataclass
class LaneEvent:
    type: str
    ts: float
    kind: str | None = None
    text: str | None = None

    @classmethod
    def from_json(cls, d: dict) -> "LaneEvent":
        return cls(type=d.get("type", "?"), ts=d["ts"],
                   kind=d.get("kind"), text=d.get("text"))


@dataclass
class TimelineLane:
    window_uid: int
    x_window_id: str | None
    wm_class: str | None
    app_name: str | None
    current_title: str | None
    alive: bool | None
    jumpable: bool | None
    focus_spans: list[dict]
    titles: list[dict]
    events: list[LaneEvent] = field(default_factory=list)
    usage_5m: float | None = None
    usage_10m: float | None = None
    usage_30m: float | None = None
    usage_1d: float | None = None
    usage_total: float | None = None
    focus_score: float | None = None

    @classmethod
    def from_json(cls, d: dict) -> "TimelineLane":
        return cls(
            window_uid=d["window_uid"], x_window_id=d.get("x_window_id"),
            wm_class=d.get("wm_class"), app_name=d.get("app_name"),
            current_title=d.get("current_title"),
            alive=d.get("alive"), jumpable=d.get("jumpable"),
            focus_spans=d.get("focus_spans", []), titles=d.get("titles", []),
            events=[LaneEvent.from_json(e) for e in d.get("events", [])],
            usage_5m=d.get("usage_5m"), usage_10m=d.get("usage_10m"), usage_30m=d.get("usage_30m"),
            usage_1d=d.get("usage_1d"),
            usage_total=d.get("usage_total"),
            focus_score=d.get("focus_score"))


class ApiClient:
    def __init__(self, settings):
        self.s = settings
        if settings.use_tcp:
            host, _, port = settings.tcp_endpoint.partition(":")
            self._base = f"http://{host}:{port or 8765}"
            transport = httpx.HTTPTransport(retries=0)
        else:
            self._base = "http://daemon"   # host ignored for UDS, but required by httpx
            transport = httpx.HTTPTransport(uds=str(settings.socket_path), retries=0)
        self._client = httpx.Client(transport=transport, timeout=settings.request_timeout_s)
        log.info("api client -> %s", settings.tcp_endpoint if settings.use_tcp
                 else settings.socket_path)

    # ───────────────────────── transport ─────────────────────────
    def _get(self, path: str, **params):
        params = {k: v for k, v in params.items() if v is not None}
        try:
            r = self._client.get(self._base + path, params=params)
            r.raise_for_status()
            return r.json()
        except (httpx.ConnectError, httpx.ConnectTimeout, FileNotFoundError) as exc:
            raise DaemonUnavailable(str(exc)) from exc
        except httpx.HTTPError as exc:
            log.warning("GET %s failed: %s", path, exc)
            raise

    def _post(self, path: str, json=None):
        try:
            r = self._client.post(self._base + path, json=json)
            r.raise_for_status()
            return r.json()
        except (httpx.ConnectError, httpx.ConnectTimeout, FileNotFoundError) as exc:
            raise DaemonUnavailable(str(exc)) from exc

    def window_capture_url(self, path: str) -> str:
        return self._base + path

    def get_bytes(self, path: str) -> bytes | None:
        try:
            r = self._client.get(self._base + path)
            if r.status_code != 200:
                return None
            return r.content
        except httpx.HTTPError:
            return None

    # ───────────────────────── endpoints (08 §2-5) ─────────────────────────
    def windows(self, *, sort="last_access", order="desc", alive="both", self_xid=None,
                current_boot_only=None) -> list[Window]:
        log.debug("api windows sort=%s order=%s alive=%s current_boot_only=%s",
                  sort, order, alive, current_boot_only)
        data = self._get("/windows", sort=sort, order=order, alive=alive, self_xid=self_xid,
                         current_boot_only=current_boot_only)
        return [Window.from_json(d) for d in data]

    def search(self, *, q=None, window_uid=None, fields=None, alive="both",
               sort="last_access", order="desc", hits="hit_only", t_from=None, t_to=None,
               self_xid=None, mode="mixed", current_boot_only=None) -> list[Window]:
        path = "/history" if (t_from is not None or t_to is not None) else "/search"
        log.debug("api search q=%s window_uid=%s fields=%s sort=%s order=%s mode=%s current_boot_only=%s",
                  q, window_uid, fields, sort, order, mode, current_boot_only)
        params = dict(q=q, window_uid=window_uid, alive=alive, sort=sort, order=order, hits=hits,
                      self_xid=self_xid, mode=mode, current_boot_only=current_boot_only)
        if fields:
            params["fields"] = ",".join(fields)
        if t_from is not None:
            params["from"] = t_from
        if t_to is not None:
            params["to"] = t_to
        return [Window.from_json(d) for d in self._get(path, **params)]

    def window(self, uid: int) -> dict:
        return self._get(f"/windows/{uid}")

    def window_captures(self, uid: int, *, before=None, after=None, limit=10) -> list[dict]:
        return self._get(f"/windows/{uid}/window_captures", before=before, after=after, limit=limit)

    def timeline(self, *, window_uid=None, sort="last_access", order="desc",
                 t_from=None, t_to=None, self_xid=None,
                 current_boot_only=None) -> list[TimelineLane]:
        params = dict(window_uid=window_uid, sort=sort, order=order, self_xid=self_xid,
                      current_boot_only=current_boot_only)
        if t_from is not None:
            params["from"] = t_from
        if t_to is not None:
            params["to"] = t_to
        return [TimelineLane.from_json(d) for d in self._get("/timeline", **params)]

    def vdesktops(self) -> list[dict]:
        return self._get("/vdesktops")

    def health(self) -> dict:
        return self._get("/health")

    def activate(self, uid: int) -> dict:
        log.debug("api activate window_uid=%d", uid)
        return self._post(f"/windows/{uid}/activate")

    def refresh_window_captures(self) -> dict:
        return self._post("/window_captures/refresh")

    def set_keyboard(self, enabled: bool) -> dict:
        return self._post("/control/keyboard", json={"enabled": enabled})

    def close(self) -> None:
        try:
            self._client.close()
        except Exception:
            pass
