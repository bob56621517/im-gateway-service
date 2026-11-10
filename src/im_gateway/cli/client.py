"""HTTP client for the im-gateway control plane.

The CLI is a **pure HTTP client**: it never imports the gateway or database
layers (HANDOVER §5.6 / §13.8). The single exception is the lazy import inside
``imgw run``, which lives in :mod:`im_gateway.cli.main` — not here.

Responsibilities of this module:

* resolve the control-plane address (``--url`` > ``IMGW_URL`` > default listen)
  and support both ``http(s)://`` and ``unix://`` endpoints;
* resolve the bearer token (``--token`` > ``IMGW_TOKEN`` > token file);
* turn HTTP/transport failures into :class:`ControlPlaneError`, a message the
  CLI can print verbatim (the server's ``{"detail": ...}`` is preserved).

User-facing prose is translated through :func:`im_gateway.cli.i18n.t`; the
English string is the message id, so English output is unchanged.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from typing import Any

import httpx

from im_gateway.cli.i18n import t
from im_gateway.core import paths

__all__ = [
    "DEFAULT_TIMEOUT",
    "DEFAULT_URL",
    "ControlPlaneError",
    "api",
    "build_client",
    "decode_response",
    "resolve_token",
    "resolve_url",
]

DEFAULT_URL = "http://127.0.0.1:8765"
DEFAULT_TIMEOUT = 10.0

_START_HINT = t("start it with `imgw run` (or check --url / IMGW_URL)")
_TOKEN_HINT = t(
    "no control-plane token found; start the daemon with `imgw run` so it creates "
    "the token file, or pass --token / set IMGW_TOKEN"
)


class ControlPlaneError(Exception):
    """A user-facing CLI error: unreachable daemon, bad response or missing token."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        detail: str | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.detail = detail


# ---------------------------------------------------------------------------
# Address / token resolution
# ---------------------------------------------------------------------------


def _normalise_url(raw: str) -> str:
    value = raw.strip()
    if not value:
        return DEFAULT_URL
    if value.startswith(("http://", "https://", "unix://")):
        return value
    if value.startswith("tcp://"):
        return "http://" + value[len("tcp://") :]
    return "http://" + value


def resolve_url(url: str | None = None) -> str:
    """Return the control-plane base URL.

    Precedence: explicit ``--url``, then ``IMGW_URL``, then the daemon's default
    listen address from :func:`im_gateway.core.paths.default_listen` (which is
    ``http://127.0.0.1:8765`` on Windows and a ``unix://`` socket on POSIX).
    ``tcp://host:port`` is accepted and normalised to ``http://host:port``.
    """
    if url is not None and url.strip():
        return _normalise_url(url)
    env_url = os.environ.get("IMGW_URL", "")
    if env_url.strip():
        return _normalise_url(env_url)
    return _normalise_url(paths.default_listen())


def resolve_token(token: str | None = None) -> str:
    """Return the control-plane bearer token.

    Precedence: explicit ``--token``, then ``IMGW_TOKEN``, then the token file
    (``IMGW_TOKEN_FILE`` or :func:`im_gateway.core.paths.default_token_file`).
    """
    if token is not None and token.strip():
        return token.strip()
    env_token = os.environ.get("IMGW_TOKEN", "")
    if env_token.strip():
        return env_token.strip()
    token_file = paths.default_token_file()
    try:
        stored = token_file.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise ControlPlaneError(
            t(
                "cannot read control-plane token {path}: {error}. {hint}",
                path=token_file,
                error=exc,
                hint=_TOKEN_HINT,
            )
        ) from exc
    if not stored:
        raise ControlPlaneError(
            t(
                "control-plane token file {path} is empty. {hint}",
                path=token_file,
                hint=_TOKEN_HINT,
            )
        )
    return stored


# ---------------------------------------------------------------------------
# Client construction
# ---------------------------------------------------------------------------


def build_client(
    url: str | None = None,
    token: str | None = None,
    *,
    timeout: float = DEFAULT_TIMEOUT,
) -> httpx.Client:
    """Build an authenticated :class:`httpx.Client` for the control plane.

    ``unix://`` addresses are served over a Unix domain socket transport; the
    HTTP host is then irrelevant (``http://localhost``) because ``uds=`` decides
    where the connection actually goes.
    """
    base_url = resolve_url(url)
    transport: httpx.BaseTransport | None = None
    if base_url.startswith("unix://"):
        socket_path = base_url[len("unix://") :]
        if not socket_path:
            raise ControlPlaneError(
                t("invalid control-plane URL: unix:// address has no socket path")
            )
        base_url = "http://localhost"
        transport = httpx.HTTPTransport(uds=socket_path)
    return httpx.Client(
        base_url=base_url,
        transport=transport,
        headers={"Authorization": f"Bearer {resolve_token(token)}"},
        timeout=timeout,
    )


# ---------------------------------------------------------------------------
# Request helper
# ---------------------------------------------------------------------------


def api(
    client: httpx.Client,
    method: str,
    path: str,
    *,
    params: Mapping[str, Any] | None = None,
    json_body: Any | None = None,
    headers: Mapping[str, str] | None = None,
    timeout: float | None = None,
) -> Any:
    """Send one control-plane request and return its decoded JSON body."""
    try:
        response = client.request(
            method,
            path,
            params=params,
            json=json_body,
            headers=headers,
            timeout=timeout,
        )
    except httpx.TransportError as exc:
        raise ControlPlaneError(
            t(
                "cannot reach the im-gateway daemon at {url}: {error}. {hint}",
                url=client.base_url,
                error=exc,
                hint=_START_HINT,
            )
        ) from exc
    return decode_response(response)


def decode_response(response: httpx.Response) -> Any:
    """Decode a control-plane response; non-2xx becomes :class:`ControlPlaneError`."""
    if not response.is_success:
        detail = _error_detail(response)
        request = response.request
        raise ControlPlaneError(
            t(
                "{method} {path} failed with HTTP {status}: {detail}",
                method=request.method,
                path=request.url.path,
                status=response.status_code,
                detail=detail,
            ),
            status_code=response.status_code,
            detail=detail,
        )
    if not response.content:
        return None
    try:
        return response.json()
    except ValueError as exc:
        raise ControlPlaneError(
            t(
                "daemon returned a non-JSON body for {path}: {body}",
                path=response.request.url.path,
                body=response.text[:200],
            )
        ) from exc


def _error_detail(response: httpx.Response) -> str:
    try:
        payload = response.json()
    except ValueError:
        payload = None
    if isinstance(payload, dict):
        detail = payload.get("detail")
        if isinstance(detail, str) and detail.strip():
            return detail.strip()
        if detail is not None:
            return json.dumps(detail, ensure_ascii=False)
        message = payload.get("message")
        if isinstance(message, str) and message.strip():
            return message.strip()
    text = response.text.strip()
    if text:
        return text[:500]
    return f"HTTP {response.status_code} {response.reason_phrase}"
