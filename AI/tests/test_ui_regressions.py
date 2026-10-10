"""Regressions a student or administrator actually hit in the browser.

Each test here names the bug it guards against. They check the rendered pages
and stylesheets, because every one of these was invisible to the server.
"""
from __future__ import annotations

import re
from pathlib import Path

from django.conf import settings
from django.test import TestCase

from AI.models import AccountStatus, User
from AI.services import chat_service, settings_service
from AI.tests.helpers import fund

PASSWORD = "Sup3rSecret!x"
TEMPLATES = Path(settings.BASE_DIR) / "templates"


def duplicate_ids(html: str) -> list[str]:
    """ids that appear more than once in a page, ignoring the page's own scripts."""
    markup = re.sub(r"<script\b.*?</script>", "", html, flags=re.S)
    ids = re.findall(r'\sid="([^"{}]+)"', markup)
    return sorted({i for i in ids if ids.count(i) > 1})


class TemplateCommentTests(TestCase):
    def test_no_one_line_comment_spans_lines(self):
        """Django's {# #} only works on one line; split over two, it prints on the page."""
        broken = []
        for path in TEMPLATES.rglob("*.html"):
            for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                for start in (m.start() for m in re.finditer(r"\{#", line)):
                    if "#}" not in line[start:]:
                        broken.append(f"{path.relative_to(TEMPLATES)}:{number}")
        self.assertEqual(broken, [], "use {% comment %} for a note longer than one line")


class Fixture(TestCase):
    def setUp(self):
        settings_service.bootstrap()
        self.student = User.objects.create_user(
            username="ann", email="ann@test.local", password=PASSWORD,
            is_verified=True, status=AccountStatus.ACTIVE,
        )
        fund(self.student, 10_000)
        self.admin = User.objects.create_superuser(username="root", email="root@test.local", password=PASSWORD)


class DuplicateIdTests(Fixture):
    """Two elements shared id="paletteBtn", so the colour picker's button was
    never the one its script found, and the colour menu stopped opening."""

    def test_the_chat_and_its_panels_have_unique_ids(self):
        self.client.force_login(self.student)
        conversation = chat_service.create_conversation(self.student)
        for url in ("/chat/", f"/chat/{conversation.pk}/", "/app/usage/", "/app/pricing/", "/app/settings/",
                    "/app/prompts/", "/app/saved/", "/app/archived/", "/app/models/", "/app/help/"):
            with self.subTest(url=url):
                self.assertEqual(duplicate_ids(self.client.get(url).content.decode()), [])

    def test_console_pages_have_unique_ids(self):
        self.client.force_login(self.admin)
        for url in ("/admin/", "/admin/payments/", "/admin/promos/new/", "/admin/accounts/", "/admin/quality/"):
            with self.subTest(url=url):
                self.assertEqual(duplicate_ids(self.client.get(url).content.decode()), [])

    def test_the_colour_picker_and_the_search_button_are_different_elements(self):
        self.client.force_login(self.student)
        body = self.client.get("/chat/").content.decode()
        self.assertEqual(body.count('id="paletteBtn"'), 1, "the colour picker's button")
        self.assertEqual(body.count('id="cmdkBtn"'), 1, "the search button")


class ComposerTests(Fixture):
    """Opening the app on a panel sized the hidden composer at 0px, and going
    back to Chats left a sliver of a message box."""

    def test_resize_does_not_measure_a_hidden_box(self):
        source = (TEMPLATES / "chat" / "chat.html").read_text(encoding="utf-8")
        body = source[source.index("function resize()"):source.index("function updateCount()")]
        self.assertIn("if (!input.offsetParent || !input.clientWidth)", body)
        show_chat = source[source.index("function showChat()"):source.index("function paintPanel(")]
        self.assertIn("resize();", show_chat, "returning to the chat must measure the box again")


class DarkModeTests(TestCase):
    """The console and public pages switch theme with a class, but the browser
    was never told — so native dropdown lists stayed white under white text."""

    def test_every_theme_tells_the_browser_when_it_is_dark(self):
        for name in ("base/console_design.html", "base/design.html"):
            with self.subTest(template=name):
                css = (TEMPLATES / name).read_text(encoding="utf-8")
                self.assertIn("html.dark { color-scheme: dark; }", css)
                self.assertIn("html.dark select option", css)
        student_css = (TEMPLATES / "app" / "_design.html").read_text(encoding="utf-8")
        self.assertIn("color-scheme: dark;", student_css)

    def test_every_css_variable_the_student_app_uses_exists(self):
        """``var(--ink-1)`` was never defined, so eight rules — notification
        titles among them — inherited whatever colour sat around them."""
        sources = [p.read_text(encoding="utf-8") for folder in ("app", "chat", "components")
                   for p in (TEMPLATES / folder).rglob("*.html")]
        text = "\n".join(sources)
        used = set(re.findall(r"var\((--[a-z0-9-]+)", text))
        defined = set(re.findall(r"(--[a-z0-9-]+)\s*:", text))
        defined |= set(re.findall(r"setProperty\('(--[a-z0-9-]+)", text))
        self.assertEqual(sorted(used - defined), [])

    def test_notification_cards_take_their_tint_from_the_theme(self):
        """The view used to send pastel-on-white tints, which stayed pastel in
        dark mode under light text. Only the tone's hue comes from the server."""
        from AI.views.panels import NOTIFICATION_TONES
        for level, tone in NOTIFICATION_TONES.items():
            with self.subTest(level=level):
                self.assertNotIn("tint", tone)
                self.assertNotIn("soft", tone)
        css = (TEMPLATES / "app" / "_design.html").read_text(encoding="utf-8")
        self.assertIn("color-mix(in srgb, var(--n-to) 8%, var(--surface))", css)
        self.assertNotIn("var(--n-tint)", css)


class PhoneLayoutTests(TestCase):
    """On a phone the answer actions and the model menu ran off the screen."""

    def test_answer_actions_become_icons_on_a_phone(self):
        css = (TEMPLATES / "app" / "_design.html").read_text(encoding="utf-8")
        self.assertIn(".msg-ai .msg-actions { flex-wrap: wrap;", css)
        self.assertIn(".msg-ai .msg-actions .act { font-size: 0;", css)

    def test_menus_never_outgrow_the_screen(self):
        css = (TEMPLATES / "app" / "_design.html").read_text(encoding="utf-8")
        self.assertIn(".menu { max-width: min(420px, calc(100vw - 16px)); }", css)
