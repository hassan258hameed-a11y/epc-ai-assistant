import os, sys, django
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
django.setup()

from django.test import Client
from AI.models import User, Payment, Plan, Conversation, Subscription

admin = User.objects.get(username="admin.demo")
c = Client()
c.force_login(admin)
User.objects.filter(pk=admin.pk).update(active_session_key=c.session.session_key)

student = User.objects.filter(username="demo.sokha").first()
plan = Plan.objects.first()
payment = Payment.objects.first()

urls = ["/admin/", "/admin/users/", "/admin/users/?status=active&tokens=low",
        "/admin/accounts/", "/admin/accounts/sample.csv",
        "/admin/tokens/", "/admin/tokens/history/", "/admin/usage/", "/admin/usage/?range=30&status=ok",
        "/admin/plans/", "/admin/plans/new/", "/admin/payments/", "/admin/payments/?status=pending",
        "/admin/referrals/", "/admin/notifications/", "/admin/audit-logs/", "/admin/settings/",
        "/admin/conversations/", "/admin/conversations/?scope=expensive",
        "/admin/subscriptions/", "/admin/subscriptions/?status=live",
        "/admin/tokens/?tab=models", "/admin/tokens/?tab=activity", "/admin/tokens/?tab=analytics",
        "/admin/health/", "/admin/health/data/",
        "/admin/exports/", "/admin/exports/?from=2020-01-01&to=2030-01-01",
        "/admin/exports/students.csv", "/admin/exports/audit.csv"]
if student: urls += [f"/admin/users/{student.pk}/", f"/admin/users/{student.pk}/edit/"]
if plan: urls.append(f"/admin/plans/{plan.pk}/edit/")
if payment: urls.append(f"/admin/payments/{payment.reference}/")
conversation = Conversation.objects.first()
if conversation: urls.append(f"/admin/conversations/{conversation.pk}/")

fails = 0
for url in urls:
    try:
        r = c.get(url)
        ok = r.status_code < 400
        if not ok: fails += 1
        print(f"{'OK ' if ok else '!! '}{r.status_code}  {url}")
    except Exception as e:
        fails += 1
        print(f"ERR      {url}  ->  {type(e).__name__}: {e}")
print("\nFAILURES:", fails)
