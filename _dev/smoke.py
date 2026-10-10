"""Developer smoke test — renders every page and reports the status code."""
import os, sys, django
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
django.setup()

from django.test import Client

def run(urls, client=None, label=""):
    client = client or Client()
    if label: print(f"--- {label} ---")
    ok = True
    for url in urls:
        try:
            r = client.get(url, follow=False)
            flag = "OK " if r.status_code < 400 else "!! "
            if r.status_code >= 400 and r.status_code not in (302, 404):
                ok = False
            print(f"{flag}{r.status_code}  {url}")
        except Exception as e:
            ok = False
            print(f"ERR      {url}  ->  {type(e).__name__}: {e}")
    return ok

if __name__ == "__main__":
    run(["/", "/plans/", "/refund-policy/", "/privacy/", "/terms/", "/login/", "/signup/",
         "/forgot-password/", "/admin/login/", "/nope/"], label="public")
