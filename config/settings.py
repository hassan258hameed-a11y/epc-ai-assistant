"""
Django settings for the EPC AI Assistant platform.

Environment driven: every secret and deployment-specific value is read from the
process environment (optionally seeded from a local ``.env`` file).  Nothing
sensitive is hard-coded here.
"""
from pathlib import Path
import os
import sys

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent

load_dotenv(BASE_DIR / ".env")


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
def env(key, default=None):
    value = os.environ.get(key)
    return default if value is None or value == "" else value


def env_bool(key, default=False):
    raw = env(key)
    if raw is None:
        return default
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def env_int(key, default=0):
    try:
        return int(str(env(key, default)).strip())
    except (TypeError, ValueError):
        return default


def env_float(key, default=0.0):
    try:
        return float(str(env(key, default)).strip())
    except (TypeError, ValueError):
        return default


def env_list(key, default=None):
    raw = env(key)
    if not raw:
        return list(default or [])
    return [item.strip() for item in raw.split(",") if item.strip()]


# --------------------------------------------------------------------------
# Core
# --------------------------------------------------------------------------
SECRET_KEY = env("SECRET_KEY", "django-insecure-dev-only-change-me-in-production")
DEBUG = env_bool("DEBUG", True)
ALLOWED_HOSTS = env_list("ALLOWED_HOSTS", ["localhost", "127.0.0.1", "[::1]"])
CSRF_TRUSTED_ORIGINS = env_list("CSRF_TRUSTED_ORIGINS", [])

if DEBUG and "testserver" not in ALLOWED_HOSTS:
    # Django's test client uses this host; only ever added in development.
    ALLOWED_HOSTS.append("testserver")

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django.contrib.humanize",
    "AI",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    # Project middleware
    "AI.middleware.SingleSessionMiddleware",
    "AI.middleware.AccountStatusMiddleware",
    "AI.middleware.StaffAreaMiddleware",
]

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "AI.context_processors.branding",
                "AI.context_processors.user_context",
                "AI.context_processors.console_scope",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"
ASGI_APPLICATION = "config.asgi.application"


# --------------------------------------------------------------------------
# Database
# --------------------------------------------------------------------------
# Development defaults to SQLite.  Set DATABASE_URL to move to
# PostgreSQL / MySQL without touching any application code:
#   postgres://user:pass@host:5432/dbname
#   mysql://user:pass@host:3306/dbname
DATABASE_URL = env("DATABASE_URL")

if DATABASE_URL:
    from urllib.parse import urlparse, unquote

    parsed = urlparse(DATABASE_URL)
    engines = {
        "postgres": "django.db.backends.postgresql",
        "postgresql": "django.db.backends.postgresql",
        "mysql": "django.db.backends.mysql",
        "sqlite": "django.db.backends.sqlite3",
    }
    engine = engines.get(parsed.scheme, "django.db.backends.postgresql")
    if engine.endswith("mysql"):
        # Shared hosting rarely has the headers mysqlclient compiles against;
        # PyMySQL is pure Python and stands in for it when it is missing.
        try:
            import MySQLdb  # noqa: F401
        except ImportError:
            try:
                import pymysql

                pymysql.install_as_MySQLdb()
            except ImportError:
                pass
    if engine.endswith("sqlite3"):
        DATABASES = {
            "default": {"ENGINE": engine, "NAME": BASE_DIR / (parsed.path.lstrip("/") or "db.sqlite3")}
        }
    else:
        DATABASES = {
            "default": {
                "ENGINE": engine,
                "NAME": unquote(parsed.path.lstrip("/")),
                "USER": unquote(parsed.username or ""),
                "PASSWORD": unquote(parsed.password or ""),
                "HOST": parsed.hostname or "",
                "PORT": str(parsed.port or ""),
                "CONN_MAX_AGE": env_int("DB_CONN_MAX_AGE", 60),
                "OPTIONS": {"charset": "utf8mb4"} if "mysql" in engine else {},
            }
        }
else:
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": BASE_DIR / "db.sqlite3",
            "OPTIONS": {"timeout": 20},
        }
    }

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
AUTH_USER_MODEL = "AI.User"


# --------------------------------------------------------------------------
# Authentication
# --------------------------------------------------------------------------
AUTHENTICATION_BACKENDS = ["AI.backends.EmailOrUsernameBackend"]

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator", "OPTIONS": {"min_length": 8}},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

PASSWORD_HASHERS = [
    "django.contrib.auth.hashers.PBKDF2PasswordHasher",
    "django.contrib.auth.hashers.PBKDF2SHA1PasswordHasher",
]

LOGIN_URL = "/login/"
LOGIN_REDIRECT_URL = "/chat/"  # the chat is the product
LOGOUT_REDIRECT_URL = "/"


# --------------------------------------------------------------------------
# Sessions & security
# --------------------------------------------------------------------------
SESSION_ENGINE = "django.contrib.sessions.backends.db"
SESSION_COOKIE_NAME = "epcai_session"
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Lax"
SESSION_COOKIE_AGE = env_int("SESSION_COOKIE_AGE", 60 * 60 * 24 * 7)
SESSION_EXPIRE_AT_BROWSER_CLOSE = False

CSRF_COOKIE_HTTPONLY = False  # the chat UI reads the token for fetch() calls
CSRF_COOKIE_SAMESITE = "Lax"

X_FRAME_OPTIONS = "DENY"
SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_REFERRER_POLICY = "same-origin"
SECURE_CROSS_ORIGIN_OPENER_POLICY = "same-origin"

# Production hardening — switched on automatically when DEBUG is off.
if not DEBUG:
    SESSION_COOKIE_SECURE = env_bool("SESSION_COOKIE_SECURE", True)
    CSRF_COOKIE_SECURE = env_bool("CSRF_COOKIE_SECURE", True)
    SECURE_SSL_REDIRECT = env_bool("SECURE_SSL_REDIRECT", True)
    SECURE_HSTS_SECONDS = env_int("SECURE_HSTS_SECONDS", 60 * 60 * 24 * 365)
    SECURE_HSTS_INCLUDE_SUBDOMAINS = True
    SECURE_HSTS_PRELOAD = True
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")


# --------------------------------------------------------------------------
# Internationalisation
# --------------------------------------------------------------------------
LANGUAGE_CODE = "en-us"
TIME_ZONE = env("TIME_ZONE", "Asia/Phnom_Penh")
USE_I18N = True
USE_TZ = True


# --------------------------------------------------------------------------
# Static & media
# --------------------------------------------------------------------------
STATIC_URL = "/static/"
STATICFILES_DIRS = [BASE_DIR / "static"]
# On shared hosting (cPanel) point both at folders inside the domain's
# document root, e.g. /home/<user>/yourdomain.com/static: Apache then serves
# them straight from disk and Python never sees those requests.
STATIC_ROOT = Path(env("STATIC_ROOT", "") or BASE_DIR / "staticfiles")

MEDIA_URL = "/media/"
MEDIA_ROOT = Path(env("MEDIA_ROOT", "") or BASE_DIR / "media")

FILE_UPLOAD_MAX_MEMORY_SIZE = 12 * 1024 * 1024
DATA_UPLOAD_MAX_MEMORY_SIZE = 26 * 1024 * 1024
DATA_UPLOAD_MAX_NUMBER_FIELDS = 2000


# --------------------------------------------------------------------------
# Cache
# --------------------------------------------------------------------------
REDIS_URL = env("REDIS_URL")
if REDIS_URL:
    CACHES = {"default": {"BACKEND": "django.core.cache.backends.redis.RedisCache", "LOCATION": REDIS_URL}}
else:
    CACHES = {
        "default": {
            "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
            "LOCATION": "epc-ai-assistant",
        }
    }


# --------------------------------------------------------------------------
# Email (OTP + notifications)
# --------------------------------------------------------------------------
EMAIL_HOST = env("EMAIL_HOST", "smtp.gmail.com")
EMAIL_PORT = env_int("EMAIL_PORT", 587)
EMAIL_HOST_USER = env("EMAIL_HOST_USER", "")
EMAIL_HOST_PASSWORD = env("EMAIL_HOST_PASSWORD", "")
EMAIL_USE_TLS = env_bool("EMAIL_USE_TLS", True)
EMAIL_USE_SSL = env_bool("EMAIL_USE_SSL", False)
EMAIL_TIMEOUT = env_int("EMAIL_TIMEOUT", 15)
DEFAULT_FROM_EMAIL = env("DEFAULT_FROM_EMAIL", "EPC AI Assistant <no-reply@epc-ai.local>")

if not EMAIL_HOST_USER:
    # No SMTP configured — print OTP mails to the console instead of failing.
    # Uses a UTF-8 safe console backend: a Windows terminal on a legacy code
    # page would otherwise raise UnicodeEncodeError on any non-ASCII character
    # and swallow the verification code entirely.
    EMAIL_BACKEND = "AI.mail.UnicodeSafeConsoleEmailBackend"
else:
    EMAIL_BACKEND = env("EMAIL_BACKEND", "django.core.mail.backends.smtp.EmailBackend")


# --------------------------------------------------------------------------
# AI providers
#
# Each vendor keeps its own credentials, its own endpoint and its own timeout.
# Nothing here is ever sent to the browser: students talk to this Django app,
# and only this app talks to OpenAI or Anthropic.
#
# ``AI_PROVIDER`` is only the fallback for a request that names no model; which
# vendor actually answers is decided per model by AI.services.model_router.
# --------------------------------------------------------------------------
AI_PROVIDER = env("AI_PROVIDER", "openai")

# --- OpenAI ---------------------------------------------------------------
OPENAI_API_KEY = env("OPENAI_API_KEY", "")
OPENAI_BASE_URL = env("OPENAI_BASE_URL", "https://api.openai.com/v1")
OPENAI_MODEL = env("OPENAI_MODEL", "gpt-4o-mini")
OPENAI_TIMEOUT = env_int("OPENAI_TIMEOUT", 60)
OPENAI_MAX_OUTPUT_TOKENS = env_int("OPENAI_MAX_OUTPUT_TOKENS", 2048)

#: A second OpenAI credential, so GPT-4o can bill to its own account rather
#: than sharing the one the rest of the OpenAI models use. Which models read
#: which setting is a field on the catalogue row (``AIModel.api_key_setting``),
#: so adding a third account is a row edit rather than a deploy. Blank falls
#: back to OPENAI_API_KEY.
OPENAI_GPT4O_API_KEY = env("OPENAI_GPT4O_API_KEY", "")

# --- Anthropic ------------------------------------------------------------
# Separate key, separate budget, separate failure domain: an Anthropic outage
# must leave the OpenAI models answering normally, and vice versa.
ANTHROPIC_API_KEY = env("ANTHROPIC_API_KEY", "")
# Blank means the SDK's own default endpoint. Set it only for a proxy.
ANTHROPIC_BASE_URL = env("ANTHROPIC_BASE_URL", "")
ANTHROPIC_MODEL = env("ANTHROPIC_MODEL", "claude-sonnet-5")
ANTHROPIC_TIMEOUT = env_int("ANTHROPIC_TIMEOUT", 60)
ANTHROPIC_MAX_OUTPUT_TOKENS = env_int("ANTHROPIC_MAX_OUTPUT_TOKENS", 4096)

AI_MAX_CONTEXT_MESSAGES = env_int("AI_MAX_CONTEXT_MESSAGES", 20)
#: The real limit on how much history is sent. A message count is a poor
#: proxy — eight one-line questions and eight pasted stack traces cost wildly
#: different amounts — so the window is sized in tokens and the count above is
#: only a ceiling on how many rows are even considered.
AI_CONTEXT_TOKEN_BUDGET = env_int("AI_CONTEXT_TOKEN_BUDGET", 1200)
#: Once this many messages have fallen out of the window without being
#: summarised, the summary is refreshed. Higher means fewer summary calls and
#: a staler summary.
AI_SUMMARY_REFRESH_AFTER = env_int("AI_SUMMARY_REFRESH_AFTER", 6)
#: How long a summary may be. It is re-sent on every subsequent request, so a
#: long one would defeat the purpose of having it.
AI_SUMMARY_MAX_TOKENS = env_int("AI_SUMMARY_MAX_TOKENS", 200)
AI_VISION_ENABLED = env_bool("AI_VISION_ENABLED", True)

# Sampling. 0.4 is deliberately below the 1.0 default: study answers should be
# accurate and repeatable rather than inventive, and a lower temperature also
# rambles less, which costs fewer output tokens for the same content.
AI_TEMPERATURE = env_float("AI_TEMPERATURE", 0.4)
# Small positive penalties discourage restating the question and padding an
# answer that is already finished.
AI_PRESENCE_PENALTY = env_float("AI_PRESENCE_PENALTY", 0.1)
AI_FREQUENCY_PENALTY = env_float("AI_FREQUENCY_PENALTY", 0.2)
#: Tokens a student must hold before a request is even attempted, so we never
#: start a call we cannot pay for. It has to sit at or below the low-balance
#: warning threshold: a floor above it would block the very messages the
#: warning says are still possible.
AI_MIN_REQUEST_TOKENS = env_int("AI_MIN_REQUEST_TOKENS", 50)

# --- Voice input ----------------------------------------------------------
# Server-side transcription, which detects the spoken language itself and
# returns text in that language. Set AI_VOICE_ENABLED=False to hide the
# microphone entirely.
AI_VOICE_ENABLED = env_bool("AI_VOICE_ENABLED", True)
#: The transcription model. Measured against ``gpt-4o-transcribe`` on English,
#: Urdu and Hindi: no accuracy difference worth having, and whisper-1 also
#: reports the detected language and the true audio duration — the first is
#: shown to the student, the second is what the per-minute charge is based on.
#: The newer family still works here; the request adapts to what it supports.
AI_TRANSCRIBE_MODEL = env("AI_TRANSCRIBE_MODEL", "whisper-1")
# Audio is billed by the second, not by tokens, so it is converted to this
# platform's single currency at a fixed rate. Students only ever see tokens.
AI_TRANSCRIBE_TOKENS_PER_MINUTE = env_int("AI_TRANSCRIBE_TOKENS_PER_MINUTE", 1500)
#: The shortest clip that is charged for at all. A recording is billed by its
#: real length above this — rounding a six-second question up to a full minute
#: made the cheapest thing a student can do the most expensive.
AI_TRANSCRIBE_MIN_SECONDS = env_int("AI_TRANSCRIBE_MIN_SECONDS", 3)
AI_TRANSCRIBE_MAX_SECONDS = env_int("AI_TRANSCRIBE_MAX_SECONDS", 120)
AI_TRANSCRIBE_MAX_BYTES = env_int("AI_TRANSCRIBE_MAX_BYTES", 10 * 1024 * 1024)

# --- Voice output ---------------------------------------------------------
# Spoken replies. The voice model is multilingual, so the same call reads back
# Urdu, Hindi and English without being told which one to expect.
AI_SPEECH_ENABLED = env_bool("AI_SPEECH_ENABLED", True)
AI_SPEECH_MODEL = env("AI_SPEECH_MODEL", "gpt-4o-mini-tts")
AI_SPEECH_VOICE = env("AI_SPEECH_VOICE", "alloy")
#: Speech is billed by characters spoken; converted to this platform's single
#: currency the same way transcription is.
AI_SPEECH_TOKENS_PER_1K_CHARS = env_int("AI_SPEECH_TOKENS_PER_1K_CHARS", 400)
AI_SPEECH_MAX_CHARS = env_int("AI_SPEECH_MAX_CHARS", 4000)

# --- Image generation -----------------------------------------------------
# None of the three chat models draws pictures, so a request for one is routed
# to a dedicated image endpoint rather than refused. It is an OpenAI service
# and is therefore paid for out of the student's OpenAI wallet — charging a
# Claude balance for it would be exactly the cross-provider leak the wallet
# design exists to prevent.
AI_IMAGE_ENABLED = env_bool("AI_IMAGE_ENABLED", True)
#: Blank so the vendor's own default wins. Setting it here pins every vendor to
#: one model id, which is only ever right for a single-vendor deployment.
AI_IMAGE_MODEL = env("AI_IMAGE_MODEL", "")

# Per-vendor credentials for the image providers. Each is the fallback for the
# key an administrator can also set in the console, so switching vendor does not
# mean retyping a key that is already on the server.
GEMINI_API_KEY = env("GEMINI_API_KEY", "")
GEMINI_BASE_URL = env("GEMINI_BASE_URL", "")
STABILITY_API_KEY = env("STABILITY_API_KEY", "")
STABILITY_BASE_URL = env("STABILITY_BASE_URL", "")
AI_IMAGE_SIZE = env("AI_IMAGE_SIZE", "1024x1024")
AI_IMAGE_QUALITY = env("AI_IMAGE_QUALITY", "medium")
AI_IMAGE_TIMEOUT = env_int("AI_IMAGE_TIMEOUT", 180)
#: Images are billed per picture, not per token. This is the conversion into
#: the one currency students actually see, and it tracks the quality actually
#: requested — a cheaper picture costs the student less, rather than every
#: picture costing the same regardless of what it cost us.
#:
#: ``medium`` is the default despite ``low`` being four times cheaper: at
#: ``low`` the model renders text as convincing gibberish, and a labelled
#: diagram with invented words on it is a request the student has to make
#: again. A wasted cheap image is more expensive than a usable one.
AI_IMAGE_TOKENS_PER_IMAGE = env_int("AI_IMAGE_TOKENS_PER_IMAGE", 1200)
AI_IMAGE_TOKENS_BY_QUALITY = {
    "low": env_int("AI_IMAGE_TOKENS_LOW", 350),
    "medium": AI_IMAGE_TOKENS_PER_IMAGE,
    "high": env_int("AI_IMAGE_TOKENS_HIGH", 4500),
}


# --------------------------------------------------------------------------
# Telegram
# --------------------------------------------------------------------------
TELEGRAM_BOT_TOKEN = env("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_BOT_USERNAME = env("TELEGRAM_BOT_USERNAME", "")
TELEGRAM_WEBHOOK_SECRET = env("TELEGRAM_WEBHOOK_SECRET", "")
TELEGRAM_TIMEOUT = env_int("TELEGRAM_TIMEOUT", 15)
# The public channel announcements are posted to ("AI Assistant Helps"):
# "@channel_username" for a public channel, or its numeric id ("-100…") for a
# private one. The bot must be an administrator there with "Post messages".
TELEGRAM_CHANNEL_ID = env("TELEGRAM_CHANNEL_ID", "")
# The invite/public link shown to students ("https://t.me/…"). Optional.
TELEGRAM_CHANNEL_URL = env("TELEGRAM_CHANNEL_URL", "")
# A community group, alongside (or instead of) the channel: announcements can
# be posted there too. Its numeric id ("-100…", shown in the console once the
# bot is added) or "@groupusername" for a public group. The bot must be a
# member allowed to send messages. Account details and codes never go there.
TELEGRAM_GROUP_ID = env("TELEGRAM_GROUP_ID", "")
# The group's invite link shown to students ("https://t.me/+…"). Optional.
TELEGRAM_GROUP_URL = env("TELEGRAM_GROUP_URL", "")
# How long a "Connect Telegram" link stays usable, in minutes.
TELEGRAM_LINK_TTL_MINUTES = env_int("TELEGRAM_LINK_TTL_MINUTES", 15)


# --------------------------------------------------------------------------
# Payment providers (credentials stay in the environment, never in the DB)
# --------------------------------------------------------------------------
SITE_URL = env("SITE_URL", "http://127.0.0.1:8000")

# ABA PayWay — e-commerce checkout. Sandbox until the production base URL and
# production merchant credentials are set.
ABA_PAYWAY_MERCHANT_ID = env("ABA_PAYWAY_MERCHANT_ID", "")
ABA_PAYWAY_API_KEY = env("ABA_PAYWAY_API_KEY", "")          # PayWay's "public key" (HMAC key)
ABA_PAYWAY_BASE_URL = env("ABA_PAYWAY_BASE_URL", "https://checkout-sandbox.payway.com.kh")
ABA_PAYWAY_CURRENCY = env("ABA_PAYWAY_CURRENCY", "USD")      # USD or KHR, as the merchant account allows
ABA_PAYWAY_PAYMENT_OPTION = env("ABA_PAYWAY_PAYMENT_OPTION", "")  # "" = every method; e.g. "abapay_khqr"
# "0" asks for PayWay's full hosted page. A merchant profile set up for the QR
# Payment API otherwise answers the purchase with raw KHQR JSON instead of a
# page. Sent unsigned (it is not in PayWay's hash order); blank leaves it out.
ABA_PAYWAY_PAYMENT_GATE = env("ABA_PAYWAY_PAYMENT_GATE", "0")
# The date on ABA's credential sheet (YYYY-MM-DD). ABA stops accepting a key
# after it, so the gateway page warns ahead of time.
ABA_PAYWAY_KEY_EXPIRES = env("ABA_PAYWAY_KEY_EXPIRES", "")

# ACLEDA Bank — XPAY e-commerce gateway (ACLEDA's hosted payment page: KHQR
# from any Cambodian bank app, or one tap into ACLEDA mobile). All six values
# come from ACLEDA's "Credential (Production) Document"; the base URL is the
# part of its openSessionV2 URL before "/XPAYConnectorServiceInterfaceImplV2".
ACLEDA_XPAY_BASE_URL = env("ACLEDA_XPAY_BASE_URL", "")
ACLEDA_XPAY_MERCHANT_ID = env("ACLEDA_XPAY_MERCHANT_ID", "")
ACLEDA_XPAY_LOGIN_ID = env("ACLEDA_XPAY_LOGIN_ID", "")        # "Remote login"
ACLEDA_XPAY_PASSWORD = env("ACLEDA_XPAY_PASSWORD", "")        # "Remote Password"
ACLEDA_XPAY_SECRET = env("ACLEDA_XPAY_SECRET", "")            # "Secret Code" — the HMAC key
ACLEDA_XPAY_MERCHANT_NAME = env("ACLEDA_XPAY_MERCHANT_NAME", "")  # defaults to the remote login
ACLEDA_XPAY_CURRENCY = env("ACLEDA_XPAY_CURRENCY", "USD")     # USD or KHR
ACLEDA_XPAY_TIMEOUT = env_int("ACLEDA_XPAY_TIMEOUT", 25)

# ACLEDA Bank — KHQR (Cambodia's national QR standard) paid into the merchant's
# Bakong account at ACLEDA, confirmed through the National Bank's Bakong Open API.
ACLEDA_KHQR_BAKONG_ID = env("ACLEDA_KHQR_BAKONG_ID", "")     # e.g. "yourname@aclb"
ACLEDA_KHQR_ACCOUNT_NAME = env("ACLEDA_KHQR_ACCOUNT_NAME", "EPC AI ASSISTANT")  # merchant name, ≤25 chars
ACLEDA_KHQR_CITY = env("ACLEDA_KHQR_CITY", "Phnom Penh")
ACLEDA_KHQR_ACCOUNT_NUMBER = env("ACLEDA_KHQR_ACCOUNT_NUMBER", "")  # optional, shown to the payer
ACLEDA_KHQR_MERCHANT_ID = env("ACLEDA_KHQR_MERCHANT_ID", "")  # set for a merchant (tag 30) KHQR
ACLEDA_KHQR_CURRENCY = env("ACLEDA_KHQR_CURRENCY", "KHR")
BAKONG_API_TOKEN = env("BAKONG_API_TOKEN", "")
BAKONG_API_BASE_URL = env("BAKONG_API_BASE_URL", "https://api-bakong.nbc.gov.kh")

# Binance Pay — merchant API. Orders are priced in USDT.
BINANCE_PAY_API_KEY = env("BINANCE_PAY_API_KEY", "")
BINANCE_PAY_API_SECRET = env("BINANCE_PAY_API_SECRET", "")
BINANCE_PAY_BASE_URL = env("BINANCE_PAY_BASE_URL", "https://bpay.binanceapi.com")
BINANCE_PAY_MERCHANT_ID = env("BINANCE_PAY_MERCHANT_ID", "")
BINANCE_PAY_CURRENCY = env("BINANCE_PAY_CURRENCY", "USDT")

# A local stand-in for all three gateways, speaking their documented APIs, so
# every flow can be clicked through end to end before credentials exist. Only
# ever mounted with DEBUG on; see AI/dev/payment_emulator.py.
PAYMENT_EMULATOR = env_bool("PAYMENT_EMULATOR", False) and DEBUG
if PAYMENT_EMULATOR:
    # Emulator mode is all or nothing: real keys against a fake gateway (or the
    # reverse) would only produce confusing failures.
    _emulator = SITE_URL.rstrip("/") + "/dev/payment-emulator"
    ABA_PAYWAY_MERCHANT_ID, ABA_PAYWAY_API_KEY = "ec-emulator", "emulator-aba-api-key"
    ABA_PAYWAY_BASE_URL = f"{_emulator}/aba"
    ACLEDA_KHQR_BAKONG_ID = ACLEDA_KHQR_BAKONG_ID or "epc_emulator@aclb"
    BAKONG_API_TOKEN, BAKONG_API_BASE_URL = "emulator-bakong-token", f"{_emulator}/bakong"
    BINANCE_PAY_API_KEY, BINANCE_PAY_API_SECRET = "emulator-binance-key", "emulator-binance-secret"
    BINANCE_PAY_BASE_URL = f"{_emulator}/binance"
    ACLEDA_XPAY_BASE_URL = f"{_emulator}/xpay"
    ACLEDA_XPAY_MERCHANT_ID, ACLEDA_XPAY_LOGIN_ID = "emulator-merchant=", "EMULATORMERCHANT"
    ACLEDA_XPAY_PASSWORD, ACLEDA_XPAY_SECRET = "EMULATORMERCHANT", "emulator-secret"
    ACLEDA_XPAY_MERCHANT_NAME = "EMULATORMERCHANT"

# When a provider has no live credentials the checkout falls back to a
# manual-review flow: the student submits proof, an admin confirms it.
PAYMENT_MANUAL_REVIEW = env_bool("PAYMENT_MANUAL_REVIEW", True)

# Pending orders are re-checked (and stale ones expired) at most once a minute
# from ordinary page loads, on a background thread, so a student who paid and
# closed the tab is still credited without a scheduler. Off under the test
# runner, where a stray thread would race the test database.
TESTING = len(sys.argv) > 1 and sys.argv[1] == "test"
PAYMENT_SWEEP_ON_REQUEST = env_bool("PAYMENT_SWEEP_ON_REQUEST", True) and not TESTING

if TESTING:
    # The test runner never sees a live payment credential. A test that needs
    # a gateway sets its own fake one, so no test can reach a real bank with
    # the keys in .env — whatever those keys are.
    ABA_PAYWAY_MERCHANT_ID = ABA_PAYWAY_API_KEY = ABA_PAYWAY_KEY_EXPIRES = ""
    ABA_PAYWAY_BASE_URL = "https://checkout-sandbox.payway.com.kh"
    ACLEDA_XPAY_BASE_URL = ACLEDA_XPAY_MERCHANT_ID = ACLEDA_XPAY_LOGIN_ID = ""
    ACLEDA_XPAY_PASSWORD = ACLEDA_XPAY_SECRET = ACLEDA_XPAY_MERCHANT_NAME = ""
    ACLEDA_KHQR_BAKONG_ID = BAKONG_API_TOKEN = ""
    BINANCE_PAY_API_KEY = BINANCE_PAY_API_SECRET = BINANCE_PAY_MERCHANT_ID = ""
    # And the choices that shape a charge are the code's defaults, whatever
    # .env says, so a test's expectations never depend on this machine.
    ABA_PAYWAY_CURRENCY, ACLEDA_XPAY_CURRENCY, ACLEDA_KHQR_CURRENCY = "USD", "USD", "KHR"
    ABA_PAYWAY_PAYMENT_OPTION, ABA_PAYWAY_PAYMENT_GATE = "", "0"
    # Nor a live Telegram bot: tests that need one set a fake token.
    TELEGRAM_BOT_TOKEN = TELEGRAM_BOT_USERNAME = TELEGRAM_CHANNEL_ID = TELEGRAM_GROUP_ID = ""

# --------------------------------------------------------------------------
# Live answers (streaming)
# --------------------------------------------------------------------------
# Answers are written word by word over Server-Sent Events. The model call runs
# on its own thread so an answer still finishes, is saved and billed when the
# tab closes (and the student gets a push notification). Under the test runner
# it runs inline, because a second thread cannot see a test's transaction.
# Production: a threaded server (gunicorn --worker-class gthread, waitress) and
# a proxy that does not buffer (nginx: the view sends X-Accel-Buffering: no).
CHAT_STREAM_THREADED = env_bool("CHAT_STREAM_THREADED", True) and not TESTING

# --------------------------------------------------------------------------
# Search by meaning (embeddings)
# --------------------------------------------------------------------------
# Uses an OpenAI-compatible /embeddings endpoint; defaults to the same key and
# base URL as chat. Vectors live in the database (AI.MessageEmbedding) and are
# compared in-process with numpy. Changing the model or dimensions means
# re-indexing: `python manage.py index_embeddings --rebuild`.
EMBEDDING_API_KEY = env("EMBEDDING_API_KEY", "") or OPENAI_API_KEY
EMBEDDING_BASE_URL = env("EMBEDDING_BASE_URL", "") or OPENAI_BASE_URL
EMBEDDING_MODEL = env("EMBEDDING_MODEL", "text-embedding-3-small")
EMBEDDING_DIMENSIONS = env_int("EMBEDDING_DIMENSIONS", 256)
EMBEDDING_TIMEOUT = env_int("EMBEDDING_TIMEOUT", 20)

# --------------------------------------------------------------------------
# Browser notifications (Web Push)
# --------------------------------------------------------------------------
# Generate a key pair once with `python manage.py generate_vapid_keys` and put
# the three lines it prints in .env. Without keys, the notification switch in
# Settings explains that the feature is not set up; nothing else changes.
VAPID_PUBLIC_KEY = env("VAPID_PUBLIC_KEY", "")
VAPID_PRIVATE_KEY = env("VAPID_PRIVATE_KEY", "")
VAPID_SUBJECT = env("VAPID_SUBJECT", "") or f"mailto:{env('SUPPORT_EMAIL', 'support@epc-ai.local')}"


# --------------------------------------------------------------------------
# Uploads
# --------------------------------------------------------------------------
UPLOAD_MAX_IMAGE_BYTES = env_int("UPLOAD_MAX_IMAGE_BYTES", 5 * 1024 * 1024)
UPLOAD_ALLOWED_IMAGE_TYPES = ["image/jpeg", "image/png", "image/webp", "image/gif"]
UPLOAD_MAX_IMAGE_DIMENSION = env_int("UPLOAD_MAX_IMAGE_DIMENSION", 2000)
# Documents attached to support tickets. Kept below the Django in-memory
# limits above so an upload never spills to a temporary file on disk.
UPLOAD_MAX_DOCUMENT_BYTES = env_int("UPLOAD_MAX_DOCUMENT_BYTES", 8 * 1024 * 1024)
# Ceiling across every file on one message.
UPLOAD_MAX_TOTAL_BYTES = env_int("UPLOAD_MAX_TOTAL_BYTES", 20 * 1024 * 1024)
# Files a student may attach to a single message.
UPLOAD_MAX_FILES_PER_MESSAGE = env_int("UPLOAD_MAX_FILES_PER_MESSAGE", 5)


# --------------------------------------------------------------------------
# Rate limits — (max hits, window in seconds)
# --------------------------------------------------------------------------
RATE_LIMITS = {
    "login": (env_int("RL_LOGIN_LIMIT", 8), env_int("RL_LOGIN_WINDOW", 300)),
    "signup": (env_int("RL_SIGNUP_LIMIT", 5), env_int("RL_SIGNUP_WINDOW", 3600)),
    "otp_send": (env_int("RL_OTP_SEND_LIMIT", 5), env_int("RL_OTP_SEND_WINDOW", 900)),
    "otp_verify": (env_int("RL_OTP_VERIFY_LIMIT", 10), env_int("RL_OTP_VERIFY_WINDOW", 900)),
    "ai_request": (env_int("RL_AI_LIMIT", 30), env_int("RL_AI_WINDOW", 60)),
    "transcribe": (env_int("RL_VOICE_LIMIT", 30), env_int("RL_VOICE_WINDOW", 300)),
    "password_reset": (env_int("RL_RESET_LIMIT", 5), env_int("RL_RESET_WINDOW", 3600)),
    "payment_create": (env_int("RL_PAYMENT_LIMIT", 10), env_int("RL_PAYMENT_WINDOW", 600)),
    # Search by meaning: one embedding request per search.
    "semantic_search": (env_int("RL_SEARCH_LIMIT", 30), env_int("RL_SEARCH_WINDOW", 60)),
}


# --------------------------------------------------------------------------
# Branding
# --------------------------------------------------------------------------
BRAND = {
    "name": env("BRAND_NAME", "AI Assistant"),
    "full_name": env("BRAND_FULL_NAME", "EPC AI Assistant"),
    "org": env("BRAND_ORG", "EPC"),
    "tagline": env("BRAND_TAGLINE", "Your study partner that never sleeps."),
    "support_email": env("SUPPORT_EMAIL", "support@epc-ai.local"),
    "support_telegram": env("SUPPORT_TELEGRAM", ""),
}

# Django's stock admin is a break-glass tool, not part of the product. The
# console at /admin/ covers everything; turn this on only to recover from a
# console outage.
DJANGO_ADMIN_ENABLED = env_bool("DJANGO_ADMIN_ENABLED", False)

MESSAGE_STORAGE = "django.contrib.messages.storage.session.SessionStorage"


# --------------------------------------------------------------------------
# Logging
# --------------------------------------------------------------------------
LOG_LEVEL = env("LOG_LEVEL", "DEBUG" if DEBUG else "INFO")
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "verbose": {"format": "[{asctime}] {levelname} {name}: {message}", "style": "{"},
    },
    "handlers": {
        "console": {"class": "logging.StreamHandler", "formatter": "verbose"},
    },
    "root": {"handlers": ["console"], "level": "WARNING"},
    "loggers": {
        "AI": {"handlers": ["console"], "level": LOG_LEVEL, "propagate": False},
        "django.request": {"handlers": ["console"], "level": "ERROR", "propagate": False},
    },
}
