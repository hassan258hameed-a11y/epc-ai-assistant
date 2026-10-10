# EPC AI Assistant

A production-ready AI study assistant for students, built as a clean Django
monolith. Students chat with an AI tutor, spend tokens metered on the provider's
**actual** reported usage, and top up through ABA PayWay, ACLEDA KHQR or Binance
Pay. Administrators get a full console for accounts, tokens, payments, analytics
and audit.

```
Student            /login/  /dashboard/  /chat/  /usage/  /plans/  /referrals/
Administrator      /admin/  (the console — the whole admin job lives here)
```

---

## Contents

1. [Requirements](#requirements)
2. [Quick start](#quick-start)
3. [Environment variables](#environment-variables)
4. [Database](#database)
5. [AI provider](#ai-provider)
6. [E-mail and OTP](#e-mail-and-otp)
7. [Telegram bot](#telegram-bot)
8. [Payments](#payments)
9. [Scheduled jobs](#scheduled-jobs)
10. [Architecture](#architecture)
11. [Token management](#token-management)
12. [Operations: health and exports](#operations-health-and-exports)
13. [Live answers, search by meaning, notifications and staff roles](#live-answers-search-by-meaning-notifications-and-staff-roles)
14. [Security](#security)
15. [Testing](#testing)
16. [Production deployment](#production-deployment)
17. [Management commands](#management-commands)
18. [Troubleshooting](#troubleshooting)

---

## Requirements

* Python 3.11 or newer
* Node.js (optional — only used by the front-end renderer tests)
* SQLite for development; PostgreSQL or MySQL for production

> **Windows PowerShell 5.1:** `&&` is a parser error there. Run each command on
> its own line, or join them with `;`. Everything else in this guide works as
> written.

---

## Quick start

Run these **one line at a time**. Windows PowerShell 5.1 does not support `&&`
as a separator — chain with `;` there if you want them on one line.

```bash
pip install -r requirements.txt
cp .env.example .env                                    # PowerShell: copy .env.example .env
python -c "import secrets; print(secrets.token_urlsafe(64))"   # paste into .env as SECRET_KEY
python manage.py migrate
python manage.py createsuperuser
python manage.py seed_demo        # optional — demo data for every screen
python manage.py check_setup      # reports what is and is not configured
python manage.py runserver
```

Then open:

| Surface | URL |
|---|---|
| Website | http://127.0.0.1:8000/ |
| Student sign-in | http://127.0.0.1:8000/login/ |
| Admin console | http://127.0.0.1:8000/admin/login/ |

`seed_demo` prints the generated demo passwords **once** — copy them from the
terminal. Re-running it will not reset existing accounts (use `--reset` for that).

---

## Environment variables

Everything sensitive lives in `.env`, which is git-ignored. `.env.example`
documents every key. The important ones:

### Core

| Variable | Purpose |
|---|---|
| `SECRET_KEY` | Django signing key. Must be long, random and secret. |
| `DEBUG` | `True` in development, **`False`** in production. |
| `ALLOWED_HOSTS` | Comma-separated hostnames your site answers on. |
| `CSRF_TRUSTED_ORIGINS` | Comma-separated `https://` origins behind a proxy. |
| `SITE_URL` | Absolute base URL, used in e-mails, receipts and callbacks. |
| `TIME_ZONE` | Defaults to `Asia/Phnom_Penh`. |

### AI provider

| Variable | Purpose |
|---|---|
| `OPENAI_API_KEY` | **Required for chat.** Never commit this. |
| `OPENAI_BASE_URL` | Point at your reseller's endpoint if not calling OpenAI directly. |
| `OPENAI_MODEL` | Defaults to `gpt-5-mini`. See [Reasoning models](#reasoning-models-the-gpt-5-family). |
| `OPENAI_MAX_OUTPUT_TOKENS` | Cap per reply (default 2048). Per-model overrides live in the catalogue. |
| `AI_MAX_CONTEXT_MESSAGES` | How much history is sent per request (default 20). |
| `AI_VISION_ENABLED` | Allow image attachments where the model supports them. |
| `AI_TEMPERATURE` | Sampling temperature (default 0.4). Low on purpose: study answers should be accurate and repeatable, and a lower temperature also rambles less. Ignored by reasoning models. |
| `AI_PRESENCE_PENALTY` / `AI_FREQUENCY_PENALTY` | Small positive defaults (0.1 / 0.2) that discourage restating the question and padding a finished answer. |
| `AI_MIN_REQUEST_TOKENS` | Balance floor below which a request is refused outright (default 500). |
| `AI_VOICE_ENABLED` | Show the microphone. Off hides voice input entirely. |
| `AI_TRANSCRIBE_MODEL` | Speech-to-text model (default `whisper-1`). |
| `AI_TRANSCRIBE_TOKENS_PER_MINUTE` | Audio is billed per minute by the provider; this converts it into tokens (default 1500). |
| `AI_TRANSCRIBE_MAX_SECONDS` | Hard stop on a recording (default 120). |
| `UPLOAD_MAX_FILES_PER_MESSAGE` | Files per message (default 5). |
| `UPLOAD_MAX_DOCUMENT_BYTES` / `UPLOAD_MAX_TOTAL_BYTES` | Per-file and per-message size caps. |

The catalogue starts with **exactly one model**: the one named in `OPENAI_MODEL`.
That is the model the API key is known to be provisioned for, so it is the only
one the platform can promise works. Add more at **/admin/tokens/?tab=models** when
you have keys for them. Students see the product name (*Atlas 5 Mini*); the raw
provider key stays in the console, where knowing exactly what was billed matters.

### Everything else

E-mail, Telegram, payment credentials, upload limits and rate limits are all
documented inline in `.env.example`.

> **Business settings** (free-trial size, referral bonuses, alert thresholds,
> the system prompt) are **not** environment variables — an administrator edits
> them at `/admin/settings/` and the change takes effect immediately.
> Credentials are never stored in the database.

---

## Database

Development uses SQLite with no configuration. To move to PostgreSQL or MySQL,
set one variable — no application code changes:

```env
DATABASE_URL=postgres://user:password@localhost:5432/epc_ai
# or
DATABASE_URL=mysql://user:password@localhost:3306/epc_ai
```

Then install the driver and migrate:

```bash
pip install "psycopg[binary]"     # PostgreSQL
pip install mysqlclient           # MySQL

python manage.py migrate
```

### Migrations

```bash
python manage.py makemigrations AI
python manage.py migrate
python manage.py showmigrations
```

### A note on concurrency

Token deductions use `SELECT ... FOR UPDATE` row locking. SQLite does not
implement row-level locks, so under real concurrent load **use PostgreSQL or
MySQL**. The balance can never go negative on either — a database check
constraint enforces that — but SQLite serialises writes and may raise
"database is locked" under contention.

---

## AI provider

The provider lives behind a small interface in `AI/services/ai_service.py`:

```python
provider = get_provider()                    # resolved from AI_PROVIDER
response = provider.complete(turns, system_prompt=prompt)
response.input_tokens, response.output_tokens, response.total_tokens
```

**Usage is always taken from the provider's `usage` object — never estimated
from character counts.** The exact number reported is what gets deducted.

To add a provider, subclass `BaseProvider`, implement `complete()` returning an
`AIResponse`, and register it in `PROVIDERS`. Nothing else changes.

### Reasoning models (the gpt-5 family)

`gpt-5-mini` is the configured default. Reasoning models behave differently from
`gpt-4o-mini`, and the provider adapts automatically — no per-model code:

| | Older chat models | Reasoning models (`gpt-5*`, `o1`, `o3`, `o4`) |
|---|---|---|
| Output limit | `max_tokens` | `max_completion_tokens` (`max_tokens` returns HTTP 400) |
| Temperature | `0.7` | omitted — only the default is accepted |
| Hidden thinking | none | **reasoning tokens**, billed and counted in the output |

The important consequence is the budget. A reasoning model spends part of its
output allowance thinking *before* it writes anything, so too small a limit
means it burns the whole budget and returns an **empty reply that you still pay
for**. Measured on `gpt-5-mini`:

```
max_completion_tokens=200   ->  200 reasoning,   0 chars of answer   (wasted)
max_completion_tokens=800   ->  448 reasoning, 693 chars of answer
max_completion_tokens=2048  ->  384 reasoning, 1067 chars of answer
```

The provider therefore enforces a floor of 2,048 output tokens for reasoning
models (`MIN_REASONING_BUDGET`). If a reply is still starved, the student gets a
clear retryable message, **nothing is charged**, and a warning is logged telling
you to raise `OPENAI_MAX_OUTPUT_TOKENS`.

Reasoning tokens are recorded per request on `AIUsageLog.reasoning_tokens`, so
the console shows exactly where a large bill came from.

To use the cheaper non-reasoning model instead, set `OPENAI_MODEL=gpt-4o-mini` —
nothing else changes.

### Cost planning

A short exchange on `gpt-5-mini` costs roughly 300–500 tokens including
reasoning; on `gpt-4o-mini` it is closer to 150–250. A 20,000-token free trial
therefore covers meaningful study on either. Set plan prices from the console —
the plan form shows the effective cost per million tokens as you type.

Billing always follows the provider's reported `usage`, never an estimate, so
reasoning tokens are charged exactly as OpenAI charges them.

---

## E-mail and OTP

Verification codes are 6 digits by default, stored **hashed**, single-use,
expiring after 10 minutes with a 5-attempt cap. Length and lifetime are
configurable at `/admin/settings/`.

With no SMTP configured, Django's console backend prints codes to the terminal —
convenient in development, and it never silently fails.

For Gmail, create an **App Password** (a normal account password will not work):

```env
EMAIL_HOST=smtp.gmail.com
EMAIL_PORT=587
EMAIL_HOST_USER=you@gmail.com
EMAIL_HOST_PASSWORD=your-16-character-app-password
EMAIL_USE_TLS=True
DEFAULT_FROM_EMAIL=EPC AI Assistant <you@gmail.com>
```

---

## Telegram bot

A Telegram bot **cannot message a user first** — the user has to press Start.
The linking flow accounts for that:

1. The student opens **Settings → Telegram** and presses *Connect Telegram*.
2. That creates a one-time link, `t.me/<bot>?start=<token>`, valid for
   `TELEGRAM_LINK_TTL_MINUTES` (default 15) and shown as a button and a QR code.
3. The student presses **Start**; Telegram delivers `/start <token>` to us.
4. The token is consumed (it can never be used again) and the **private** chat is
   linked. Groups and channels can never be linked.

From then on the student's own chat receives, if they allow it: sign-in codes,
payment confirmations and failures, plan and token alerts, support replies and
(optionally) announcements. **Codes and account details only ever go to a
linked, private, unblocked chat** — never to the channel or a group; otherwise
they fall back to e-mail. E-mail keeps working for everything.

Bot commands: `/start`, `/status` (balances and plan), `/help`, `/unlink`.

Messages are plain, bold-titled text with buttons — no emoji.

### What reaches Telegram, and who decides

Every alert the site sends a student can reach their private chat: payments
(confirmed, failed, expired, refunded), tokens (low balance, tokens added by an
administrator), plan expiry, support replies and messages from the team, and
referral rewards. Two sets of switches decide:

- **The administrator's** — `/admin/telegram/` → *Automatic alerts* (also under
  Settings → Telegram alerts): a master switch and one per kind of alert.
- **The student's** — Settings → Telegram: alerts on or off, plus the same
  categories as in-app alerts (low tokens, expiry, payments, announcements).

Sign-in codes are not alerts and are never switched off by either: a student
who chose Telegram for them always gets them there.

### The channel and the group

Announcements published in the console can go to the public **channel**, to a
community **group**, and privately to students who opted in — any mix, chosen
per announcement. Retracting one deletes the channel and group posts too (a bot
may delete its own posts for 48 hours).

- **Channel:** the bot must be an administrator with *Post Messages*.
- **Group:** add the bot as a member; it only needs to be allowed to send
  messages (it can delete its own posts there within 48 hours). Add it once and `/admin/telegram/` shows the group's id
  under *Detected group*. A group upgraded to a supergroup gets a new id; the
  page shows that too. Keep BotFather's privacy mode on — the bot then only
  sees commands. In a group it answers `/help` and sends everything personal
  (`/start`, `/status`, `/unlink`) to the private chat instead.

### Setup

1. Create a bot with [@BotFather](https://t.me/BotFather) (`/newbot`) and copy
   the token. Leave `/setprivacy` enabled.
2. Create the channel, and add the bot as an administrator with *Post Messages*.
   Optionally create a group and add the bot to it (see above).
3. Fill in `.env` and restart:

   ```env
   TELEGRAM_BOT_TOKEN=123456:ABC-your-token
   TELEGRAM_BOT_USERNAME=your_bot_username          # without @
   TELEGRAM_WEBHOOK_SECRET=<python -c "import secrets; print(secrets.token_urlsafe(32))">
   TELEGRAM_CHANNEL_ID=@your_channel                # or -100… for a private channel
   TELEGRAM_CHANNEL_URL=https://t.me/your_channel
   TELEGRAM_GROUP_ID=-100…                          # from "Detected group"
   TELEGRAM_GROUP_URL=https://t.me/+your-invite-link
   ```

4. On **`/admin/telegram/`** press *Register webhook* and *Set command menu*,
   then *Re-check*: the bot should show Online and the channel *Can post*. The
   page also has the full step-by-step guide, a `.env` checklist, test buttons
   and the delivery log.

Or from the command line:

```bash
python manage.py telegram_bot --webhook        # production (requires HTTPS)
python manage.py telegram_bot --set-commands   # the / command menu
python manage.py telegram_bot --poll           # development (no public URL needed;
                                               # --delete-webhook first if one is registered)
python manage.py telegram_bot --status         # inspect the current configuration
```

The webhook at `/webhooks/telegram/` verifies Telegram's secret-token header;
with `DEBUG=False` it refuses every call until `TELEGRAM_WEBHOOK_SECRET` is set.

---

## Payments

Four providers ship in the box, all behind one interface
(`AI/services/payments/base.py`). A student picks a package on **Pricing**, then
chooses a method on the checkout page, which shows the exact amount each one
charges. ACLEDA is offered once: through its own gateway (XPAY) when that is set
up, otherwise as a Bakong KHQR.

| Provider | Student pays | Charged in | How the server confirms it |
|---|---|---|---|
| ABA PayWay | Hosted checkout (ABA Mobile, KHQR, card) | USD (or KHR) | PayWay's pushback, then PayWay's signed **Check Transaction** API |
| ACLEDA Bank (XPAY) | ACLEDA's hosted page (its KHQR, paid from ACLEDA mobile or any Cambodian bank app) — or that KHQR shown on our payment page | USD (or KHR) | ACLEDA's signed **getTxnStatus** — only success *with* a confirm date counts |
| ACLEDA KHQR | Scans a dynamic KHQR with any Cambodian bank app | KHR (or USD) | National Bank **Bakong Open API** `check_transaction_by_md5` (polling — Bakong has no callbacks) |
| Binance Pay | Scans a Binance Pay QR / opens the Binance app | USDT | **RSA-signed** webhook (verified against Binance's published certificate) + order query |

### The rule that matters

**A plan is never activated from the browser.** A redirect back from a bank, a
success page or a screenshot only makes the server *ask the gateway*. Only a
gateway answer the server fetched or authenticated itself — or an explicit admin
confirmation — ever credits tokens. On top of that:

- the amount **and** currency the gateway reports must match the order, or the
  order is held for review (never credited);
- one gateway transaction can pay for one order only (duplicate guard);
- crediting is idempotent — a webhook may arrive ten times and the student is
  credited once.

### Currency, expiry and statuses

`Payment.amount` stays in the plan's currency (USD) so every report adds up;
what was actually charged is kept separately (`charge_amount`,
`charge_currency`, `exchange_rate`). Riel = USD × the rate in
**Settings → Payments** (`payment_khr_rate`), rounded up to ៛100. USDT is 1:1.

Each order has a payment window (`payment_expiry_minutes`, default 15). The
payment page counts down and polls; an unpaid order becomes **Expired** (with a
final gateway check first, so a last-second payment is still credited). Orders
can be **Pending → Paid / Failed / Expired / Cancelled / Refunded**. Students are
told about every outcome in the app, by e-mail and in their private Telegram
chat when linked.

Pressing "Continue" twice reopens the same open order instead of creating a
second one.

### One press of Pay, one attempt

ABA and ACLEDA's XPAY are hosted pages, and both remember every reference they
are sent: PayWay refuses a `tran_id` it has seen ("Duplicated transaction ID"),
and an XPAY session cancels itself if its form is posted twice. So the order
page's **Pay** button asks the server for a *fresh, signed* hand-off each time
(`/payments/<reference>/pay/`), recorded as a `PaymentAttempt` with its own
gateway reference. Checking an order checks its attempts — the newest on every
poll, older ones every minute — so a student who finishes an older attempt in
another tab is still credited, once. For ABA the hand-off page submits itself
to PayWay, and for ACLEDA to ACLEDA's own page. "Or show ACLEDA's KHQR on this
page" makes the server post the session's form itself, read the KHQR out of it
(checksum verified) and show it on our payment page instead. ACLEDA's page only reads a plain urlencoded form — posted multipart it
answers "The system has been updating". There is no one-tap ACLEDA mobile
button: ACLEDA's deeplink dead-ends on an error page unless its separate QR app
is installed.

The browser's return from a bank needs no session: a bank may send it back
with a cross-site POST that carries no cookie, so the return only asks the
gateway and then redirects.

ABA details worth knowing: `payment_gate=0` is posted (unsigned) so a QR-API
merchant profile still gets the full hosted page; `shipping` is always an
explicit `0`; and when a riel payment settles in dollars, the echoed
`original_amount` is what is matched against the order.

### Without credentials

A provider with no credentials falls back to **manual review** (when
`payment_manual_review` is on): the student gets transfer details (and a real
KHQR for ACLEDA once `ACLEDA_KHQR_BAKONG_ID` is set), uploads a screenshot, and
an administrator confirms it at `/admin/payments/`. With manual review off, only
automatically confirmed methods are offered.

### Testing every flow locally — the gateway emulator

With `DEBUG=True` and `PAYMENT_EMULATOR=True`, every gateway points at a local
emulator (`AI/dev/payment_emulator.py`, mounted at `/dev/payment-emulator/`)
that speaks their documented APIs and **checks our signatures** — PayWay's
form hash, XPAY's HMAC over login, password and txid, Binance's HMAC request
signing, the Bakong bearer token — answers with the codes the live gateways
use, and calls our real webhooks back (Binance's RSA-signed). XPAY sessions
are single-use there, as they are at ACLEDA.

The test runner never sees live payment keys: `config/settings.py` blanks them
under `manage.py test`, so the real keys can sit in `.env` without a test ever
reaching a bank. The
payment page shows a *Simulate the payer* button. `AI/tests/test_payment_gateways.py`
runs the same flows automatically.

Binance Pay has no public sandbox; ABA has one
(`https://checkout-sandbox.payway.com.kh`); Bakong's API is production only.

### Admin

- `/admin/payments/` — every order, with the charged currency, gateway order
  ID, expiry, event trail, amount-mismatch / duplicate flags, *Re-check with
  provider*, confirm (also for a late payment), reject and refund.
- `/admin/payment-gateways/` — each gateway's mode (live / sandbox / manual),
  which `.env` variables are set (never their values), the callback URLs to
  register, a **Test connection** button that makes one harmless authenticated
  call, and a go-live checklist.

### Callback URLs

```
https://your-domain/webhooks/payments/aba_payway/   # ABA PayWay pushback
https://your-domain/webhooks/payments/acleda_xpay/  # ACLEDA XPAY callback (optional)
https://your-domain/webhooks/payments/binance/      # Binance Pay webhook
https://your-domain/webhooks/telegram/              # Telegram bot
```

Bakong needs none, and ACLEDA's is optional: XPAY payments are confirmed when
the student comes back, while the page is open, and by the background sweep.
Give ABA the site's domain to whitelist. Each URL must be public HTTPS, and `SITE_URL` must be
set to your real domain — the URLs sent to the gateways are built from it.

### Adding a provider

1. Subclass `PaymentProvider` in `AI/services/payments/`.
2. Implement `create_checkout`, `verify`, `health_check` and (if supported)
   `parse_webhook` — `parse_webhook` **must** authenticate the caller (or
   re-query the gateway) before returning. List its settings in `required_env`.
   A hosted gateway returns a `handoff` checkout and implements
   `start_attempt` (see `aba_payway.py` / `acleda_xpay.py`).
3. Register it in `PROVIDERS`, add its choice to `PaymentMethod`, and add it to
   `CHECKOUT_METHODS` in `AI/views/payments.py`.

---

## Scheduled jobs

Run hourly to send expiry reminders and low-balance alerts, and to clear expired
codes and rate-limit rows:

```bash
python manage.py send_reminders
python manage.py send_reminders --dry-run     # preview without sending
```

**Linux (cron):**

```cron
0 * * * * cd /srv/epc-ai && /srv/epc-ai/.venv/bin/python manage.py send_reminders >> /var/log/epc-ai/reminders.log 2>&1
```

**Windows (Task Scheduler):**

```powershell
schtasks /create /tn "EPC AI Reminders" /tr "C:\path\to\python.exe C:\path\to\manage.py send_reminders" /sc hourly
```

Alerts are de-duplicated in the database, so running the job more often than
necessary is harmless — nobody gets the same reminder twice.

It also indexes new answers for search by meaning (up to 200 a run), so the
index stays current even for students who never search.

The same job re-checks open KHQR orders with Bakong and expires unpaid orders
past their window. Page loads also do this at most once a minute on a
background thread (`PAYMENT_SWEEP_ON_REQUEST`), so nothing depends on the
scheduler — but running it every 5–10 minutes is the reliable setup.

---

## Architecture

```
config/               Django project (settings, root URLs, WSGI/ASGI)
AI/
  models/             Split by domain: users, tokens, chat, billing, comms, system
  services/           All business logic lives here, not in views
    ai_service.py         Provider abstraction; extracts real token usage
    chat_service.py       Orchestrates guard → save → call → charge
    token_service.py      The ledger. Every balance change goes through it
    payment_service.py    create → verify → settle, idempotent
    payments/             One module per payment provider
    otp_service.py        Issue and verify one-time codes
    telegram_service.py   Bot API, linking, notifications
    notification_service.py  In-app alerts, reminders, de-duplication
    referral_service.py   Referral programme and the welcome grant
    account_service.py    Provisioning, enable/disable, bulk import
    analytics_service.py  Dashboard aggregations
    audit_service.py      Audit trail
    settings_service.py   Operator-editable settings
    health_service.py     The operational checks behind /admin/health/
    export_service.py     The dataset catalogue behind /admin/exports/
  views/              Grouped by surface: public, auth, student, chat, payments, console
  forms/              Auth, account and admin forms
  utils/              decorators, ratelimit, uploads, pagination, qr, request
  templatetags/       Template filters and the inline sparkline
  management/commands/  seed_demo, send_reminders, telegram_bot, check_setup
  tests/              310+ tests across accounting, auth, permissions, payments, chat, ops
templates/
  base/               design.html (the design system), public / app / console shells
  components/         logo, icons, charts, stat cards, pagination, empty states
  public/ auth/ student/ chat/ console/ errors/ emails/
static/
  vendor/tailwind.js  Tailwind, vendored locally — the app makes no CDN calls
  brand/favicon.svg
```

### Front-end approach

As specified: **no separate CSS or JS build**. Tailwind utilities live directly
in the templates, and page-specific JavaScript sits in a `{% block scripts %}` on
the page that needs it.

The shared design system — colour ramps, type scale, and component primitives
like `.btn-primary`, `.card`, `.input`, `.badge-*` — is defined once in
`templates/base/design.html` so the product stays visually consistent without a
global stylesheet.

Tailwind is **vendored into `static/vendor/`**, so the application never calls a
CDN at runtime. Charts are hand-rolled inline SVG (`templates/components/charts.html`);
QR codes are rendered server-side as inline SVG. There are no third-party
front-end dependencies to audit or break.

### Token accounting

Every movement is a row in `TokenTransaction`; balances on `TokenAccount` are the
running total. Balances are **never** overwritten.

```python
token_service.grant(user, 50_000, description="Promotional grant", created_by=admin)
token_service.consume(user, response.total_tokens, conversation=chat, message=reply)
token_service.deduct(user, 1_000, description="Correction", created_by=admin)
```

Guarantees, each covered by a test:

* No negative balances — enforced in code *and* by a DB check constraint.
* No double deduction — the account row is locked for the whole transaction.
* No duplicate credits — payments, referrals and free trials use idempotency keys.
* Full traceability — every row records who, why, when and the resulting balance.
* Repairable — `token_service.recalculate(user)` rebuilds a balance from the ledger.

#### Lifetime totals vs the current allocation

`TokenAccount` keeps two pairs of counters, because they answer different
questions:

| Field | Answers |
|---|---|
| `total_allocated` / `total_consumed` | Everything that ever moved. Used by history and audit views. |
| `cycle_allocated` / `cycle_consumed` | The **current** allocation — what a student is shown as *42,350 / 50,000*. |

`token_service.set_balance(user, 50_000)` starts a new cycle: the student ends up
holding exactly 50,000 and sees *0 used*, whatever their history. Top-ups extend
the cycle in progress rather than starting a new one. The invariant
`balance == cycle_allocated - cycle_consumed` is maintained on every write.

---

## Attachments and voice

### Files

A student can attach up to five files to a message — images plus **PDF, Word,
Excel, CSV, JSON, Markdown and plain text**. Every file is validated by its
*content*, never its filename: a renamed executable, an HTML page or a fake PDF
is refused.

Documents are read once, at upload, and the extracted text is stored on the
attachment. That matters for three reasons: a regenerate never re-parses the
file, a follow-up question still has the document in scope, and the text lives
somewhere auditable rather than being rebuilt from a file that might change.

Extraction is deliberately bounded — about 24,000 characters, 40 PDF pages, 300
spreadsheet rows — because document text becomes prompt text, and prompt text
costs tokens. When something is cut short the model is told so explicitly, so it
never implies it read a whole report it only saw the first part of. A scanned PDF
with no text layer says so and suggests attaching the pages as images instead.

Document text is counted in the affordability check, so a large attachment is
refused up front rather than half-way through.

### Voice

The microphone records in the browser and sends the audio to the server, which
**detects the spoken language itself** and returns text in that same language —
Khmer, Hindi, Urdu, English and the rest, with nothing to choose. The transcript
lands in the message box for the student to check and edit; it is never sent on
their behalf, because recognition mishears and a wrong question wastes tokens.

Audio is billed by the minute by the provider, so it is converted to tokens at
`AI_TRANSCRIBE_TOKENS_PER_MINUTE` and logged like any other usage. A failed
transcription charges nothing.

> The browser's own `SpeechRecognition` API is **not** used. It transcribes only
> the one language it is told to expect, so it cannot do the auto-detection this
> needs.

---

## Token management

Everything lives inside the main site — there is no separate admin application.
The console mounts at `/admin/` and token management at **`/admin/tokens/`**,
behind `@admin_required`.

Django's stock admin is **not mounted**. The console covers the whole job:

| Section | Page |
|---|---|
| Students, bulk account creation, referrals | `/admin/users/`, `/admin/accounts/`, `/admin/referrals/` |
| Conversations and full transcripts | `/admin/conversations/` |
| Tokens, the pool, AI models, activity, analytics | `/admin/tokens/` |
| Usage log | `/admin/usage/` |
| Plans, payments, subscriptions | `/admin/plans/`, `/admin/payments/`, `/admin/subscriptions/` |
| Announcements, audit log, settings | `/admin/notifications/`, `/admin/audit-logs/`, `/admin/settings/` |

Set `DJANGO_ADMIN_ENABLED=True` to mount the stock admin at `/django-admin/` as
a break-glass tool if the console is ever unusable. It is off by default.

Opening a student's transcript is a read of their private messages, so it is
written to the audit log like any other consequential action.

One page, five tabs:

| Tab | What it covers |
|---|---|
| **Overview** | The pool balance, allocated / consumed / remaining, request volume and health, consumption trend, where tokens went, heaviest users, recent allocations and pool movements. |
| **Students** | Searchable, filterable, sortable table with each student's allocation, usage, remaining balance and period activity. Assign, add or remove tokens; open a per-student drawer with their ledger and per-model usage. |
| **AI models** | The catalogue. Availability switch, default model, per-model allocation, output budget, temperature, list-price cost and usage counters. Models removed from the catalogue keep their history, shown as retired. |
| **Token activity** | Every AI request, newest first: student, model, input, output, total, tokens charged, latency, errors. |
| **Analytics** | Rolling day / week / month / quarter consumption, input vs output, requests per day, usage by student, token flow by transaction type. |

### The admin token pool

The platform holds one supply of tokens, and every token an administrator gives a
student comes out of it:

```
Pool 300,000  ->  give Student A 25,000  ->  Student A 25,000, pool 275,000
              ->  give Student B 50,000  ->  Student B 50,000, pool 225,000
```

Taking tokens back returns them to the pool, and lowering an allocation refunds
the difference. An assignment the pool cannot cover is **refused in full** — the
student is not part-credited and the pool does not move.

Two details worth knowing:

* **Assigning only draws the difference.** Topping a student from 30,000 up to
  50,000 costs the pool 20,000, not 50,000.
* **Usage is not reclaimed.** Tokens a student spends on AI are consumed; they do
  not flow back to the pool.

Top the pool up from the card at the top of `/admin/tokens/` — that figure
represents what your organisation has actually bought from the provider. A new
install starts at zero, so fund it before assigning anything.

The pool governs administrative distribution only. Tokens a student buys with a
plan are created by that purchase and credited directly, so a paying customer is
never blocked by an empty pool.

### Assigning tokens

*Assign* sets an exact balance, *Add* tops up, *Remove* takes back (clamped at
zero). **Bulk allocate** applies the same change to all active students, to those
who have run out, or to those running low — and stops, reporting where it got to,
if the pool runs dry mid-run.

```
Student A -> 50,000     Student B -> 100,000
Student C -> 25,000     Student D -> 200,000
```

Each student's balance is entirely separate.

### How the limit is enforced

The frontend mirrors the limit; it never decides it. Every request goes through
`chat_service`, in this order:

1. Account is active.
2. Model is in the catalogue, **active**, and inside its own allocation.
3. Balance is above zero, above `AI_MIN_REQUEST_TOKENS`, and large enough for the
   estimated prompt plus room for a reply.
4. Only then is the provider called.
5. Actual reported usage is charged — never an estimate from character counts.
6. The usage log, the per-model counters and the student's balance are updated.
7. The fresh balance is returned to the browser.

A failed or errored call charges nothing: the only debit happens after a response
with real usage numbers. Editing the balance in DevTools buys nothing, because
step 3 re-reads it from the database on the next request.

When a student runs out they get the upgrade path — *"You've reached your token
limit. Upgrade your plan to continue using AI."* with a button to the pricing
page — and a warning banner appears while they are merely running low.

---

## Operations: health and exports

Two administrator-only pages for running the platform rather than serving it.
Neither has a student-facing counterpart; both are behind `@admin_required` and
return a student to `/chat/` without confirming the page exists.

### System health — `/admin/health/`

`check_setup` answers "is this configured?" from a terminal, which is no help at
02:00 when something is wrong in production. This page answers the question an
administrator actually has — *is anything broken right now, and what do I do
about it* — as 21 checks in five groups.

| Group | Covers |
|---|---|
| **Security & deployment** | `DEBUG`, the secret key, `ALLOWED_HOSTS`, cookie security, database engine, how many administrators can still sign in |
| **AI provider** | Credentials, the failure rate over the last 24 hours, mean reply time — judged on the requests actually served, not on configuration alone |
| **Token supply** | Pool balance, tokens held by students, and a projected runway |
| **Integrations** | SMTP, Telegram, payment gateways, whether any plan is purchasable |
| **Waiting on a human** | Pending payments (flagged past 48 hours), open tickets, failed sign-ins, rate-limit trips, subscriptions about to lapse |

Every check carries a verdict, the value behind it and one line on what to do,
with a deep link to the page that fixes it. Failures are repeated at the top so
nothing needs scrolling for. When provider requests are failing, the codes
behind the rate are broken out in a table.

Three properties make it safe to open on a bad day:

* **No outbound calls.** Everything is read from settings and the database, so
  the page cannot cost money, hang on a slow provider or trip a rate limit.
  Reachability is inferred from the request log the app already keeps.
* **No secrets.** A credential is reported as present or absent, never shown.
* **It cannot crash.** Each probe is isolated — one that raises becomes a
  visible failing check with the exception type, and the rest of the page still
  renders.

The count of failing checks rides along in the sidebar on every console page.
It is cached for two minutes, so the badge costs nothing; the page itself always
runs the checks fresh.

**The runway forecast** projects the last 30 days of allocations forward. It
measures what was *given to students*, not what they then spent — the pool
empties when tokens leave it, whatever happens to them afterwards. Under a week
is a failure, under a month a warning.

### Data exports — `/admin/exports/`

Eight datasets as CSV: students, payments, the token ledger, pool movements, the
AI usage log, subscriptions, support tickets and the audit trail. One date range
applies to every card, and each shows the row count for that range before you
download — which is what stops someone fetching a 200,000-row file to discover
it was the wrong term.

| Property | How |
|---|---|
| **Streamed** | Rows are written to the response as they are read, through `StreamingHttpResponse`. A large export starts downloading at once and costs constant memory. |
| **Explicit columns** | Nothing is serialised by reflection. A field only leaves the system because it is named in `export_service.py` — which is what stops a future model change quietly adding a password hash to a file someone e-mails around. |
| **Audited** | Bulk personal data leaving the platform is consequential, so every download writes an audit entry with the dataset, range and row count, and is shown on the page itself. |
| **Never cached** | `Cache-Control: no-store, private`, so no proxy keeps a copy of a snapshot of live data. |

Cards carry a sensitivity note where one is warranted — the student export
contains names, e-mail addresses and phone numbers; the audit trail contains IP
addresses. Message content is never exported: the usage log is metadata only.

Dates are inclusive at both ends. "Up to the 31st" means the whole of the 31st.

---

## Live answers, search by meaning, notifications and staff roles

### Live answers

Answers are written into the chat word by word, as the model produces them.
Send, Regenerate and Edit all stream; a picture request does not (there are no
words to show) and arrives whole on the same connection.

- **Transport:** Server-Sent Events from the same endpoints
  (`/api/chat/send/` etc. with `stream=1`). The last event carries exactly
  the JSON the plain request returns, so saving, billing, follow-up
  suggestions and error handling are shared with the non-streaming path.
  The suggestion block is held back and never appears while writing.
- **The answer does not depend on the tab.** The model call runs on a worker
  thread (`AI/services/stream_service.py`). If the tab closes or the
  connection drops, the answer still finishes, is saved and is billed once;
  the page picks it up when it comes back, and a student who left gets a
  browser notification (below).
- **Stop is its own request** (`/api/chat/stream/<id>/stop/`). The provider
  closes its connection to the vendor, the part already written is kept, and
  only that part is charged. OpenAI reports usage only at the end of a
  stream, so a stopped answer's usage is estimated (about 4 characters per
  token); Anthropic's input count is its own. Stopped before the first word,
  nothing is saved and only the question is charged. Such rows are logged
  with `error_code="stopped"`.
- **Switches:** "Live answers" in Admin → Settings turns it off for everyone
  (answers then arrive whole). `CHAT_STREAM_THREADED` must be on in
  production and needs a threaded server — see *Production deployment*.
  Under the test runner the job runs inline.

### Search by meaning

The ⌘K palette finds past answers by what they mean, not only by the words
typed: "how do leaves make food" finds a chat about photosynthesis. Results
appear under **By meaning**, after the ordinary word matches, without
repeating a chat the words already found.

- Each answer is embedded together with the question before it and the chat
  title (`AI/services/embedding_service.py`), with an OpenAI-compatible
  `/embeddings` endpoint — `EMBEDDING_API_KEY` / `EMBEDDING_BASE_URL`, which
  default to the chat key and URL. Vectors are stored in the database
  (`MessageEmbedding`) and compared with numpy; no vector database is needed
  at this scale.
- New answers are indexed just before each search and by the hourly
  `send_reminders` job. Index everything once after deploying, and again
  after changing the model or dimensions:

  ```bash
  python manage.py index_embeddings            # everything not yet indexed
  python manage.py index_embeddings --rebuild  # after changing EMBEDDING_MODEL / _DIMENSIONS
  ```

- Only the student's own chats are searched; chats in Recently deleted are
  never indexed or returned; deleting a chat for good deletes its vectors.
- Switch: "Search by meaning" in Admin → Settings. Rate limit:
  `RL_SEARCH_LIMIT` / `RL_SEARCH_WINDOW`. System health shows indexed and
  pending counts.

### Browser notifications (Web Push)

Students can turn on notifications per device in **Settings → Notifications
on this device**. They arrive when the site is closed: an answer finished
while they were away, payment updates, support replies, low tokens, expiry
reminders and announcements — each obeying the category switches students
already have, plus "Tell me when an answer is ready".

1. `python manage.py generate_vapid_keys` and paste the three lines it prints
   (`VAPID_PUBLIC_KEY`, `VAPID_PRIVATE_KEY`, `VAPID_SUBJECT`) into `.env`.
2. Restart. Without keys the card says notifications are not set up; nothing
   else changes.

The service worker (`/sw.js`) only shows notifications and opens the chat
they are about; it caches nothing. On iPhone and iPad, Web Push needs iOS
16.4+ and the site added to the Home Screen — the card explains this. Devices
the push service reports as gone are removed automatically. OTPs and other
private details are never sent as notifications.

### Staff roles

Every administrator has a staff role, set on **Admin → Staff** (owners only):

| Role | Can use |
|---|---|
| **Owner** | Everything, including Staff, Settings and the audit log. Only owners create accounts, import or bulk-export. |
| **Support** | Students (read) and student care (notes, tags, messages, password reset, enable/disable), support tickets, saved replies, conversations. |
| **Finance** | Students (read), payments, gateways, subscriptions, plans, promos, reports, cost & margin, referrals and campaigns, granting plans. |
| **Content** | Announcements, Telegram channel posts, prompt library, answer quality. |
| **Operations** | Students (read), tokens and models, usage limits and analytics, model status, health, e-mail log, exports, Telegram setup. |

- Everyone has the dashboard, search and their own sign-in security.
- The matrix is data in `AI/services/staff_permissions.py`; one middleware
  enforces it for every console page, form post, JSON endpoint and download.
  A test fails if a console route is added without an area, and an unmapped
  route is owner-only.
- Menus, ⌘K, the bell, the dashboard and the student page show only what the
  role can use. A blocked URL shows a "no access" page naming owners to ask.
- Existing administrators became owners. An administrator with no role gets
  only the shared areas until an owner picks one — a forgotten role fails
  closed. A superuser is always an owner, and the last owner cannot step
  down. Every change is in the audit log.

---

## Security

| Area | Implementation |
|---|---|
| Passwords | PBKDF2 hashing; validated against Django's validators |
| Sessions | HttpOnly, SameSite=Lax, Secure in production; one active session per account |
| CSRF | Enabled site-wide; webhooks are exempt but authenticate their own callers |
| XSS | Django auto-escaping; the chat renderer escapes before converting markdown |
| SQL injection | Django ORM throughout — no raw SQL |
| Authorisation | `@admin_required` / `@login_required_student` on every protected view |
| Object-level access | Conversations, payments and receipts are always filtered by owner |
| Rate limiting | Database-backed sliding window on login, signup, OTP, AI and payments |
| File uploads | Decoded with Pillow, re-encoded to strip metadata, random server-side filename |
| OTP | Hashed at rest, single use, short expiry, attempt-capped |
| Payments | Signature-verified callbacks; idempotent crediting; never trusted from the browser |
| Secrets | Environment only — never in the database, never in the repository |
| Audit | Every consequential admin action recorded with actor, target, values and IP |

Production settings (`DEBUG=False`) switch on HSTS, SSL redirect, secure cookies
and the proxy SSL header automatically.

### Uploads

Filenames and `Content-Type` headers from the browser are never trusted. Each
image is decoded with Pillow, checked against an allow-list of real formats,
re-encoded (which strips EXIF, ICC and any appended payload), resized if
oversized, and stored under a random UUID filename. An SVG or a PHP file renamed
to `.png` is rejected — there is a test for exactly that.

---

## Testing

```bash
python manage.py test AI                       # everything
python manage.py test AI.tests.test_tokens     # one module
python manage.py test AI.tests -v 2            # verbose
```

| Module | Covers |
|---|---|
| `test_tokens` | Ledger correctness, clamping, idempotency, concurrent deduction |
| `test_auth` | Signup, login, disabled accounts, single session, OTP, password reset |
| `test_permissions` | Console lockout, cross-user data isolation |
| `test_payments` | Settlement, idempotency, refunds, webhook signature rejection |
| `test_chat` | Actual-usage billing, zero-token cutoff, failure handling, uploads |
| `test_growth` | Referrals, free trial, admin accounts, bulk import |
| `test_system` | Rate limits, alerts, expiry reminders, settings, audit |
| `test_support` | Tickets, replies, status transitions, admin assignment |
| `test_attachments` | Upload limits, document extraction, image handling |
| `test_console_data` | Conversations, transcripts, subscriptions |
| `test_console_ops` | Health checks and probe isolation, export contents, range filtering, export auditing |
| `test_frontend` | Markdown XSS safety (via Node), template integrity |

The front-end tests skip automatically if Node.js is not installed.

---

## Production deployment

### 1. Environment

```env
DEBUG=False
SECRET_KEY=<long random string>
ALLOWED_HOSTS=your-domain.com,www.your-domain.com
CSRF_TRUSTED_ORIGINS=https://your-domain.com
SITE_URL=https://your-domain.com
DATABASE_URL=postgres://user:password@host:5432/epc_ai
```

### 2. Prepare

One line at a time (PowerShell 5.1 has no `&&`):

```bash
pip install -r requirements.txt
python manage.py check --deploy
python manage.py migrate
python manage.py collectstatic --noinput
python manage.py createsuperuser
python manage.py check_setup
```

### 3. Serve

```bash
gunicorn config.wsgi:application --bind 0.0.0.0:8000 --workers 4 --worker-class gthread --threads 8   # Linux
waitress-serve --port=8000 --threads=16 config.wsgi:application                                     # Windows
```

Threads matter: a live answer keeps its request open while it is written
(and its model call runs on a thread of its own), so a server with one
thread per worker would serve one answer at a time. With a plain
`--workers 4` gunicorn, set `CHAT_STREAM_THREADED=False` or turn "Live
answers" off in Admin → Settings — answers then arrive whole, as before.

Put Nginx, Caddy or IIS in front to terminate TLS and serve `/static/` and
`/media/`. Nginx example:

```nginx
location /static/ { alias /srv/epc-ai/staticfiles/; expires 30d; }
location /media/  { alias /srv/epc-ai/media/; expires 7d; }
location / {
    proxy_pass http://127.0.0.1:8000;
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
    # Live answers: the app sends X-Accel-Buffering: no, which nginx obeys;
    # keep the read timeout above the longest answer.
    proxy_read_timeout 180s;
}
```

With more than one worker process, set `REDIS_URL` (below) so a Stop pressed
on one worker reaches an answer being written on another.

### 4. Shared cache (multi-worker)

Rate limiting works without Redis because it is database-backed, but a shared
cache keeps settings lookups fast across workers:

```env
REDIS_URL=redis://127.0.0.1:6379/0
```

### 5. Checklist

- [ ] `DEBUG=False` and a fresh `SECRET_KEY`
- [ ] `ALLOWED_HOSTS` and `CSRF_TRUSTED_ORIGINS` set
- [ ] PostgreSQL or MySQL, not SQLite
- [ ] HTTPS terminated in front of the app
- [ ] `python manage.py check --deploy` reports nothing
- [ ] `send_reminders` scheduled hourly
- [ ] Payment webhooks registered with each provider
- [ ] Telegram webhook registered
- [ ] Database backups configured
- [ ] `.env` is not in version control

---

## Management commands

| Command | What it does |
|---|---|
| `seed_demo` | Demo admin, students, plans, conversations, payments. `--reset` to start over. |
| `check_setup` | Reports which integrations are configured. Never prints a secret. |
| `send_reminders` | Expiry reminders, low-balance alerts, cleanup. `--dry-run` to preview. |
| `telegram_bot` | `--webhook`, `--poll`, `--delete-webhook`, `--status`. |
| `createsuperuser` | Create an administrator (a superuser is always an owner). |
| `generate_vapid_keys` | Keys for browser notifications; prints the three `.env` lines. |
| `index_embeddings` | Index answers for search by meaning. `--user`, `--limit`, `--rebuild`. |

---

## Troubleshooting

**"The AI service is not configured yet."**
`OPENAI_API_KEY` is missing. Run `python manage.py check_setup`, add the key to
`.env`, and restart.

**Verification codes never arrive.**
Without SMTP they are printed to the console running `runserver` — look there.
For Gmail you need an App Password, not the account password.

**Telegram says "This chat is not linked".**
The student must press **Start** in Telegram from their own profile deep link.
The bot cannot initiate the conversation. Check `telegram_bot --status`.

**A payment is stuck pending.**
Open it at `/admin/payments/<reference>/` and press *Re-check with provider*. If
the provider has no credentials, confirm it manually after checking the
student's uploaded proof.

**"database is locked" under load.**
SQLite serialising writes. Move to PostgreSQL or MySQL via `DATABASE_URL`.

**Everything renders without styling.**
`static/vendor/tailwind.js` is missing or not being served. Confirm it exists,
and that `collectstatic` has run in production.

**A student says they were signed out unexpectedly.**
Only one session is allowed per account — signing in elsewhere ends the previous
one. It can be turned off at `/admin/settings/` under Security.

---

## Licence

Proprietary — built for EPC.
