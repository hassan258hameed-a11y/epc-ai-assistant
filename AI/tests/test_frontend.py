"""Front-end guarantees that Python alone cannot check.

The assistant's replies are converted to HTML in the browser, so the renderer in
``templates/chat/chat.html`` is the boundary that stops a malicious model
response becoming script execution. These tests extract that function and run it
under Node when Node is available.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from django.conf import settings
from django.test import SimpleTestCase

CHAT_TEMPLATE = Path(settings.BASE_DIR) / "templates" / "chat" / "chat.html"
#: The stylesheet is shared with the few flows that keep a page of their own,
#: so it lives in its own partial. Rules are asserted against that; markup and
#: behaviour against the template above.
DESIGN_TEMPLATE = Path(settings.BASE_DIR) / "templates" / "app" / "_design.html"


def chat_source() -> str:
    """The chat screen as the browser receives it — markup plus its stylesheet."""
    return CHAT_TEMPLATE.read_text(encoding="utf-8") + DESIGN_TEMPLATE.read_text(encoding="utf-8")
NODE = shutil.which("node")

# The only tags the renderer is allowed to emit. Anything else in its output
# would mean markup from the model survived escaping.
SAFE_TAGS = re.compile(
    r"</?(?:p|br|h[1-3]|ul|ol|li|code|pre|em|strong|blockquote|hr|table|thead|tbody"
    r"|tr|th|td|div|span|button|svg|use|a)(?:\s[^>]*)?>"
)

HARNESS = """
const { renderMarkdown } = require('./renderer.js');
const cases = JSON.parse(process.argv[2]);
const results = {};
for (const [name, input] of Object.entries(cases)) {
  try { results[name] = renderMarkdown(input); }
  catch (e) { results[name] = 'ERROR: ' + e.message; }
}
process.stdout.write(JSON.stringify(results));
"""


def strip_comments(source: str) -> str:
    """Source with /* … */ and // … removed, so a comment cannot be a finding."""
    source = re.sub(r"/\*.*?\*/", "", source, flags=re.DOTALL)
    source = re.sub(r"(?m)^\s*//.*$", "", source)
    return re.sub(r"(?m)\s//.*$", "", source)


def extract_renderer() -> str:
    """Pull the markdown functions out of the chat template."""
    source = CHAT_TEMPLATE.read_text(encoding="utf-8")
    start = source.index("  function esc(text)")
    end = source.index("  function hydrate(root)")
    code = source[start:end]

    # Inline {% include %}s the way Django would, minus their comment blocks.
    def include(match):
        text = (CHAT_TEMPLATE.parents[1] / match.group(1)).read_text(encoding="utf-8")
        return re.sub(r"\{% comment %\}.*?\{% endcomment %\}", "", text, flags=re.S)

    code = re.sub(r'\{% include "([^"]+)" %\}', include, code)
    return code + "\nmodule.exports = { renderMarkdown: markdown };\n"


@unittest.skipIf(NODE is None, "Node.js is not installed; skipping renderer tests")
class MarkdownRendererTests(SimpleTestCase):
    """Escape-then-convert: nothing from the model is ever inserted as raw HTML."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._tmp = tempfile.TemporaryDirectory()
        directory = Path(cls._tmp.name)
        (directory / "renderer.js").write_text(extract_renderer(), encoding="utf-8")
        (directory / "harness.js").write_text(HARNESS, encoding="utf-8")
        cls._dir = directory

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()
        super().tearDownClass()

    @classmethod
    def render(cls, cases: dict[str, str]) -> dict[str, str]:
        result = subprocess.run(
            [NODE, "harness.js", json.dumps(cases)],
            # Node writes UTF-8; without this Windows decodes with its ANSI code
            # page and every ±, ² and √ the renderer produces arrives mangled.
            cwd=cls._dir, capture_output=True, text=True, encoding="utf-8", timeout=30,
        )
        if result.returncode != 0:
            raise AssertionError(f"Renderer harness failed: {result.stderr}")
        return json.loads(result.stdout)

    # -- security ---------------------------------------------------------
    def test_html_in_a_reply_is_escaped_not_executed(self):
        out = self.render(
            {
                "script": '<script>alert("xss")</script>',
                "img": "<img src=x onerror=alert(1)>",
                "iframe": '<iframe src="evil"></iframe>',
                "handler": '<div onclick="steal()">click</div>',
                "in_code": "```js\n<script>bad()</script>\n```",
                "in_inline": "`<b>literal</b>`",
                "in_table": "| A |\n|---|\n| <script>x</script> |",
                "in_heading": "## <script>x</script>",
            }
        )

        for name, html in out.items():
            for tag in ("<script", "<iframe", "<img", "<object", "<embed", "<style", "<form"):
                self.assertNotIn(tag, html, f"{name} produced a live {tag} element")

            # Once the renderer's own tags are removed, no markup may remain.
            leftover = SAFE_TAGS.sub("", html)
            self.assertNotIn("<", leftover, f"{name} leaked markup: {leftover}")

        # The payload is still visible to the student, just as inert text.
        self.assertIn("&lt;script&gt;", out["script"])
        self.assertIn("&lt;img", out["img"])
        self.assertIn("&lt;b&gt;", out["in_inline"])

    def test_javascript_urls_are_not_linkified(self):
        out = self.render(
            {
                "js": "[click](javascript:alert(1))",
                "data": "[click](data:text/html,<script>x</script>)",
                "https": "[safe](https://example.com)",
            }
        )
        self.assertNotIn('href="javascript:', out["js"])
        self.assertNotIn("<a ", out["data"])
        self.assertIn('href="https://example.com"', out["https"])
        self.assertIn('rel="noopener noreferrer"', out["https"])

    # -- formatting -------------------------------------------------------
    def test_common_markdown_renders(self):
        out = self.render(
            {
                "h2": "## Heading",
                "bold": "**strong**",
                "italic": "some *emphasis* here",
                "inline_code": "call `print()` now",
                "ul": "- one\n- two",
                "ol": "1. first\n2. second",
                "quote": "> quoted text",
                "rule": "---",
                "para": "Just a sentence.",
            }
        )
        self.assertIn("<h2>Heading</h2>", out["h2"])
        self.assertIn("<strong>strong</strong>", out["bold"])
        self.assertIn("<em>emphasis</em>", out["italic"])
        self.assertIn("<code>print()</code>", out["inline_code"])
        self.assertIn("<ul>", out["ul"])
        self.assertIn("<ol>", out["ol"])
        self.assertIn("<blockquote>", out["quote"])
        self.assertIn("<hr>", out["rule"])
        self.assertIn("<p>Just a sentence.</p>", out["para"])

    def test_code_blocks_get_a_language_label_and_copy_button(self):
        out = self.render({"code": '```python\nprint("hi")\n```'})["code"]
        self.assertIn("codeblock", out)
        self.assertIn("data-copy-code", out)
        self.assertIn("&quot;hi&quot;", out)

        # One label, not two. The bar used to print the friendly name *and* the
        # raw tag, so a Python block was captioned "Python PYTHON".
        self.assertIn(">Python<", out)
        self.assertEqual(out.count("codeblock-name"), 1)
        self.assertNotIn("codeblock-lang", out)

    def test_an_unknown_language_still_gets_its_own_label(self):
        """Falling back to the raw tag beats captioning everything "Code"."""
        out = self.render({"code": "```brainfuck\n+++\n```"})["code"]
        self.assertIn(">brainfuck<", out)

    def test_a_fence_with_no_language_says_so_plainly(self):
        out = self.render({"code": "```\njust text\n```"})["code"]
        self.assertIn(">Code<", out)

    def test_syntax_highlighting_only_adds_spans(self):
        """Highlighting must not alter a single character of the escaped code."""
        cases = {
            "py": '```python\nprint("hi")\n```',
            "js": '```js\nconst n = 1; // note\nfoo("a & b");\n```',
            "html": '```html\n<a href="x">&amp;</a>\n```',
            "sql": "```sql\nSELECT * FROM users WHERE id = 7;\n```",
            "plain": "```\njust text 42\n```",
        }
        out = self.render(cases)
        for name, rendered in out.items():
            body = rendered[rendered.index("<pre>"):]
            stripped = SAFE_TAGS.sub("", body)
            expected = cases[name].split("\n", 1)[1].rsplit("\n```", 1)[0]
            expected = (
                expected.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
                .replace('"', "&quot;").replace("'", "&#39;")
            )
            self.assertEqual(stripped, expected, f"{name} was altered by highlighting")

    def test_tables_render_and_scroll_horizontally(self):
        out = self.render({"table": "| Name | Value |\n|---|---|\n| alpha | 1 |"})["table"]
        self.assertIn("<table>", out)
        self.assertIn("<th>Name</th>", out)
        self.assertIn("<td>alpha</td>", out)
        self.assertIn("table-scroll", out)  # keeps wide tables inside the bubble

    def test_a_mixed_reply_keeps_every_part(self):
        out = self.render(
            {"mixed": "Intro line\n\n```js\nlet x = 1;\n```\n\n- point one\n- point two\n\n## Summary"}
        )["mixed"]
        self.assertIn("<p>Intro line</p>", out)
        self.assertIn("codeblock", out)
        self.assertIn("<ul>", out)
        self.assertIn("<h2>Summary</h2>", out)

    # -- maths and line breaks ---------------------------------------------
    def test_latex_that_slips_through_reads_as_maths(self):
        out = self.render({
            "formula": r"\(x = \frac{-b \pm \sqrt{b^2 - 4ac}}{2a}\)",
            "inline": r"Area $\pi r^2$ at 30^\circ",
            "display": "$$E = mc^2$$",
        })
        self.assertIn("x = (-b ± √(b² - 4ac))/(2a)", out["formula"])
        self.assertIn("Area π r² at 30°", out["inline"])
        self.assertIn("E = mc²", out["display"])

    def test_money_and_code_are_left_alone(self):
        out = self.render({"money": "It costs $5 and $10", "code": r"Type `x^2 + \frac{1}{2}` exactly"})
        self.assertIn("It costs $5 and $10", out["money"])
        self.assertIn(r"<code>x^2 + \frac{1}{2}</code>", out["code"])

    def test_tidying_maths_cannot_smuggle_in_markup(self):
        out = self.render({"x": r"\(\frac{<script>x</script>}{2}\) and $<img src=x>^2$"})["x"]
        self.assertNotIn("<script", out)
        self.assertNotIn("<img", out)
        self.assertNotIn("<", SAFE_TAGS.sub("", out))

    def test_a_models_line_breaks_are_kept(self):
        """Working written one step per line must not run together."""
        out = self.render({"steps": "2x + 3 = 7\n2x = 4\nx = 2"})["steps"]
        self.assertEqual(out, "<p>2x + 3 = 7<br>2x = 4<br>x = 2</p>")

    def test_empty_and_missing_input_are_safe(self):
        out = self.render({"empty": "", "spaces": "   "})
        self.assertEqual(out["empty"], "")
        self.assertNotIn("undefined", out["spaces"])


class MoneyFilterTests(SimpleTestCase):
    """``money`` exists because a template cannot format then prefix a symbol.

    ``"$"|add:total|floatformat:2`` concatenates first and asks floatformat to
    parse "$1234.5", which it cannot — so the figure rendered as nothing. That
    shipped on the dashboard's revenue tile and the payments total.
    """

    def render(self, value):
        from django.template import Context, Template

        return Template("{% load ai_extras %}{{ v|money }}").render(Context({"v": value}))

    def test_a_plain_amount_is_grouped_and_two_places(self):
        self.assertEqual(self.render(Decimal("1234.5")), "$1,234.50")

    def test_zero_and_missing_values_are_still_money(self):
        for value in (Decimal("0"), 0, None, ""):
            self.assertEqual(self.render(value), "$0.00", repr(value))

    def test_the_sign_goes_outside_the_symbol(self):
        """A loss reads as -$5.00. "$-5.00" is a currency nobody uses."""
        self.assertEqual(self.render(Decimal("-5")), "-$5.00")
        self.assertEqual(self.render(Decimal("-1234.56")), "-$1,234.56")

    def test_a_loss_too_small_to_show_is_not_negative_zero(self):
        """At two decimal places a third of a cent is zero, not "-$0.00"."""
        self.assertEqual(self.render(Decimal("-0.004")), "$0.00")

    def test_nonsense_does_not_raise(self):
        self.assertEqual(self.render("not a number"), "$0.00")


class TemplateIntegrityTests(SimpleTestCase):
    """Cheap structural checks that do not need a browser."""

    def test_chat_page_loads_no_remote_scripts(self):
        html = CHAT_TEMPLATE.read_text(encoding="utf-8")
        external = re.findall(r'<script[^>]+src="(https?://[^"]+)"', html)
        self.assertEqual(external, [], f"Chat page must not load remote scripts: {external}")

    def test_page_templates_extend_a_base_or_are_standalone_documents(self):
        templates = Path(settings.BASE_DIR) / "templates"
        for path in sorted(templates.rglob("*.html")):
            relative = path.relative_to(templates).as_posix()
            if relative.startswith(("components/", "base/", "emails/")) or path.name.startswith("_"):
                continue
            text = path.read_text(encoding="utf-8").lstrip()
            self.assertTrue(
                text.startswith(("{% extends", "{% load", "<!doctype")),
                f"{relative} should extend a base template or be a standalone document",
            )

    def test_no_stray_control_characters_in_source(self):
        """Guards against a corrupted edit writing raw control bytes into a file."""
        root = Path(settings.BASE_DIR)
        for folder in ("templates", "AI"):
            for path in sorted((root / folder).rglob("*")):
                if path.suffix not in {".html", ".py"} or not path.is_file():
                    continue
                text = path.read_text(encoding="utf-8", errors="replace")
                stray = {ch for ch in text if ord(ch) < 32 and ch not in "\n\r\t"}
                self.assertEqual(
                    stray, set(), f"{path.relative_to(root)} contains control characters: {stray!r}"
                )

    def test_no_multiline_django_comments(self):
        """``{# … #}`` is single-line only.

        A ``{#`` left unclosed on its own line is not parsed as a comment at
        all — Django renders the whole block as page text. That shipped once,
        putting three paragraphs of developer notes above the composer. Use
        ``{% comment %}`` for anything spanning lines.
        """
        templates = Path(settings.BASE_DIR) / "templates"
        for path in sorted(templates.rglob("*.html")):
            for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                if "{#" not in line:
                    continue
                self.assertIn(
                    "#}",
                    line[line.index("{#"):],
                    f"{path.relative_to(templates).as_posix()}:{number} opens a "
                    f"{{# comment #}} it never closes on the same line — it will "
                    f"render as visible text. Use {{% comment %}} instead.",
                )

    def test_no_template_prefixes_a_literal_onto_a_value_with_add(self):
        """``"$"|add:total`` silently produces nothing.

        Django's ``add`` coerces to the type of its *left* operand. With a
        string on the left and a number on the right the addition fails, and a
        failed filter renders as the empty string — so the label or figure
        simply disappears, with no error anywhere.

        This has now shipped three times: the dashboard's revenue tile, the
        payments total, and three cards on the new console pages. The fix is
        always the same — format the value first (``value|intcomma|add:" left"``)
        or build the string in the view — so the pattern is banned outright.
        """
        # Concatenating two *strings* this way is fine — `add` only fails when
        # the right operand is a number. A static check cannot tell the two
        # apart, so the handful of string-to-string uses are named here with
        # the reason, and everything else is a bug.
        override_prefix = ':root:not([data-motion="full"]) '
        allowed = {
            # BRAND.support_email is a string, so this builds a real mailto:.
            "public/plans.html": {"mailto:"},
        }

        templates = Path(settings.BASE_DIR) / "templates"
        offenders = []
        # A double-quoted literal that *starts* a filter chain and is then
        # given |add:. The lookbehind excludes a quote preceded by a colon,
        # which is a filter argument rather than the chain's left operand —
        # `value|stringformat:"s"|add:" left"` is the correct direction and
        # must not be flagged.
        pattern = re.compile(r'(?<![:\w])"[^"]*"\|add:')
        for path in sorted(templates.rglob("*.html")):
            relative = path.relative_to(templates).as_posix()
            for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                for match in pattern.finditer(line):
                    literal = match.group(0).split('"')[1]
                    if literal in allowed.get(relative, set()):
                        continue
                    offenders.append(f"{relative}:{number}")
        self.assertEqual(
            offenders, [],
            "a string literal on the left of |add: renders as nothing when the "
            "right side is a number. Format the value first, or build the "
            "string in the view. Offending lines: " + ", ".join(offenders),
        )

    def test_attachment_filenames_can_be_truncated(self):
        """The chip's text column must be a block for the ellipsis to apply.

        ``max-width``, ``overflow`` and ``text-overflow`` are ignored on a
        non-replaced inline element, so a long filename used to run straight
        under the remove button instead of ellipsising.
        """
        html = DESIGN_TEMPLATE.read_text(encoding="utf-8")
        for selector in (".attach-name", ".attach-size"):
            rule = re.search(re.escape(selector) + r"\s*\{([^}]*)\}", html)
            self.assertIsNotNone(rule, f"{selector} rule is missing")
            body = rule.group(1)
            self.assertIn("display: block", body, f"{selector} must be a block to clip text")
            self.assertIn("overflow: hidden", body, f"{selector} must hide its overflow")
            self.assertIn("text-overflow: ellipsis", body, f"{selector} must ellipsise")

    def test_forms_are_posted_to_their_attribute_not_their_property(self):
        """``form.action`` is not reliably the URL, and the failure is silent.

        A form's named controls become properties on the form element and
        shadow the built-in ones. Several panels carry a hidden
        ``<input name="action">`` saying which operation to run, so on those
        forms ``form.action`` is that input — and ``fetch(form.action)``
        posts to "/app/settings/[object HTMLInputElement]" and 404s, with
        nothing in the console to say why.

        The same trap applies to ``method``, ``submit``, ``id`` and ``name``,
        so this checks the whole family rather than the one that bit us.
        """
        shadowed = ("action", "method", "submit", "reset", "elements")
        for path in (
            Path(settings.BASE_DIR) / "templates" / "chat" / "chat.html",
            Path(settings.BASE_DIR) / "templates" / "auth" / "_epc_auth.html",
        ):
            # Comments are stripped first, so the explanation of this very trap
            # does not read as an instance of it.
            source = strip_comments(path.read_text(encoding="utf-8"))
            for name in shadowed:
                found = re.findall(rf"\bform\.{name}\b", source)
                self.assertEqual(
                    found, [],
                    f"{path.name} reads form.{name} as a property; a control of "
                    f"that name would shadow it. Use getAttribute() instead.",
                )

    def test_composer_offers_a_choice_of_attachment_sources(self):
        """The "+" button opens a menu rather than one undifferentiated picker."""
        html = CHAT_TEMPLATE.read_text(encoding="utf-8")
        self.assertIn('id="attachMenu"', html)
        for choice in ("image", "camera", "doc", "all"):
            self.assertIn(f'data-pick="{choice}"', html, f"menu is missing the {choice} option")
        self.assertIn('aria-haspopup="menu"', html, "the trigger must announce its menu")

    def test_images_open_in_the_viewer_but_stay_real_links(self):
        """Clicking a thumbnail opens the overlay; the anchor underneath survives."""
        html = chat_source()
        self.assertIn('id="lightbox"', html)
        self.assertIn('id="lbImage"', html)
        self.assertIn("cursor: zoom-in", html, "thumbnails should look openable")
        # Modified clicks must fall through to the browser's own handling.
        self.assertIn("event.metaKey || event.ctrlKey", html)
        self.assertIn('<a href="{{ file.file.url }}" target="_blank" rel="noopener">', html)


def _strip_css_comments(css: str) -> str:
    """``/* … */`` removed, newlines kept so offsets stay roughly comparable."""
    return re.sub(r"/\*.*?\*/", lambda m: "\n" * m.group(0).count("\n"), css, flags=re.S)


def _style_blocks(path: Path) -> str:
    """Everything between ``<style>`` and ``</style>``, comments removed."""
    text = path.read_text(encoding="utf-8")
    return _strip_css_comments("\n".join(re.findall(r"<style[^>]*>(.*?)</style>", text, re.S)))


def _rules(css: str):
    """Walk the stylesheet, yielding ``(offset, selector, body, ancestors)``.

    ``ancestors`` is the list of enclosing at-rule preambles, so a rule inside
    ``@media (prefers-reduced-motion: reduce)`` can be told apart from the same
    rule written at the top level — which is the whole point: the two look
    almost identical in a diff and behave completely differently.
    """
    stack, preamble, start = [], [], 0
    i = 0
    while i < len(css):
        ch = css[i]
        if ch == "{":
            head = css[start:i].strip()
            stack.append(head)
            preamble.append((head, i))
            start = i + 1
        elif ch == "}":
            if preamble:
                head, open_at = preamble.pop()
                body = css[start:i]
                if not head.startswith("@") and head:
                    yield open_at, head, body, [h for h, _ in preamble]
                stack.pop() if stack else None
            start = i + 1
        elif ch == ";" and not preamble:
            start = i + 1
        i += 1


#: Selector -> the animation declarations it makes, for one stylesheet.
_ANIM = re.compile(r"animation(?:-name)?\s*:\s*([^;}]+)")


class DesignStylesheetTests(SimpleTestCase):
    """Structural checks on the shared stylesheet.

    Three faults shipped here that read perfectly well in the source and were
    invisible until a browser computed them, so each has a test:

    * a block pasted twice, whose second copy left the tail of a media query
      stranded at the top level — which switched the thinking indicator off for
      everybody, in every motion setting;
    * ``prefers-reduced-motion`` rules written *above* the rules they switch
      off, which therefore lost on source order and stopped nothing;
    * the same, but losing on specificity to a ``:first-child`` further down.

    None of these are catchable by eye in a long stylesheet and none of them
    raise anything. They are exactly what a test is for.
    """

    def test_style_braces_balance(self):
        for path in (DESIGN_TEMPLATE, CHAT_TEMPLATE):
            css = _style_blocks(path)
            self.assertEqual(
                css.count("{"),
                css.count("}"),
                f"{path.name}: unbalanced braces in <style> — a stray brace silently "
                f"reparents every rule after it",
            )

    def test_animation_none_is_only_ever_a_reduced_motion_rule(self):
        """Switching an animation off unconditionally is always a mistake here.

        It is also what a half-deleted media query leaves behind, which is how
        the thinking indicator went still: the closing rules of a reduced-motion
        block survived a bad edit at the top level, where they applied to
        everyone.
        """
        #: Rules that switch an animation off for a reason other than motion
        #: preference. Each one suppresses a *competing* animation for the
        #: moment another is running, which is the opposite of the fault this
        #: test exists to catch — so each is named here rather than the test
        #: being weakened to let any of them through.
        override_prefix = ':root:not([data-motion="full"]) '
        allowed = {
            # While an answer is being uncovered word by word, its blocks must
            # not also be sliding in: two entrances at once reads as a stutter.
            ".md.is-revealing > *",
        }
        offenders = []
        for offset, selector, body, ancestors in _rules(_style_blocks(DESIGN_TEMPLATE)):
            if selector.strip().replace(override_prefix, "") in allowed:
                continue
            if not any("prefers-reduced-motion" in a for a in ancestors):
                for value in _ANIM.findall(body):
                    if value.strip() == "none":
                        line = _style_blocks(DESIGN_TEMPLATE)[:offset].count("\n") + 1
                        offenders.append(f"{selector} (line ~{line} of <style>)")
        self.assertEqual(
            offenders,
            [],
            "these rules switch an animation off for every visitor, not just those "
            "who asked for less motion: " + "; ".join(offenders),
        )

    def test_reduced_motion_rules_are_not_beaten_by_later_rules(self):
        """A media query carries no extra weight of its own.

        ``@media (prefers-reduced-motion: reduce) { .x { animation: none } }``
        does nothing at all if ``.x { animation: … }`` appears further down the
        file, or if a more specific ``.x:first-child`` does. Both happened, and
        both left the empty state drifting for people who had asked for stillness.
        """
        css = _style_blocks(DESIGN_TEMPLATE)
        rules = list(_rules(css))
        #: Reduced-motion rules are written behind an override
        #: (`:root:not([data-motion="full"]) …`) so a reader can ask for the
        #: motion back. Stripped here, or every comparison below would be
        #: against a selector that matches nothing and the test would pass
        #: while catching nothing.
        override = ':root:not([data-motion="full"]) '
        silenced = [
            (offset, sel.strip().replace(override, ""))
            for offset, selector, body, ancestors in rules
            if any("prefers-reduced-motion" in a for a in ancestors)
            and any(v.strip() == "none" for v in _ANIM.findall(body))
            for sel in selector.split(",")
        ]
        problems = []
        for offset, target in silenced:
            for other_offset, other_selector, body, ancestors in rules:
                if other_offset <= offset or any("prefers-reduced-motion" in a for a in ancestors):
                    continue
                if not any(v.strip() not in ("none", "") for v in _ANIM.findall(body)):
                    continue
                for sel in (s.strip() for s in other_selector.split(",")):
                    same = sel == target
                    # `.hero-ambient i:first-child` outranks `.hero-ambient i`.
                    more_specific = (
                        sel.startswith(target)
                        and sel[len(target):][:1] in (":", ".", "#", "[")
                    )
                    if same or more_specific:
                        problems.append(f"{target!r} silenced, then {sel!r} starts it again below")
        self.assertEqual(sorted(set(problems)), [], "; ".join(sorted(set(problems))))

    def test_no_block_of_the_stylesheet_is_pasted_twice(self):
        """A duplicated paste is how the stranded media-query tail got in.

        Twelve identical consecutive lines is far past coincidence in CSS, where
        real repetition is a line or two of shared declarations.
        """
        css = _style_blocks(DESIGN_TEMPLATE)
        # Blank lines are skipped rather than compared: stripping comments
        # leaves runs of them behind, and two identical runs of whitespace are
        # not a duplicated paste.
        lines = [(n, ln.rstrip()) for n, ln in enumerate(css.splitlines(), 1) if ln.strip()]
        window, seen, duplicates = 12, {}, []
        for i in range(len(lines) - window):
            chunk = tuple(ln for _, ln in lines[i : i + window])
            if chunk in seen:
                duplicates.append(
                    f"lines {seen[chunk]} and {lines[i][0]} of <style> are identical"
                )
            else:
                seen[chunk] = lines[i][0]
        self.assertEqual(duplicates[:3], [], "; ".join(duplicates[:3]))


LIGHT_RENDERER = Path(settings.BASE_DIR) / "templates" / "components" / "light_markdown_js.html"


@unittest.skipIf(NODE is None, "Node.js is not installed; skipping renderer tests")
class LightRendererTests(SimpleTestCase):
    """The small renderer used by shared chats and the printable export."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._tmp = tempfile.TemporaryDirectory()
        directory = Path(cls._tmp.name)
        source = LIGHT_RENDERER.read_text(encoding="utf-8")

        def include(match):
            text = (LIGHT_RENDERER.parents[1] / match.group(1)).read_text(encoding="utf-8")
            return re.sub(r"\{% comment %\}.*?\{% endcomment %\}", "", text, flags=re.S)

        source = re.sub(r"\{% comment %\}.*?\{% endcomment %\}", "", source, flags=re.S)
        source = re.sub(r'\{% include "([^"]+)" %\}', include, source)
        (directory / "renderer.js").write_text(source + "\nmodule.exports = { renderMarkdown: markdown };\n",
                                               encoding="utf-8")
        (directory / "harness.js").write_text(HARNESS, encoding="utf-8")
        cls._dir = directory

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()
        super().tearDownClass()

    def render(self, cases):
        result = subprocess.run([NODE, "harness.js", json.dumps(cases)], cwd=self._dir, capture_output=True,
                                text=True, encoding="utf-8", timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_tables_render_as_tables_with_alignment(self):
        out = self.render({"t": "| Stage | Output |\n|:---|---:|\n| Light | **ATP** |\n| Calvin | Glucose |"})["t"]
        self.assertIn("<th>Stage</th>", out)
        self.assertIn('<th style="text-align:right">Output</th>', out)
        self.assertIn('<td style="text-align:right"><strong>ATP</strong></td>', out)
        self.assertEqual(out.count("<tr>"), 3)

    def test_table_cells_are_escaped(self):
        out = self.render({"t": "| a |\n|---|\n| <img src=x onerror=alert(1)> |"})["t"]
        self.assertNotIn("<img", out)
        self.assertIn("&lt;img", out)
        for tag in re.findall(r"</?[a-z0-9]+[^>]*>", out):
            self.assertRegex(tag, SAFE_TAGS)

    def test_a_pipe_in_a_sentence_is_not_a_table(self):
        out = self.render({"t": "Use a | b to join.\nNext line."})["t"]
        self.assertNotIn("<table>", out)
