"""
HTTP Request and Response objects for smart.park.

Request  : built once per incoming request; passed to every controller function.
Response : returned by every controller; written back to the client socket.
MultiDict: dict subclass where .get() returns the first value (form fields).

Quick reference:
  request.method          — "GET", "POST", etc.
  request.path            — "/admin/slots"
  request.args.get(key)   — query string value
  request.form.get(key)   — form field value (POST)
  request.param(key)      — URL path parameter (e.g. <slot_id>)
  request.get_json()      — parsed JSON body
  request.current_user    — logged-in user (or None)
  request.session         — server-side session dict

  Response.html(str)      — 200 HTML response
  Response.json(dict)     — 200 JSON response
  Response.redirect(url)  — 302 redirect
  Response.not_found()    — 404
"""
from __future__ import annotations

import json
from http.cookies import SimpleCookie
from typing import Any, Optional
from urllib.parse import parse_qs, urlparse


class MultiDict(dict):
    """A dict where each key maps to a list of values, but `.get()` returns
    only the first one — matching the convenience behaviour expected for
    `request.args` and `request.form`.
    """

    def get(self, key: str, default: Any = None) -> Any:
        values = super().get(key)
        if not values:
            return default
        return values[0]

    def getlist(self, key: str) -> list:
        return super().get(key, [])


def _parse_query_string(qs: str) -> MultiDict:
    parsed = parse_qs(qs, keep_blank_values=True)
    return MultiDict(parsed)


class Request:
    """
    Represents one incoming HTTP request.

    Built once per request in main.py and passed explicitly to every
    controller function. Carries the method, path, form data, cookies,
    session, and the currently logged-in user.
    """

    def __init__(self, method: str, raw_path: str, headers, body: bytes,
                 remote_addr: str = ""):
        self.method = method.upper()
        parsed = urlparse(raw_path)
        self.path = parsed.path
        self.args = _parse_query_string(parsed.query)
        self.headers = headers  # http.client.HTTPMessage-like, case-insensitive .get()
        self.remote_addr = remote_addr
        self._raw_body = body or b""

        # Path parameters injected by the router once a route pattern
        # matches (e.g. {"location_id": "3"} for "/locations/<location_id>").
        self.path_params: dict[str, str] = {}

        # Parsed lazily / eagerly depending on content type.
        self.form = MultiDict()
        self._json: Optional[Any] = None
        self._json_parsed = False

        content_type = (self.headers.get("Content-Type") or "").lower()
        if self.method in ("POST", "PUT", "PATCH"):
            if "application/x-www-form-urlencoded" in content_type:
                decoded = self._raw_body.decode("utf-8", errors="replace")
                self.form = _parse_query_string(decoded)
            elif "application/json" in content_type:
                pass  # parsed on demand via get_json()

        # Cookies
        self.cookies: dict[str, str] = {}
        cookie_header = self.headers.get("Cookie")
        if cookie_header:
            jar = SimpleCookie()
            jar.load(cookie_header)
            self.cookies = {k: morsel.value for k, morsel in jar.items()}

        # Filled in by the session middleware (see auth/session_manager.py)
        self.session: dict[str, Any] = {}
        self._session_id: Optional[str] = None

        # `current_user`. Defaults to an anonymous stand-in.
        self.current_user = None

    def get_json(self, silent: bool = True) -> Optional[dict]:
        """Parse and return the JSON request body.
        Returns None (instead of raising) when silent=True."""
        if self._json_parsed:
            return self._json
        self._json_parsed = True
        if not self._raw_body:
            self._json = None
            return None
        try:
            self._json = json.loads(self._raw_body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            if not silent:
                raise
            self._json = None
        return self._json

    def param(self, name: str, default: Any = None) -> Any:
        """Fetch a value from the matched URL path parameters, e.g. the
        ``location_id`` in ``/locations/<location_id>``."""
        return self.path_params.get(name, default)


class Response:
    """
    Represents one outgoing HTTP response.

    Controllers always return one of these (via the classmethod helpers
    below) so main.py has a single type to write back to the socket.
    """

    def __init__(self, body: bytes = b"", status: int = 200,
                 headers: Optional[dict] = None,
                 content_type: str = "text/html; charset=utf-8"):
        self.body = body
        self.status = status
        self.headers = headers or {}
        if "Content-Type" not in self.headers:
            self.headers["Content-Type"] = content_type
        self.set_cookies: list[str] = []

    @classmethod
    def html(cls, body: str, status: int = 200, headers: Optional[dict] = None) -> "Response":
        return cls(body.encode("utf-8"), status=status, headers=headers,
                    content_type="text/html; charset=utf-8")

    @classmethod
    def json(cls, data: Any, status: int = 200) -> "Response":
        body = json.dumps(data, default=str).encode("utf-8")
        return cls(body, status=status, content_type="application/json")

    @classmethod
    def redirect(cls, location: str, status: int = 302) -> "Response":
        resp = cls(b"", status=status, content_type="text/html; charset=utf-8")
        resp.headers["Location"] = location
        return resp

    @classmethod
    def not_found(cls, body: str = "Not Found") -> "Response":
        return cls.html(body, status=404)

    @classmethod
    def stream(cls, chunks_iterable, content_type: str) -> "StreamingResponse":
        """Return a streaming response (used by the CCTV camera proxy).
        Chunks are written directly to the socket without buffering."""
        return StreamingResponse(chunks_iterable, content_type=content_type)

    def set_cookie(self, name: str, value: str, max_age: Optional[int] = None,
                    http_only: bool = True, path: str = "/", same_site: str = "Lax") -> None:
        cookie = SimpleCookie()
        cookie[name] = value
        cookie[name]["path"] = path
        if max_age is not None:
            cookie[name]["max-age"] = max_age
        if http_only:
            cookie[name]["httponly"] = True
        cookie[name]["samesite"] = same_site
        self.set_cookies.append(cookie[name].OutputString())


class StreamingResponse:
    """A response whose body is produced lazily, chunk by chunk.
    Used by the CCTV camera proxy — main.py writes each chunk directly
    to the socket instead of buffering the whole body.
    """

    def __init__(self, chunks_iterable, content_type: str, status: int = 200):
        self.chunks_iterable = chunks_iterable
        self.content_type = content_type
        self.status = status
        self.headers = {"Content-Type": content_type}
        self.set_cookies: list[str] = []
