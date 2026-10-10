"""The contract every AI vendor implementation satisfies.

Nothing above this layer knows what an OpenAI payload or an Anthropic payload
looks like.  Callers hand a list of :class:`ChatTurn` to :meth:`BaseProvider.complete`
and get an :class:`AIResponse` back, whichever vendor answered.

Two rules make that abstraction worth having:

* **Errors are translated here.** Every vendor raises :class:`AIError` with a
  message that is already safe to show a student; raw vendor text, keys, URLs
  and stack traces stay in the server log.
* **Usage is reported, never estimated.** ``input_tokens`` / ``output_tokens``
  come from the vendor's own accounting, because that is what the student is
  charged for.
"""
from __future__ import annotations

import base64
import binascii
import math
import threading
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

#: ``finish_reason`` of an answer the student stopped part-way. Its usage is
#: partly estimated: a vendor reports usage only at the end of a stream.
STOPPED = "stopped"


class AIError(Exception):
    """Provider failure carrying a message that is safe to show a student."""

    def __init__(
        self,
        message: str,
        *,
        code: str = "ai_error",
        status: int | None = None,
        retryable: bool = True,
    ):
        self.friendly_message = message
        self.code = code
        self.status = status
        self.retryable = retryable
        super().__init__(message)


@dataclass
class AIResponse:
    content: str
    input_tokens: int
    output_tokens: int
    total_tokens: int
    model: str
    request_id: str = ""
    latency_ms: int = 0
    finish_reason: str = ""
    #: Hidden thinking tokens on reasoning models (already counted inside
    #: ``output_tokens``). Exposed so admins can see why a model costs more.
    reasoning_tokens: int = 0
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class ChatTurn:
    """Provider-neutral message passed into :meth:`BaseProvider.complete`."""

    role: str
    content: str
    #: Kept for callers that send exactly one image.
    image_data_url: str | None = None
    #: Several images on one turn. Merged with ``image_data_url`` by :meth:`images`.
    image_data_urls: list[str] = field(default_factory=list)

    def images(self) -> list[str]:
        combined = list(self.image_data_urls)
        if self.image_data_url and self.image_data_url not in combined:
            combined.insert(0, self.image_data_url)
        return combined


class CancelToken:
    """Lets another thread stop a streamed answer.

    ``cancel()`` is called by the request that pressed Stop. The provider
    registers a hook that closes its connection, so a stop takes effect at
    once rather than at the next chunk — a reasoning model can be silent for
    a long time before it writes anything. ``check`` is polled as well, for a
    stop that arrived at another server process (through the cache).
    """

    def __init__(self, check: Callable[[], bool] | None = None):
        self._event = threading.Event()
        self._check = check
        self._hooks: list[Callable[[], Any]] = []
        self._lock = threading.Lock()

    def cancel(self) -> None:
        with self._lock:
            if self._event.is_set():
                return
            self._event.set()
            hooks = list(self._hooks)
        for hook in hooks:
            try:
                hook()
            except Exception:  # pragma: no cover — closing is best-effort
                pass

    @property
    def cancelled(self) -> bool:
        if self._event.is_set():
            return True
        if self._check is not None and self._check():
            self.cancel()
            return True
        return False

    def on_cancel(self, hook: Callable[[], Any]) -> None:
        with self._lock:
            if not self._event.is_set():
                self._hooks.append(hook)
                return
        hook()


def estimate_tokens(text: str) -> int:
    """~4 characters per token. Only for a stream that ended before the vendor
    reported its usage: a stopped answer, or an endpoint that never reports it."""
    return math.ceil(len(text or "") / 4)


def split_data_url(url: str) -> tuple[str, str] | None:
    """Pull ``(media_type, base64 payload)`` out of a ``data:`` URL.

    OpenAI takes the whole URL as-is; Anthropic wants the two halves separately.
    Anything that is not a valid base64 data URL returns ``None`` so the caller
    can drop the image rather than send a malformed request.
    """
    if not url or not url.startswith("data:"):
        return None
    header, _, payload = url.partition(",")
    if not payload or ";base64" not in header:
        return None
    media_type = header[len("data:"):].split(";", 1)[0].strip()
    if not media_type.startswith("image/"):
        return None
    try:
        base64.b64decode(payload, validate=True)
    except (binascii.Error, ValueError):
        return None
    return media_type, payload


class BaseProvider:
    """One vendor. Subclasses own their transport, payload shape and errors."""

    name = "base"

    def complete(
        self,
        turns: Iterable[ChatTurn],
        *,
        system_prompt: str = "",
        max_tokens: int | None = None,
        profile: str = "standard",
    ) -> AIResponse:
        """One reply. ``profile`` is :mod:`AI.services.question_profile`'s
        reading of the question; a vendor with nothing to tune may ignore it."""
        raise NotImplementedError

    def stream(
        self,
        turns: Iterable[ChatTurn],
        *,
        system_prompt: str = "",
        max_tokens: int | None = None,
        profile: str = "standard",
        on_delta: Callable[[str], Any] | None = None,
        cancel: CancelToken | None = None,
    ) -> AIResponse:
        """One reply, handed to ``on_delta`` piece by piece as it is written.

        Returns the same :class:`AIResponse` as :meth:`complete`. When
        ``cancel`` fires, the reply so far comes back with ``finish_reason``
        :data:`STOPPED`. A vendor without streaming answers in one piece.
        """
        result = self.complete(turns, system_prompt=system_prompt, max_tokens=max_tokens, profile=profile)
        if on_delta is not None and result.content:
            on_delta(result.content)
        return result

    def effective_output_budget(self, requested: int | None = None) -> int:
        """The output limit this vendor would really use for ``requested``.

        Asking is not the same as getting: a reasoning model raises a budget
        that is too small to think in, so a caller that must not overspend has
        to know what the request will actually be allowed to cost *before* it
        makes it. Overridden by any vendor that adjusts the number.
        """
        return int(requested or self.max_output_tokens)

    @property
    def is_configured(self) -> bool:
        """True when this vendor has credentials. Checked before every call."""
        return False

    @property
    def supports_vision(self) -> bool:
        return False
