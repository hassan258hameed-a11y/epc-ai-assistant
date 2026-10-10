"""The public pages and the sign-in pages, as a visitor's browser sees them.

Each test names what went wrong in a real browser: sign-in pages that stayed
white in dark mode, a theme choice that did not carry from the app, a header
that wrapped onto two rows on a tablet, labels too faint to read, controls
too small for a thumb and decorative loops that never stopped.
"""
from __future__ import annotations

import re
from pathlib import Path

from django.conf import settings
from django.test import TestCase

from AI.models import AccountStatus, Conversation, ConversationShare, Message, User
from AI.services import settings_service
from AI.tests.test_ui_regressions import duplicate_ids

TEMPLATES = Path(settings.BASE_DIR) / "templates"
PASSWORD = "Sup3rSecret!x"

PUBLIC_URLS = ("/", "/plans/", "/privacy/", "/terms/", "/refund-policy/")
SIGN_IN_URLS = ("/login/", "/signup/", "/forgot-password/", "/admin/login/")


def read(name: str) -> str:
    return (TEMPLATES / name).read_text(encoding="utf-8")


def style_of(name: str) -> str:
    """Every <style> block of a template, comments removed."""
    blocks = re.findall(r"<style[^>]*>(.*?)</style>", read(name), flags=re.S)
    return re.sub(r"/\*.*?\*/", "", "\n".join(blocks), flags=re.S)


def tokens(block: str) -> dict[str, str]:
    return dict(re.findall(r"(--[a-z0-9-]+)\s*:\s*([^;]+);", block))


def rule_block(css: str, selector: str) -> str:
    match = re.search(re.escape(selector) + r"\s*\{(.*?)\n\}", css, flags=re.S)
    assert match, f"{selector} block is missing"
    return match.group(1)


def contrast(fg: str, bg: str) -> float:
    def lum(hex_colour: str) -> float:
        h = hex_colour.strip().lstrip("#")
        channels = [int(h[i:i + 2], 16) / 255 for i in (0, 2, 4)]
        lin = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
        return 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2]

    a, b = lum(fg), lum(bg)
    return (max(a, b) + 0.05) / (min(a, b) + 0.05)


class Fixture(TestCase):
    def setUp(self):
        settings_service.bootstrap()


class ThemeCarriesAcrossTheProductTests(Fixture):
    """A student who chose dark in the app met a white sign-in page: the app
    stores 'lumen-theme', these pages only ever read 'epcai-theme'."""

    def test_the_bootstrap_reads_the_apps_choice_first(self):
        boot = read("public/_theme_boot.html")
        lumen = boot.index("localStorage.getItem('lumen-theme')")
        legacy = boot.index("localStorage.getItem('epcai-theme')")
        system = boot.index("prefers-color-scheme: dark")
        self.assertLess(lumen, legacy, "the app's key must win over the old public one")
        self.assertIn("media.matches", boot[legacy:], "with nothing saved, the system decides")
        self.assertTrue(system)

    def test_a_toggle_writes_both_keys(self):
        boot = read("public/_theme_boot.html")
        self.assertIn("localStorage.setItem('lumen-theme', theme)", boot)
        self.assertIn("localStorage.setItem('epcai-theme', theme)", boot)
        for name in ("base/public.html", "auth/_auth_base.html", "auth/_epc_auth.html"):
            with self.subTest(template=name):
                source = read(name)
                self.assertIn("window.epcTheme.toggle()", source)
                self.assertNotIn("setItem('epcai-theme'", source, "toggles go through the shared bootstrap")

    def test_every_page_decides_the_theme_in_head_before_paint(self):
        for url in PUBLIC_URLS + SIGN_IN_URLS:
            with self.subTest(url=url):
                html = self.client.get(url).content.decode()
                head = html[:html.index("<body")]
                self.assertIn("localStorage.getItem('lumen-theme')", head)
                self.assertIn("setAttribute('data-theme'", head)
                # Before the first stylesheet, so nothing is drawn in the wrong theme.
                self.assertLess(head.index("lumen-theme"), head.index("<style"))

    def test_every_sign_in_page_offers_the_switch(self):
        for url in SIGN_IN_URLS:
            with self.subTest(url=url):
                self.assertContains(self.client.get(url), "data-theme-toggle")


class SignInSignUpSlideTests(TestCase):
    """Pressing "Sign up" focused the first field mid-slide; the browser
    scrolled the clipped rail to reach it and the form landed off to the left."""

    source = read("auth/_epc_auth.html")

    def test_the_rail_cannot_be_scrolled(self):
        self.assertIn("overflow: clip", rule_block(style_of("auth/_epc_auth.html"), ".rail"))
        self.assertIn("rail.scrollLeft = 0", self.source)

    def test_switching_panels_focuses_without_scrolling(self):
        self.assertIn("first.focus({ preventScroll: true })", self.source)
        self.assertNotIn("first.focus();", self.source)


class SignInCardDarkThemeTests(TestCase):
    """/login/ and /signup/ were committed to a single light theme."""

    css = style_of("auth/_epc_auth.html")

    def light(self) -> dict[str, str]:
        return tokens(rule_block(self.css, ":root"))

    def dark(self) -> dict[str, str]:
        return tokens(rule_block(self.css, ':root[data-theme="dark"]'))

    def test_the_browser_is_told_both_themes_exist(self):
        source = read("auth/_epc_auth.html")
        self.assertIn('<meta name="color-scheme" content="light dark">', source)
        self.assertIn("color-scheme: dark;", rule_block(self.css, ':root[data-theme="dark"]'))
        self.assertIn("color-scheme: light;", rule_block(self.css, ":root"))
        self.assertIn('{% include "public/_theme_boot.html" %}', source)

    def test_dark_redefines_every_colour_token(self):
        light, dark = self.light(), self.dark()
        self.assertEqual(sorted(set(light) - set(dark)), [], "a token left light in the dark theme")

    def test_a_browser_without_scripts_still_follows_a_dark_system(self):
        fallback = re.search(
            r'@media \(prefers-color-scheme: dark\)\s*\{\s*:root:not\(\[data-theme="light"\]\)\s*\{(.*?)\n  \}',
            self.css, flags=re.S,
        )
        self.assertIsNotNone(fallback)
        self.assertEqual(tokens(fallback.group(1)), self.dark(), "the two dark blocks have drifted apart")

    def test_every_variable_the_card_uses_is_defined(self):
        used = set()
        for name in ("auth/_epc_auth.html", "auth/_auth_card.html", "auth/login.html", "auth/signup.html"):
            used |= set(re.findall(r"var\((--[a-z0-9-]+)", read(name)))
        defined = set(self.light()) | {"--rot"}
        self.assertEqual(sorted(used - defined), [])

    def test_surfaces_take_their_colour_from_the_theme(self):
        """No light-only literal left on anything the card is built from."""
        for selector in (".stage {", ".float {", ".card {", ".seg {", ".seg-pill {", ".inp {", ".box {",
                         ".btn-ghost {", ".aside {", ".eye:hover {", ".ov {", ".ov-disc {", ".f-expr {",
                         ".f-track {", ".segs i {", ".theme-btn {"):
            with self.subTest(selector=selector):
                body = re.search(r"(?m)^" + re.escape(selector) + r"(.*?)\}", self.css, flags=re.S).group(1)
                for declaration in re.findall(r"(?:^|[;{\s])(background|color|border|background-color)\s*:\s*([^;]+);", body):
                    self.assertNotRegex(
                        declaration[1], r"#[0-9A-Fa-f]{3,6}\b|rgba?\(",
                        f"{selector} {declaration[0]} is a fixed colour: {declaration[1]}",
                    )

    def test_quiet_text_is_readable_in_both_themes(self):
        """brand-sub, the unselected tab, the eyebrow, "New here?" and the
        footer read 2.3-2.6:1 on the light card."""
        for theme, t in (("light", self.light()), ("dark", self.dark())):
            with self.subTest(theme=theme):
                self.assertGreaterEqual(contrast(t["--tx3"], t["--sf"]), 4.5)
                self.assertGreaterEqual(contrast(t["--tx2"], t["--seg"]), 4.5)
                self.assertGreaterEqual(contrast(t["--tx2"], t["--sf"]), 4.5)
                self.assertGreaterEqual(contrast(t["--ac-tx"], t["--sf"]), 4.5)
                self.assertGreaterEqual(contrast(t["--danger-tx"], t["--sf"]), 4.5)
                self.assertGreaterEqual(contrast("#FFFFFF", t["--ac"]), 4.5, "the primary button's label")
        for selector in (".brand-sub {", ".seg-btn {", ".foot {"):
            with self.subTest(selector=selector):
                body = self.css[self.css.index(selector):]
                body = body[:body.index("}")]
                self.assertNotIn("var(--tx3)", body)

    def test_the_card_highlight_ends_invisible(self):
        """A sweep resting at the card's edge showed as a pale wedge in dark."""
        sheen = re.search(r"@keyframes sheen\s*\{(.*?)\}\s*\}", self.css, flags=re.S).group(1)
        self.assertTrue(sheen.rstrip().endswith("opacity: 0;"), sheen)


class SplitSignInPagesTests(Fixture):
    """Forgot/reset password, the code screen and the console sign-in."""

    def test_the_story_panel_is_the_same_in_both_themes(self):
        base = read("auth/_auth_base.html")
        self.assertIn('class="auth-story relative hidden w-1/2', base)
        self.assertIn("html.dark body.auth-split { background: #0B0E14; }", base)
        self.assertNotIn("bg-accent-500/20 text-accent-300", base + read("auth/admin_login.html"))

    def test_the_code_and_reset_screens_render(self):
        student = User.objects.create_user(
            username="ann", email="ann@test.local", password=PASSWORD,
            is_verified=True, status=AccountStatus.ACTIVE,
        )
        session = self.client.session
        session["pending_user_id"] = student.pk
        session["pending_otp_purpose"] = "signup"
        session.save()
        otp = self.client.get("/otp/")
        self.assertEqual(otp.status_code, 200)
        self.assertContains(otp, "data-theme-toggle")
        self.assertEqual(duplicate_ids(otp.content.decode()), [])

        session = self.client.session
        session["reset_user_id"] = student.pk
        session.save()
        reset = self.client.get("/reset-password/")
        self.assertEqual(reset.status_code, 200)
        self.assertContains(reset, "localStorage.getItem('lumen-theme')")


class PublicHeaderTests(Fixture):
    """At 820px "Get started" dropped to a second row under the nav."""

    def test_the_header_never_wraps_and_folds_into_a_menu(self):
        css = style_of("base/public.html")
        header = re.search(r"\.lp-header \{([^}]*)\}", css).group(1)
        self.assertNotIn("flex-wrap: wrap", header)
        self.assertIn("@media (max-width: 1099px)", css)
        self.assertIn("@media (max-width: 639px)", css)
        html = self.client.get("/").content.decode()
        self.assertRegex(html, r'<button[^>]*class="lp-menu-btn"[^>]*aria-expanded="false"[^>]*aria-controls="lpMenu"')
        self.assertIn('<div class="lp-menu" id="lpMenu">', html)

    def test_the_theme_button_draws_one_icon(self):
        """`.lp svg { display: block }` outranks `.hidden`, so the moon and the
        sun were drawn on top of each other in the light theme."""
        source = read("base/public.html")
        self.assertNotIn('class="hidden dark:block"', source)
        self.assertIn(".lp .lp-sun, .lp .lp-to-light { display: none; }", source)
        self.assertIn(".dark .lp .lp-sun { display: block; }", source)

    def test_the_sticky_header_is_not_blurred(self):
        header = re.search(r"\.lp-header \{([^}]*)\}", style_of("base/public.html")).group(1)
        self.assertNotIn("backdrop-filter", header)

    def test_public_and_sign_in_pages_have_unique_ids(self):
        for url in PUBLIC_URLS + SIGN_IN_URLS:
            with self.subTest(url=url):
                self.assertEqual(duplicate_ids(self.client.get(url).content.decode()), [])


class ContrastTests(TestCase):
    def test_the_active_period_button_has_a_background_of_its_own(self):
        """The white "Regular" label sat on a sibling pill, so to anything
        reading the element itself it was white on white."""
        css = style_of("public/home.html")
        active = re.search(r"\.lp-seg button\.is-on \{([^}]*)\}", css).group(1)
        self.assertIn("background-color: #1D4ED8", active)

    def test_white_labels_sit_on_the_deeper_blue(self):
        """#3B82F6 carries white at 3.7:1; every filled control starts at #2563EB."""
        for name in ("public/home.html", "base/public.html"):
            with self.subTest(template=name):
                self.assertNotIn("linear-gradient(135deg, #3B82F6, #1D4ED8)", read(name))
        self.assertGreaterEqual(contrast("#FFFFFF", "#2563EB"), 4.5)

    def test_dark_labels_use_light_tints(self):
        css = style_of("public/home.html")
        self.assertIn(".dark .lp-page .lp-eyebrow { background: rgba(96,165,250,.14); color: #93C5FD; }", css)
        self.assertIn("--blue-ink: #93C5FD;", style_of("base/public.html"))
        self.assertIn(".dark .lp-page .lp-plan.is-rec .lp-plan-cta", css, "the recommended plan keeps its fill in dark")

    def test_quiet_public_text_is_readable(self):
        light = tokens(re.search(r"\.lp \{([^}]*)\}", style_of("base/public.html")).group(1))
        self.assertGreaterEqual(contrast(light["--soft"], "#FFFFFF"), 4.5)
        self.assertGreaterEqual(contrast(light["--soft"], "#F4F7FC"), 4.5)

    def test_keyboard_focus_is_an_outline(self):
        """A focus ring is a box-shadow, and buttons with shadows painted over it."""
        design = read("base/design.html")
        self.assertIn(":focus-visible { outline: 2px solid", design)
        self.assertNotIn(":focus-visible { @apply outline-none ring-2", design)
        self.assertIn(".lp :focus-visible { outline: 2px solid var(--focus);", read("base/public.html"))
        self.assertIn(":focus-visible { outline: 2px solid var(--focus);", read("auth/_epc_auth.html"))


class TouchTargetTests(TestCase):
    """Footer links were 20px tall, "Choose" 30px, the theme toggle 38px."""

    def test_touch_screens_get_44px_controls(self):
        for name, needles in (
            ("base/design.html", (".btn-sm { min-height: 44px; }", ".btn-icon { min-width: 44px; min-height: 44px; }")),
            ("base/public.html", (".lp-icon-btn, .lp-menu-btn { width: 44px; height: 44px; }",
                                  ".lp-foot-links a { display: flex; align-items: center; min-height: 44px; }")),
            ("auth/_epc_auth.html", (".theme-btn { width: 44px; height: 44px; }", ".inp { height: 48px; font-size: 16px; }")),
            ("public/_legal_base.html", ("@media (pointer: coarse) { .prose-epc a { padding-block: 12px; } }",)),
        ):
            source = read(name)
            self.assertIn("@media (pointer: coarse)", source)
            for needle in needles:
                with self.subTest(template=name, rule=needle):
                    self.assertIn(needle, source)


class MotionTests(TestCase):
    """Decorative animations play a few times, then rest. `infinite` is for
    things that mean something: a spinner, a typing indicator, the marquee."""

    ALLOWED = {"lpMarquee", "lpMarqueeR", "lpDotB", "lpBlink"}

    def test_no_endless_decorative_loop(self):
        names = ["base/public.html", "base/design.html"] + [
            p.relative_to(TEMPLATES).as_posix() for folder in ("auth", "public")
            for p in sorted((TEMPLATES / folder).glob("*.html"))
        ]
        offenders = []
        for name in names:
            source = read(name)
            # The Tailwind config's named utilities (animate-spin & co) are
            # opt-in classes, not loops that run on these pages by themselves.
            source = re.sub(r"tailwind\.config = \{.*?\n  \};", "", source, flags=re.S)
            for declaration in re.findall(r"animation\s*:\s*([^;\"}]+)", source):
                if "infinite" not in declaration:
                    continue
                name_used = declaration.split()[0]
                if name_used not in self.ALLOWED:
                    offenders.append(f"{name}: {declaration.strip()}")
        self.assertEqual(offenders, [])

    def test_reduced_motion_can_be_overridden_on_the_sign_in_card(self):
        css = style_of("auth/_epc_auth.html")
        block = css[css.index("@media (prefers-reduced-motion: reduce)"):]
        self.assertIn(':root:not([data-motion="full"]) .aur', block)
        self.assertIn(':root:not([data-motion="full"]) .ov.is-open .ov-shackle', block)

    def test_the_demo_does_not_type_for_someone_who_asked_for_stillness(self):
        source = read("public/home.html")
        self.assertIn("(prefers-reduced-motion: reduce)", source)
        self.assertIn("if (calm) { finish(); return; }", source)


class SharedChatLayoutTests(Fixture):
    """A long code line or an unbroken word widened the shared page to 1,240px
    on a 390px phone."""

    def test_long_content_stays_inside_the_transcript(self):
        css = style_of("public/shared_chat.html")
        msg = re.search(r"\.shared-msg \{([^}]*)\}", css).group(1)
        self.assertIn("overflow-wrap: anywhere", msg)
        self.assertIn("min-width: 0", msg)
        pre = re.search(r"\.shared-md pre \{([^}]*)\}", css).group(1)
        self.assertIn("overflow-x-auto", pre)
        self.assertIn("max-width: 100%", pre)
        table = re.search(r"\.shared-md table \{([^}]*)\}", css).group(1)
        self.assertIn("overflow-x: auto", table)

    def test_a_shared_conversation_renders(self):
        owner = User.objects.create_user(
            username="ben", email="ben@test.local", password=PASSWORD,
            is_verified=True, status=AccountStatus.ACTIVE,
        )
        conversation = Conversation.objects.create(user=owner, title="Long lines")
        Message.objects.create(conversation=conversation, role="user", content="x" * 400)
        last = Message.objects.create(conversation=conversation, role="assistant", content="```\n" + "y" * 400 + "\n```")
        link = ConversationShare.objects.create(conversation=conversation, last_message_id=last.pk)
        response = self.client.get(f"/share/{link.token}/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(duplicate_ids(response.content.decode()), [])
        self.assertContains(response, "localStorage.getItem('lumen-theme')")
