"""Resolve every {% url %} name used in the templates."""
import os, re, sys, pathlib
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
import django; django.setup()
from django.urls import reverse, NoReverseMatch

templates = pathlib.Path("templates")
names = set()
for path in templates.rglob("*.html"):
    text = path.read_text(encoding="utf-8")
    for name in re.findall(r"\{%\s*url\s+'([^']+)'", text):
        names.add((name, path.relative_to(templates).as_posix()))

bad = 0
for name, source in sorted(names):
    try:
        reverse(name)
    except NoReverseMatch as exc:
        if "arguments" in str(exc) or "keyword" in str(exc):
            continue  # needs args — the name itself exists
        bad += 1
        print(f"MISSING  {name}   (used in {source})")
print(f"{len(names)} url references checked, {bad} unresolvable")
