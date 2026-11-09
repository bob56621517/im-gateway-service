"""Terminal QR rendering for ``imgw channel login`` (HANDOVER §5.6, §13.7).

``channel login`` itself is milestone 2: the daemon will answer with a login
URL (or a short-lived token) that the CLI has to show on screen. This module
provides the rendering primitives now so the QR dependency and output shape are
settled and testable without a real login flow.

``segno`` is the only dependency; no image library and no extra process.
"""

from __future__ import annotations

import io

import segno

__all__ = ["render_qr", "render_qr_bytes"]

#: Medium error correction is the usual trade-off for login QR codes: robust
#: enough for phone cameras, small enough for an 80-column terminal.
_ERROR_CORRECTION = "m"


def _make(text: str) -> segno.QRCode:
    if not text:
        raise ValueError("cannot render a QR code for empty text")
    return segno.make(text, error=_ERROR_CORRECTION)


def render_qr(text: str, *, compact: bool = True, border: int = 2) -> str:
    """Render ``text`` as a QR code string for terminal output.

    ``compact=True`` uses half-block characters (one text line per two module
    rows), which fits in a normal terminal. The caller decides how to print the
    returned string (``typer.echo`` handles the-encoding).
    """
    buffer = io.StringIO()
    _make(text).terminal(out=buffer, compact=compact, border=border)
    return buffer.getvalue()


def render_qr_bytes(text: str, *, scale: int = 4, border: int = 2) -> bytes:
    """Render ``text`` as PNG bytes (for image-capable terminals, logs or docs)."""
    buffer = io.BytesIO()
    _make(text).save(buffer, kind="png", scale=scale, border=border)
    return buffer.getvalue()
