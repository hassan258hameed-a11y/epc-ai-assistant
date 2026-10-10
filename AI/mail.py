"""E-mail backends.

Only the development console backend lives here. Real SMTP delivery uses
Django's own backend and is unaffected by anything in this module.
"""
from __future__ import annotations

import sys

from django.core.mail.backends.console import EmailBackend as DjangoConsoleBackend


class UnicodeSafeConsoleEmailBackend(DjangoConsoleBackend):
    """Console backend that cannot be killed by the Windows console encoding.

    Windows terminals default to a legacy code page (cp1252), so printing a
    message containing any non-ASCII character — an em dash, a currency
    symbol, a student's name in Khmer — raises ``UnicodeEncodeError``.
    Django's console backend does not catch that, so the *entire send* fails and
    the verification code is never shown. On a developer machine that looks
    exactly like "the code never arrived".

    Writing through a UTF-8 wrapper that replaces unencodable characters keeps
    the message readable and, crucially, keeps the code visible.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.stream = self._safe_stream(self.stream)

    @staticmethod
    def _safe_stream(stream):
        # Python 3.7+ can reconfigure a text stream in place.
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
            return stream
        except (AttributeError, ValueError, OSError):
            pass

        # Otherwise wrap the underlying buffer.
        buffer = getattr(stream, "buffer", None)
        if buffer is not None:
            import io

            try:
                return io.TextIOWrapper(buffer, encoding="utf-8", errors="replace", line_buffering=True)
            except (AttributeError, ValueError, OSError):
                pass

        return stream

    def write_message(self, message):
        try:
            return super().write_message(message)
        except UnicodeEncodeError:
            # Last resort: never let a console encoding problem lose the e-mail.
            body = message.message().as_string()
            self.stream.write(body.encode("ascii", "replace").decode("ascii"))
            self.stream.write("\n" + "-" * 79 + "\n")
            return True
