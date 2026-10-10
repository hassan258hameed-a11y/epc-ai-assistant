# Site audit and improvement round — September 2026

This records what the site had before this round, how it is built, the bugs
that were reproduced, what comparable products do, and which features were
chosen and why. The results of the round (what was fixed and built, and what
was tested) are at the end.

## 1. What already existed

**Student app** (`/chat/`, panels under `/app/…`):
- **Chat:** several models, each with its own token wallet; images, PDF/Word/Excel/PowerPoint/CSV/text files, paste, drag-and-drop and camera; voice dictation and read-aloud; image generation.
- **Answer tools:** regenerate, rewrite (shorter, simpler, more detail, steps, translate), edit and resend, follow-up suggestions, thumbs up/down with a reason, save an answer, copy, Markdown with code highlighting and maths.
- **Composer:** per-chat drafts, an offline banner, daily/weekly/monthly limits with a countdown, a low-token banner.
- **Sidebar:** title search, folders (colour, instructions), pin, rename, archive, move, delete, export (Markdown, text, print), flashcards and quizzes from a chat, token and usage meters, streak and daily goal, six colour palettes, light/dark.
- **Command palette** (Ctrl+K) with content search; a keyboard shortcuts dialog; read-only share links.
- **Panels:** Usage (with CSV), Pricing, Compare models, Prompt library, Saved answers, Study, Notifications, Help (with tickets), Profile, Settings (appearance, defaults, personalisation, notifications, Telegram, sign-in codes, data export, delete account), Billing, Archived chats.
- **Standalone pages:** checkout (ABA PayWay / ACLEDA KHQR / Binance Pay), payment status and receipt, referrals, support ticket threads, public shared chats.

**Admin console** (`/admin/`):
- **Dashboard** with ⌘K search.
- **People:** students (filters, bulk actions, notes, tags, messages, plan grants), running-low and expiring-access lists, account creation (single or CSV), referrals and campaigns.
- **Usage:** conversations, AI tokens and distribution, token management and the model editor, usage limits, answer-quality triage, prompt library, analytics.
- **Revenue:** reports and cohorts, plans, promo codes, payments, payment gateways, subscriptions, cost and margin.
- **System:** announcements (scheduled, targeted, Telegram), Telegram, support tickets and saved replies, e-mail log, audit log, system health, data exports, settings.

**Public:** home, plans, legal pages, sign-in, sign-up, password reset, one-time codes, admin sign-in.

## 2. How it is built (patterns kept in this round)

- **Stack:** Django 5.2, SQLite, server-rendered templates. The student app is one shell (`templates/chat/chat.html`). Panels are HTML fragments swapped in place (`X-App-Panel` header), and each still has a real URL.
- **Student design system:** `templates/app/_design.html`, built on CSS custom properties. Light and dark tokens are defined twice: under `[data-theme]` and under `prefers-color-scheme`. There are six accent palettes. Choices are stored in `localStorage` (`lumen-theme`, `palette`).
- **Console:** Tailwind (a local Play build, `darkMode: 'class'`) plus `console_design.html`; four pages carry their own CSS (`.ca-*`, `.pm-*`, `.cm-*`, `.an-*`).
- **Icons:** SVG sprites (`components/icons.html`, `console_icons.html`, plus inline symbols in `chat.html`).
- **Motion rules:** decorative animation plays a few times and stops; no `backdrop-filter` in the console; `prefers-reduced-motion` is honoured unless the student turns animations on.

## 3. Bugs reproduced (before this round)

The audit used code reading by three agents plus an automated browser sweep: every page at 390×844, 844×390, 820×1180, 1180×820 and 1440×900, in light and dark, about 1,000 page loads.

### Deleting chats (the reported bug)
| Problem | Severity |
|---|---|
| The ⋯ menu was placed as if 230px tall but is about 415px, so Delete (the last item) fell off-screen for rows below mid-screen, on phones and on most desktop rows | High |
| The ⋯ button had `opacity: 0` without hover, so it was invisible on touch screens and when reached by keyboard. It was the only way to delete | High |
| Delete failures were silent: no `.catch`, and non-OK answers were ignored | Med |
| The confirm button had no danger style (`.btn-solid.is-danger` didn't exist) | Med |
| Focus landed on the destructive button, with no focus trap, focus return or backdrop click | Med |
| Deleting a chat left stale folder counts and empty group headings, and showed no toast | Med |
| Deleting the open chat pushed a history entry to a dead URL and lost the draft | Low |
| Deleting was permanent, with no undo. The files stayed on disk | Med |
| An open chat that had no sidebar row (archived, or beyond the first 200) couldn't be deleted at all | Med |
| After another tab deleted the chat, "Try again" looped forever | Med |
| After "Mark all as read", deleting a notification redirected to a POST-only URL: 405 and a blank panel | High |
| Removing a saved answer was really a toggle, so a stale tab saved it again. Pin and archive were toggles too | Med |
| Deleting a prompt or study set had no confirmation | Med |

### Light theme and themes generally
| Problem | Severity |
|---|---|
| The palette script wrote accent colours inline on `<html>` and re-derived them only for one of four theme controls. Switching theme from Settings or the phone account menu left dark navy fills under dark text in about 57 places. This was the "light pages render wrong" report | High |
| Light `--ink-4` (dates, section labels, token counts) was 2.3:1 on the sidebar | High |
| Toasts in dark mode were white text on a near-white pill | High |
| The stop icon and the usage-chart tooltip were invisible in dark mode | Med |
| The system-dark block was missing `--ok-line` and `--warn-soft` | Low |
| The onboarding card had a peach gradient in dark mode | Low |
| Focus rings and glows were fixed indigo instead of the chosen palette | Low |
| White text on accent-filled buttons in dark mode was about 3.2:1 | Med |
| The sign-in, sign-up, password and code pages had no dark theme | High |
| The admin console's custom-CSS pages had light-only status colours in dark mode | Med |

### Mobile and tablet
| Problem | Severity |
|---|---|
| The command palette on a phone had no close button | High |
| The closed sidebar drawer stayed keyboard-focusable while off-screen | Med |
| The conversation card was flush left on phones and portrait iPads | Med |
| Many touch targets were 20–32px | Med |
| Admin dialogs didn't fit a landscape phone | High |
| Referral campaigns couldn't be reached on phones | High |
| The admin sidebar was absent from 768 to 1279px, including iPad landscape | Med |
| The admin bulk bar covered the list on phones | Med |
| The home-page header wrapped at iPad portrait width | Med |

### Other functional bugs
| Problem | Severity |
|---|---|
| Escape closed a dialog and also stopped the answer being written | Med |
| Closed dialogs stayed in the tab order | Med |
| Panel forms painted error pages (403/405/500) into the panel | Med |
| Typing moved focus to the composer behind open modals | Low |
| The "No tokens left" placeholder stayed after switching to a funded model | Low |
| The `#i-cpu` icon was missing | Low |
| Error pages that weren't JSON showed "can't reach the server" | Low |

### Admin view-level bugs
About 30 were confirmed. Examples:
- Resetting your own password logged you out, returned a 400 and lost the temporary password.
- Confirming or rejecting an already-paid payment reported success that hadn't happened.
- Bundle plans were only partly credited on a grant and only partly reversed on a refund.
- Bad query values caused 500s.
- Several pages ran N+1 queries.
- Dates were in UTC in messages sent to students.

The full list is in the console agent's report and in `AI/tests/test_console_overhaul.py`.

## 4. What comparable platforms do, and what was chosen

Reviewed: ChatGPT, Claude.ai, Poe, TypingMind, LibreChat, Open WebUI, OpenRouter, ChatHub, Perplexity, Mammouth, T3 Chat, Msty.

| Chosen for students | Seen in | Why it fits this site |
|---|---|---|
| **Recently deleted (trash) with Undo** | Grok; missing from ChatGPT and Claude | Fixes the reported deletion problem at its root: deleting is never a disaster |
| **Model cards in the picker** (what each model is for, what it can do, cost of a typical answer, your balance, status) | LibreChat, Open WebUI, Poe, Mammouth, OpenRouter | Students chose by name alone and spent expensive wallets on easy questions |
| **One tap to send the same message with another model** | ChatGPT, Mammouth | Out of tokens, paused or erroring models previously ended in plain text |
| **Never pay twice** (a message ID makes a retry fetch the saved answer) | LibreChat resumable streams | Phone connections drop mid-request, and "Try again" used to charge again |
| **Model status notice and maintenance mode** | OpenRouter, Open WebUI banners | Incidents can be explained to students instead of producing errors |
| **Branch from an answer** | ChatGPT, LibreChat, TypingMind | Try another approach from a good point, or drop a long, costly history |
| **Search filters and saved answers in search** | Open WebUI, Mammouth | Finding previous work: archived chats and date ranges were unsearchable |
| **Token estimate for long messages** | Poe, TypingMind | Pasting a long document is where students overspend without noticing |
| **Answer-ready tab mark** | Open WebUI | Thinking models take a while and students switch tabs |

| Chosen for admins | Seen in |
|---|---|
| **Model status page:** measured health (error rate, p50/p95, last error), declared status with a student-facing note, pause all chat | OpenRouter, Open WebUI |
| **"Can this student chat?" explainer and per-model wallets on the student page** | Open WebUI "preview access" |
| **Daily admin digest** by Telegram and e-mail | TypingMind, OpenRouter |
| **Ticket assignment to any admin, bulk ticket actions, payment filters, subscription extend, e-mail retry, editable "best for" per model** | Standard admin gaps found in the audit |

**Not chosen in the first round, and why:**
- **Streaming replies:** needs an ASGI or threaded server and a proxy that doesn't buffer. *Built in round two (section 6).*
- **Search by meaning:** needs embeddings and a vector store. *Built in round two, with vectors stored in the database instead of a separate vector store.*
- **Silent automatic model routing:** it would spend a different wallet without asking. Still not chosen.
- **Web push when the tab is closed:** needs VAPID keys and a service worker. *Built in round two.*
- **Staff roles with scoped permissions:** a larger change to every console view; worth its own round. *Built in round two.*

## 5. Results

**Built.** Everything in the "chosen" tables above, plus the following:
- Chat options (⋯) in the chat header on every screen size.
- Menus that open as bottom sheets on phones.
- A Recently deleted panel with restore, delete for good, and empty.
- The colour palette applied on the checkout, receipt and ticket pages.
- Tables rendered properly in shared chats and printed exports.
- A dark theme for every sign-in page, following the same theme choice everywhere (app, public pages and console).

**Fixed.** Every bug listed in section 3. While testing, two more were found and fixed:
- The one-time sign-in code check could pick a voided code when two were created in the same clock tick (about 15 ms on Windows). The result was "That code has expired" for a code that was valid.
- The console's page transform could leave fixed dialogs off-screen after a slow load.

**Tested.**
- **Automated tests:** 1,400 in the full suite, all passing. New modules:
  - `test_chat_recovery` (39)
  - `test_console_overhaul` (65)
  - `test_model_status` (32)
  - `test_public_auth_ui` (28)
  - `LightRendererTests` (3)
- **Browser sweep:** every student, public and admin page (68 pages) at 390×844, 844×390, 820×1180, 1180×820 and 1440×900, in light and dark — 690 page loads.
  - No horizontal overflow, off-screen elements, JavaScript errors, duplicate ids, failed pages or tap targets under 32px.
  - Remaining flags are expected: pill-tab highlights the checker can't see, a disabled button's label, intentional dark bands, and Token management, which is light by design.
- **Click-through flows** (26 checks), asserted against the database:
  - delete then Undo, from the phone sheet and the desktop sidebar;
  - the Recently deleted panel;
  - theme switching with a custom palette;
  - the model picker as a sheet;
  - the search close button and filters;
  - Tab never entering the closed drawer;
  - fallback models on a failed send;
  - the model notice, branching, and toast readability.
- **Agent checks:** the console and public-page agents each ran their own clicks through every dialog, menu and bulk bar at all five sizes.

**Not done, and why.** See the "Not chosen" list in section 4. Also:
- E-mail retry only covers low-token warnings, because the log doesn't store other message bodies.
- Payments, Binance Pay, ABA PayWay, ACLEDA/Bakong and Telegram still need the production credentials from the earlier round before they can be tested against the real services.

## 6. Round two: the four server features

Built on 28 September 2026, after the first round listed them as needing server changes.

| Feature | What students or admins get | Where it lives |
|---|---|---|
| **Live answers** | Answers appear word by word. Stop keeps what was written and charges only that. A closed tab or dropped connection doesn't lose the answer: it finishes on the server and appears when the student returns. Regenerate and Edit stream too. | `AI/services/stream_service.py`, the `stream()` method on both vendors, `chat.html` |
| **Search by meaning** | The ⌘K palette adds a **By meaning** group: "how do leaves make food" finds the photosynthesis chat. It never repeats a chat the word search already found. | `AI/services/embedding_service.py`, `/api/chat/search/meaning/`, `index_embeddings` |
| **Browser notifications** | Per-device switch in Settings. Alerts arrive with the site closed: answer ready (when the student left mid-answer), payments, support replies, low tokens, expiry, announcements. Each alert type has its own switch. iPhone instructions included. | `AI/services/push_service.py`, `/sw.js`, `generate_vapid_keys` |
| **Staff roles** | Owner, Support, Finance, Content, Operations. The Staff page is for owners only. Every console route is mapped to an area and enforced in one middleware. Menus, ⌘K, the bell, the dashboard and the student page only show what the role can use. | `AI/services/staff_permissions.py`, `StaffAreaMiddleware`, `/admin/staff/` |
| **Token management in dark mode** | Follows the console theme like every other page; the light look is unchanged. | `templates/console/tokens.html` |

**Decisions worth knowing**
- **Stopping an answer.** Stopping is billed for what was written. OpenAI only reports usage at the end of a stream, so a stopped answer's usage is estimated from its length. Stopped before the first word, only the question is charged and nothing is saved. Before this round, Stop only hung up the browser, and the full answer was charged anyway.
- **Search by meaning storage.** Vectors live in the database and are compared with numpy, so there is no new service to run. The rate limit is separate from chat.
- **Admins with no role.** An administrator with no role fails closed (shared areas only). Existing administrators became owners, and the last owner can't step down.
- **Hidden controls.** Links and buttons a role can't use are hidden, not left to open a "no access" page. A crawl as each role found 0 such controls, across 24,779 links and 1,501 forms.

**Tested**
- **Full suite:** 1,598 tests, all passing (5 live-provider tests skipped without real keys). New or extended modules:
  - `test_streaming` (39)
  - `test_semantic_search` (46)
  - `test_push` (50)
  - `test_staff_roles` (60)
  - streamed pictures and low-token push copies in existing modules
- **Live answers in a browser,** against a local stand-in for the OpenAI API (no real calls), 19 of 19 checks:
  - words growing on screen, and the suggestion block never visible;
  - tables and code rendered, and the follow-up chips shown;
  - Stop part-way: the answer kept, logged as stopped, charged only for what was written;
  - regenerate written in place;
  - the answer saved after the tab was closed mid-answer;
  - no sideways scroll on a phone in dark mode.
- **Search by meaning in the palette:** 10 of 10 checks on desktop and phone.
- **Browser notifications:** turned on in Edge through Windows' real push service. A test notification was accepted, and turning it off removed the device.
- **Final sweep:** every student, public and admin page (now including Staff) at 390×844, 844×390, 820×1180, 1180×820 and 1440×900, light and dark. 680 page loads.
  - No overflow, off-screen elements, JavaScript errors, duplicate ids or small tap targets.
  - Remaining flags are the known checker false positives: selected pills and gradient buttons, a disabled button's label, and the intentional dark bands on the home page and sign-in side panels.

**Still to do by the site owner**
- Run `python manage.py generate_vapid_keys` and put the three lines it prints in `.env`. Notifications stay off until then.
- Run `python manage.py index_embeddings` once to index past answers. It makes real embedding calls: about 2,000 characters per answer, with `text-embedding-3-small`.
- In production:
  - use a threaded server (`gunicorn --worker-class gthread --threads 8`, or waitress);
  - keep `proxy_read_timeout` above the longest answer;
  - set `REDIS_URL` when running more than one worker, so Stop reaches an answer on another worker.
