import os, sys, django
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
django.setup()

from django.test import Client
from AI.models import User, Payment, Conversation, Plan, SupportTicket

student = User.objects.get(username="demo.sokha")
c = Client()
c.force_login(student)
User.objects.filter(pk=student.pk).update(active_session_key=c.session.session_key)

conv = Conversation.objects.filter(user=student).first()
paid = Payment.objects.filter(user=student, status="paid").first()
pending = Payment.objects.filter(status="pending").first()
plan = Plan.objects.filter(is_active=True).first()
ticket = SupportTicket.objects.filter(user=student).first()

urls = ["/dashboard/", "/billing/", "/usage/", "/plans/", "/referrals/", "/notifications/",
        "/profile/", "/help/", "/help/tickets/", "/chat/"]
if conv: urls.append(f"/chat/{conv.pk}/")
if ticket: urls.append(f"/help/tickets/{ticket.reference}/")
if plan: urls.append(f"/checkout/{plan.slug}/")
if paid: urls.append(f"/payments/{paid.reference}/receipt/")
if pending and pending.user_id == student.pk: urls.append(f"/payments/{pending.reference}/")
urls += ["/admin/", "/admin/users/"]   # must NOT be reachable as a student

for url in urls:
    try:
        r = c.get(url)
        print(f"{r.status_code:>3}  {url}" + (f"  -> {r.headers.get('Location','')}" if r.status_code in (301,302) else ""))
    except Exception as e:
        print(f"ERR  {url}  {type(e).__name__}: {e}")
