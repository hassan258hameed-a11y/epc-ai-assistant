# Developer scripts

Throwaway helpers for local checking. Not imported by the application and not
part of the test suite.

| Script | Purpose |
|---|---|
| `smoke.py` | Request every public page and report status codes. |
| `smoke_student.py` | Same, signed in as a student (also proves console lockout). |
| `smoke_admin.py` | Same, signed in as an administrator. |
| `dump_pages.py` | Save rendered HTML for every page into `_dev/out/` for inspection. |
| `inspect_html.py` | Check dumped HTML for unrendered tags and missing assets. |
| `check_urls.py` | Resolve every `{% url %}` name used in the templates. |

Run them from the project root, e.g. `python _dev/smoke.py`.

The real test suite is `python manage.py test AI`.

## Tailwind styling checks (`_dev/tw/`)

Tailwind is compiled in the browser by the vendored Play CDN, so a single bad
utility class aborts the **whole** stylesheet and every page renders unstyled.
These scripts catch that. They need `npm install jsdom` inside `_dev/tw/`.

```bash
python _dev/dump_pages.py                 # render every page to _dev/out/
node _dev/tw/check_all.js _dev/out/*.html # compile each one, assert real CSS
```

Two failure modes this has already caught:

* `bg-brand-500/12` — Tailwind's default opacity scale has no `12`, so the
  build died and *every* page lost its styling. Fixed by defining a complete
  0–100 opacity scale in `templates/base/design.html`.
* `@apply group` — `group` is a marker class, not a utility. Fixed by moving it
  into the markup.

**Known limitation:** the checker evaluates every inline script on the page, and
jsdom cannot survive all of them. `chat.html` is now skipped outright, and
`console_tokens.html` reports a **false failure** — removing that page's own
scripts makes the same file compile 50KB of CSS, so the markup and the design
system are both fine. Both were verified in real headless Chrome, which renders
them correctly:

```powershell
& "C:\Program Files\Google\Chrome\Application\chrome.exe" --headless=new `
  --disable-gpu --dump-dom --virtual-time-budget=15000 `
  "http://127.0.0.1:8000/chat/" > dump.txt
```

Then confirm `.flex{` and the component classes appear in `dump.txt`.
