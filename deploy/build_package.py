"""Build the upload for cPanel shared hosting.

    python deploy/build_package.py --domain pensionsync.online \
        --db-name examproc_epcai --db-user examproc_epcai --db-pass "the password"

Writes two things into dist/:

* ``epc-ai-assistant.zip`` — the code, templates, static files and the
  exported configuration. No database, no uploads, no .env, no tests.
* ``.env.production`` — your current .env with what must change for the live
  site changed: DEBUG off, a new SECRET_KEY, the domain, the MySQL database,
  the folders Apache serves, and the payment emulator off. Every API key,
  payment key, Telegram and e-mail setting is carried over as it is.

Upload the .env separately and rename it to ``.env`` on the server. It holds
live keys: never put it inside public_html or the domain's folder.
"""
from __future__ import annotations

import argparse
import secrets
import sys
import zipfile
from pathlib import Path
from urllib.parse import quote

ROOT = Path(__file__).resolve().parent.parent
DIST = ROOT / "dist"

INCLUDE = ["AI", "config", "templates", "static", "manage.py", "passenger_wsgi.py", "requirements.txt",
           "deploy/live_config.json", "deploy/DEPLOY_CPANEL.md", ".env.example"]
SKIP_PARTS = {"__pycache__", "tests"}
SKIP_SUFFIXES = {".pyc", ".pyo", ".log", ".sqlite3", ".bak"}


def build_zip(target: Path) -> int:
    count = 0
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as archive:
        for entry in INCLUDE:
            path = ROOT / entry
            if not path.exists():
                sys.exit(f"Missing {entry} — run `python manage.py export_live_config` first?")
            files = [path] if path.is_file() else sorted(p for p in path.rglob("*") if p.is_file())
            for file in files:
                rel = file.relative_to(ROOT)
                if SKIP_PARTS.intersection(rel.parts) or file.suffix in SKIP_SUFFIXES:
                    continue
                archive.write(file, rel.as_posix())
                count += 1
    return count


def production_env(args) -> str:
    source = ROOT / ".env"
    if not source.exists():
        sys.exit("No .env next to manage.py to start from.")
    domain = args.domain.strip().lower().removeprefix("https://").removeprefix("http://").strip("/")
    home = args.home.rstrip("/")
    docroot = (args.docroot or f"{home}/{domain}").rstrip("/")
    db_url = (f"mysql://{quote(args.db_user, safe='')}:{quote(args.db_pass, safe='')}"
              f"@localhost:3306/{quote(args.db_name, safe='')}"
              if args.db_name else "mysql://DB_USER:DB_PASSWORD@localhost:3306/DB_NAME")
    overrides = {
        "DEBUG": "False",
        "SECRET_KEY": secrets.token_urlsafe(50),
        "ALLOWED_HOSTS": f"{domain},www.{domain}",
        "CSRF_TRUSTED_ORIGINS": f"https://{domain},https://www.{domain}",
        "SITE_URL": f"https://{domain}",
        "DATABASE_URL": db_url,
        "STATIC_ROOT": f"{docroot}/static",
        "MEDIA_ROOT": f"{docroot}/media",
        "PAYMENT_EMULATOR": "False",
        "SESSION_COOKIE_SECURE": "True",
        "CSRF_COOKIE_SECURE": "True",
        "SECURE_SSL_REDIRECT": "True",
        "LOG_LEVEL": "INFO",
    }
    lines, seen = [], set()
    for line in source.read_text(encoding="utf-8").splitlines():
        key = line.split("=", 1)[0].strip() if "=" in line and not line.lstrip().startswith("#") else ""
        if key in overrides:
            lines.append(f"{key}={overrides[key]}")
            seen.add(key)
        else:
            lines.append(line)
    missing = [k for k in overrides if k not in seen]
    if missing:
        lines += ["", "# --- Live site (added by deploy/build_package.py) ---"]
        lines += [f"{k}={overrides[k]}" for k in missing]
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--domain", required=True)
    parser.add_argument("--home", default="/home2/examproc", help="cPanel home directory (cPanel → General Information)")
    parser.add_argument("--docroot", help="The domain's document root, if not <home>/<domain>")
    parser.add_argument("--db-name", default="")
    parser.add_argument("--db-user", default="")
    parser.add_argument("--db-pass", default="")
    args = parser.parse_args()

    DIST.mkdir(exist_ok=True)
    zip_path = DIST / "epc-ai-assistant.zip"
    files = build_zip(zip_path)
    env_path = DIST / ".env.production"
    env_path.write_text(production_env(args), encoding="utf-8")

    size = zip_path.stat().st_size / 1024 / 1024
    print(f"{zip_path}  ({files} files, {size:.1f} MB)")
    print(f"{env_path}  (holds live keys: upload it separately, outside public folders)")
    if not args.db_name:
        print("Database left as a placeholder: edit DATABASE_URL in it, or re-run with --db-name/--db-user/--db-pass.")


if __name__ == "__main__":
    main()
