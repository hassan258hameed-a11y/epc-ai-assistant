# Putting EPC AI Assistant live on cPanel shared hosting

For **pensionsync.online**, on the same cPanel account as examprocambodia.com.
The existing site is not touched at any step: the new site gets its own
domain folder, its own MySQL database and its own Python application, and the
code lives outside every public folder.

| | Existing site | New site |
|---|---|---|
| Domain | examprocambodia.com | pensionsync.online |
| Public folder | `/home2/examproc/public_html` | `/home2/examproc/pensionsync.online` |
| Code | — | `/home2/examproc/epc_ai` (not public) |
| Database | its own | `examproc_epcai` (new) |

Replace `/home2/examproc` everywhere if cPanel → *General Information* shows a
different **Home Directory**.

---

## 1. Point the domain at this hosting (Namecheap)

1. Namecheap → *Domain List* → **pensionsync.online** → *Manage*.
2. *Nameservers* → **Custom DNS** → enter the same two nameservers
   examprocambodia.com uses (open examprocambodia.com in the Domain List to
   copy them, or ask the host).
3. Save. It usually works within an hour (allow up to 24).

## 2. Add the domain in cPanel

1. cPanel → **Domains** → **Create A New Domain**.
2. Domain: `pensionsync.online`.
3. **Untick** “Share document root with examprocambodia.com”. This is what
   keeps the two sites apart.
4. Document root: `pensionsync.online` (becomes `/home2/examproc/pensionsync.online`).
5. Submit.
6. Once the domain resolves: cPanel → **SSL/TLS Status** → tick
   pensionsync.online and www.pensionsync.online → **Run AutoSSL**.
   The site must have its padlock before going further: payments, Telegram and
   secure sign-in all need https.

## 3. Create the database

cPanel → **MySQL® Database Wizard**:

1. Database name: `epcai` (cPanel shows it as `examproc_epcai`).
2. User: `epcai` (shown as `examproc_epcai`), with a long generated password.
   Copy the password somewhere safe.
3. Privileges: **ALL PRIVILEGES** → *Make Changes*.

## 4. Build the upload (on your computer)

In the project folder:

```
python manage.py export_live_config
python deploy/build_package.py --domain pensionsync.online --db-name examproc_epcai --db-user examproc_epcai --db-pass "THE PASSWORD"
```

This creates in `dist/`:

* `epc-ai-assistant.zip` — the site (no database, no test uploads, no secrets);
* `.env.production` — your settings for the live site, **including live keys**.

## 5. Upload

cPanel → **File Manager**:

1. *Settings* (top right) → tick **Show Hidden Files** → Save.
2. In the home folder (`/home2/examproc`, *not* public_html) → **+ Folder** → `epc_ai`.
3. Open `epc_ai` → **Upload** `epc-ai-assistant.zip` → back → right-click it →
   **Extract** into `/home2/examproc/epc_ai`. Then delete the zip.
4. Upload `.env.production` into the same folder and **rename it to `.env`**.
   It must sit next to `manage.py`, never inside a public folder.

## 6. Create the Python application

cPanel → **Setup Python App** → **Create Application**:

| Field | Value |
|---|---|
| Python version | **3.11** (3.10 or newer; Django 5.2 needs it) |
| Application root | `epc_ai` |
| Application URL | `pensionsync.online` |
| Application startup file | `passenger_wsgi.py` |
| Application Entry point | `application` |

**Create**.

> **Important:** creating (or editing) the app makes cPanel overwrite
> `passenger_wsgi.py` with its own template, which loads itself and crashes
> with "maximum recursion depth exceeded" (a 500 page). Straight after
> creating the app, put ours back: in Terminal, inside `epc_ai`,
> ```
> cat > passenger_wsgi.py <<'EOF'
> import os
> import sys
>
> sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
> os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
> os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")
>
> from config.wsgi import application  # noqa: E402,F401
> EOF
> ```
> (Extracting a new upload over the folder also restores it.)

At the top of the app's page cPanel shows a command like
`source /home2/examproc/virtualenv/epc_ai/3.11/bin/activate && cd /home2/examproc/epc_ai` — copy it.

## 7. Install and set up (cPanel → Terminal)

Paste the command you copied, then run these one at a time:

```
pip install --upgrade pip
pip install -r requirements.txt
python manage.py check --deploy
python manage.py migrate
python manage.py createsuperuser
python manage.py import_live_config deploy/live_config.json
python manage.py collectstatic --noinput
mkdir -p /home2/examproc/pensionsync.online/media
```

* `createsuperuser` makes the **owner** account you sign in to the console with.
* `import_live_config` brings your plans, models, prices, prompt library and
  settings (no test students, chats or payments).

If cPanel has no **Terminal**, ask the host to enable SSH/Terminal; the pip
step can also be done from *Setup Python App* → *Run Pip Install*.

Back in **Setup Python App** → your app → **Restart**.

## 8. Check it

* https://pensionsync.online — the home page, with a padlock.
* https://pensionsync.online/admin/login/ — sign in as the owner.
* Console → **Settings** → check the support e-mail, then the other groups.
* Sign up as a test student: the code e-mail arrives, and the account gets
  3,000 GPT-5 Mini tokens.

## 9. Scheduled jobs (cPanel → Cron Jobs)

*Common settings*: **Once per ten minutes** (`*/10 * * * *`). Command:

```
cd /home2/examproc/epc_ai && /home2/examproc/virtualenv/epc_ai/3.11/bin/python manage.py send_reminders >/dev/null 2>&1
```

This publishes scheduled announcements, confirms pending payments, sends
expiry and low-balance alerts and the daily admin digest, and cleans up.

## 10. Telegram: switch to the live site

One bot can only be connected to one place:

1. On your computer, **stop** `python manage.py telegram_bot --poll` and don't
   run it again (the local site stops receiving bot messages after this).
2. Live console → **Telegram** → **Register webhook**. Students' `/start`,
   `/status` and linking now reach the live site directly. No terminal stays open.
3. **Set command menu** once more, then **Re-check**.

## 11. Payments

The banks are told where to send results on every order (from `SITE_URL`), so
nothing changes in their portals:
`https://pensionsync.online/webhooks/payments/aba/`, `…/acleda/`, `…/binance/`.

* **ABA PayWay** checks which website a payment comes from. If the checkout
  page shows a domain error, ask ABA merchant support to add
  `pensionsync.online` to your merchant profile.
* **ACLEDA XPay**: do one small real payment and check it confirms.
* Console → **Payment gateways** shows each gateway's status.

## 12. E-mail

Codes are sent through Gmail (supportepcaiassistant@gmail.com). Some shared
hosts block outgoing mail to Gmail's server. Test with **Forgot password**.
If no e-mail arrives, cPanel → **Email Accounts** → create
`support@pensionsync.online`, and set in `.env`:

```
EMAIL_HOST=mail.pensionsync.online
EMAIL_PORT=465
EMAIL_USE_TLS=False
EMAIL_USE_SSL=True
EMAIL_HOST_USER=support@pensionsync.online
EMAIL_HOST_PASSWORD=that mailbox's password
DEFAULT_FROM_EMAIL=EPC AI Assistant <support@pensionsync.online>
```

then **Restart** the app.

## Updating the live site later

1. On your computer: `python deploy/build_package.py --domain pensionsync.online` (no DB flags
   needed, and ignore the new `.env.production`: the server keeps its own `.env`).
2. Upload the zip to `/home2/examproc/epc_ai`, extract it over the old files (overwrite).
3. Terminal: activate (step 6), then
   `pip install -r requirements.txt && python manage.py migrate && python manage.py collectstatic --noinput`.
4. **Setup Python App → Restart.**

## If something goes wrong

* **“Incomplete response” / 500**: Setup Python App → your app → open the log
  file it names, or in Terminal run `python manage.py check --deploy`.
* **Pages load without styling**: run `collectstatic` again and check
  `STATIC_ROOT` in `.env` is `/home2/examproc/pensionsync.online/static`.
* **“Bad Request (400)”**: `ALLOWED_HOSTS` in `.env` must list the domain.
* **Redirect loop**: if you put Cloudflare in front, set its SSL mode to *Full*.
* **Answers appear all at once instead of word by word**: the host is buffering
  the stream. Everything still works; ask the host to disable response
  buffering/compression for `text/event-stream`.
