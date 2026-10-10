"""QR rendering.

QR codes are drawn server-side as inline SVG — no image files, no external
service, and the payload never leaves our infrastructure.
"""
from __future__ import annotations

import io
import logging

from django.utils.safestring import mark_safe

logger = logging.getLogger("AI.qr")


#: Substituted into the rendered SVG afterwards. The QR writer only accepts a
#: real colour, but a code that has to be legible in both themes wants to
#: inherit the surrounding text colour instead — so it is drawn in a sentinel
#: nobody would choose and the sentinel is swapped for the keyword.
_SENTINEL = "#010203"


def svg(payload: str, *, scale: int = 5, border: int = 2, dark: str = "#0B1020") -> str:
    """Return an inline ``<svg>`` for ``payload``, or an empty string on failure.

    ``dark`` may be ``currentColor``, in which case the code inherits the text
    colour of wherever it is placed and so stays readable in both themes.
    """
    if not payload:
        return ""
    inherit = dark == "currentColor"
    if inherit:
        dark = _SENTINEL
    try:
        import segno
    except ImportError:  # pragma: no cover — dependency documented in requirements
        logger.warning("segno is not installed; QR codes cannot be rendered.")
        return ""

    try:
        code = segno.make(payload, error="m")
        buffer = io.BytesIO()
        code.save(
            buffer,
            kind="svg",
            scale=scale,
            border=border,
            dark=dark,
            light=None,
            xmldecl=False,
            svgns=True,
            omitsize=True,
        )
        markup = buffer.getvalue().decode()
        if inherit:
            markup = markup.replace(_SENTINEL, "currentColor")
        return mark_safe(markup)
    except Exception:  # pragma: no cover — payload too long for any version
        logger.exception("QR rendering failed")
        return ""
