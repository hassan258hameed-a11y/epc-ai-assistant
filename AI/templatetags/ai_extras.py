"""Template helpers used across the product."""
from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from django import template
from django.utils import timezone
from django.utils.safestring import mark_safe

from AI.utils.pagination import querystring

register = template.Library()


@register.simple_tag
def qs(request, **kwargs):
    """Rebuild the querystring with overrides: ``{% qs request page=3 %}``."""
    return querystring(request, **kwargs)


@register.filter
def intcomma(value):
    """Thousands separators without pulling in humanize at every call site."""
    try:
        return f"{int(value):,}"
    except (TypeError, ValueError):
        return value


@register.filter
def money(value, symbol="$"):
    """``1234.5`` -> ``$1,234.50``.

    Exists because a template cannot format a number and prefix a symbol in one
    filter chain: ``"$"|add:total|floatformat:2`` concatenates first, then asks
    floatformat to parse "$1234.5", which it cannot — so the whole figure came
    out as an empty string. Formatting and labelling belong in one step.
    """
    try:
        amount = Decimal(str(value if value not in (None, "") else 0))
    except (InvalidOperation, ValueError, TypeError):
        return f"{symbol}0.00"

    # Round first, then decide the sign. A loss of one third of a cent is not
    # "-$0.00" — at the precision being displayed it is zero, and printing a
    # negative zero makes a reader look for money that is not there.
    amount = amount.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    if amount == 0:
        return f"{symbol}0.00"
    # The sign belongs outside the symbol: "-$5.00", not "$-5.00".
    sign = "-" if amount < 0 else ""
    return f"{sign}{symbol}{abs(amount):,.2f}"


@register.filter
def compact_number(value):
    """1_240_000 -> '1.2M' — used on dense metric tiles."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return value
    for threshold, suffix in ((1_000_000_000, "B"), (1_000_000, "M"), (1_000, "K")):
        if abs(number) >= threshold:
            trimmed = number / threshold
            return f"{trimmed:.1f}".rstrip("0").rstrip(".") + suffix
    return f"{int(number):,}"


@register.filter
def percent_of(value, total):
    try:
        total = float(total)
        if total <= 0:
            return 0
        return max(0, min(100, round(float(value) / total * 100)))
    except (TypeError, ValueError, ZeroDivisionError):
        return 0


@register.filter
def tone_classes(tone):
    """Map a semantic tone to badge classes."""
    return {
        "success": "badge-success",
        "warning": "badge-warning",
        "danger": "badge-danger",
        "info": "badge-info",
        "brand": "badge-brand",
    }.get(tone, "badge-neutral")


@register.filter
def initials(user):
    return getattr(user, "initials", "?")


@register.filter
def timeago(value):
    """Short relative time: 'just now', '4m', '3h', '2d', then a date."""
    if not value:
        return ""
    now = timezone.now()
    try:
        delta = now - value
    except TypeError:
        return value
    seconds = delta.total_seconds()
    if seconds < 45:
        return "just now"
    if seconds < 3600:
        return f"{int(seconds // 60)}m ago"
    if seconds < 86400:
        return f"{int(seconds // 3600)}h ago"
    if seconds < 604800:
        return f"{int(seconds // 86400)}d ago"
    return timezone.localtime(value).strftime("%d %b %Y")


@register.filter
def stringify_all(values):
    """``[1, 2]`` -> ``["1", "2"]`` so a template `in` test can match.

    A bound multiple-choice field holds primary keys as integers while the
    option values render as strings, so comparing them directly never matches
    and every option comes back unselected.
    """
    if values is None:
        return []
    if not isinstance(values, (list, tuple, set)):
        values = [values]
    return [str(getattr(value, "pk", value)) for value in values]


@register.filter
def get_item(mapping, key):
    try:
        return mapping.get(key)
    except AttributeError:
        return None


@register.simple_tag
def sparkline(values, width=120, height=32, stroke="currentColor"):
    """Inline SVG sparkline — no chart library, no external request."""
    points = [float(v or 0) for v in (values or [])]
    if len(points) < 2:
        return mark_safe(f'<svg width="{width}" height="{height}" aria-hidden="true"></svg>')

    top, bottom = max(points), min(points)
    span = (top - bottom) or 1
    step = width / (len(points) - 1)
    coords = [
        f"{index * step:.1f},{height - 3 - ((value - bottom) / span) * (height - 6):.1f}"
        for index, value in enumerate(points)
    ]
    path = "M" + " L".join(coords)
    area = f"{path} L{width:.1f},{height} L0,{height} Z"
    return mark_safe(
        f'<svg width="{width}" height="{height}" viewBox="0 0 {width} {height}" fill="none" aria-hidden="true">'
        f'<path d="{area}" fill="{stroke}" fill-opacity=".10"/>'
        f'<path d="{path}" stroke="{stroke}" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/>'
        f"</svg>"
    )


@register.filter
def widget_type(field):
    """Widget class name, e.g. 'TextInput' / 'Textarea' / 'Select'."""
    try:
        return field.field.widget.__class__.__name__
    except AttributeError:
        return ""


@register.filter
def field_has_error(field):
    return bool(getattr(field, "errors", None))


@register.filter
def add_class(field, css):
    """Append CSS classes to a bound form field widget."""
    existing = field.field.widget.attrs.get("class", "")
    return field.as_widget(attrs={"class": f"{existing} {css}".strip()})


@register.filter
def plus_prefix(value):
    """Mark a figure as a gain: ``9`` -> ``+9``, ``$38.00`` -> ``+$38.00``.

    Zero is returned untouched. "+0" claims a rise that did not happen, and on
    a console where several tiles show nothing yet on a fresh install, a row of
    green "+0" pills would be the first thing an administrator saw.

    Anything already carrying a sign is left alone, so a negative stays
    negative rather than becoming "+-3".
    """
    text = str(value).strip()
    if not text or text[0] in "+-":
        return text
    if not any(ch.isdigit() and ch != "0" for ch in text):
        return text
    return "+" + text


@register.filter
def model_tone(model):
    """The colour a model wears wherever it appears.

    One map, in the view module, read from templates through here — so a card,
    a table column, a usage bar and a ledger pill for the same model can never
    drift apart. Anything unrecognised gets the neutral fallback rather than
    no colour at all.
    """
    from AI.views.console_ai import tone_for

    if model is None:
        from AI.views.console_ai import FALLBACK_TONE

        return FALLBACK_TONE
    return tone_for(model)


#: Marks for people. Deliberately a different set from the model colours, so a
#: student's disc is never mistaken for a model's tile on the same screen.
_AVATAR_TONES = (
    ("#60A5FA", "#2563EB"),
    ("#F472B6", "#DB2777"),
    ("#8B84F5", "#4F46E5"),
    ("#67D3F8", "#0EA5E9"),
    ("#A78BFA", "#7C3AED"),
    ("#F87171", "#DC2626"),
)


@register.filter
def avatar_tone(person):
    """A stable colour for a person's initials disc.

    Derived from the username rather than from their position in the list, so
    the same student is the same colour on every page and does not change hue
    when somebody above them is deleted. `sum(ord(...))` rather than `hash()`
    because Python salts string hashing per process — the colour would change
    on every restart.
    """
    key = str(getattr(person, "username", "") or getattr(person, "pk", "") or "")
    start, end = _AVATAR_TONES[sum(ord(ch) for ch in key) % len(_AVATAR_TONES)]
    return {"from": start, "to": end}
