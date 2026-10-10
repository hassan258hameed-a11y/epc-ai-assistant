import os, sys, django, pathlib
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
django.setup()
from django.test import Client
from AI.models import User, Conversation, Payment, Plan

out = pathlib.Path(__file__).parent / "out"
out.mkdir(exist_ok=True)

student = User.objects.get(username="demo.sokha")
admin = User.objects.get(username="admin.demo")

sc = Client(); sc.force_login(student)
User.objects.filter(pk=student.pk).update(active_session_key=sc.session.session_key)
ac = Client(); ac.force_login(admin)
User.objects.filter(pk=admin.pk).update(active_session_key=ac.session.session_key)

conv = Conversation.objects.filter(user=student).first()
plan = Plan.objects.first()
pending = Payment.objects.filter(status="pending").first()

pages = {
  "chat.html": (sc, f"/chat/{conv.pk}/" if conv else "/chat/"),
  "student_dashboard.html": (sc, "/dashboard/"),
  "student_usage.html": (sc, "/usage/"),
  "student_plans.html": (sc, "/plans/"),
  "student_profile.html": (sc, "/profile/"),
  "student_referrals.html": (sc, "/referrals/"),
  "checkout.html": (sc, f"/checkout/{plan.slug}/"),
  "console_dashboard.html": (ac, "/admin/"),
  "console_users.html": (ac, "/admin/users/"),
  "console_user_detail.html": (ac, f"/admin/users/{student.pk}/"),
  "console_tokens.html": (ac, "/admin/tokens/"),
  "console_usage.html": (ac, "/admin/usage/"),
  "console_settings.html": (ac, "/admin/settings/"),
  "console_accounts.html": (ac, "/admin/accounts/"),
  "console_audit.html": (ac, "/admin/audit-logs/"),
  "console_health.html": (ac, "/admin/health/"),
  "console_exports.html": (ac, "/admin/exports/"),
}
if pending:
    pages["payment_detail.html"] = (Client(), f"/payments/{pending.reference}/")
    if pending.user_id == student.pk:
        pages["payment_detail.html"] = (sc, f"/payments/{pending.reference}/")

for name, (client, url) in pages.items():
    r = client.get(url)
    (out / name).write_bytes(r.content)
    print(f"{r.status_code}  {url}  -> {name} ({len(r.content)}b)")
