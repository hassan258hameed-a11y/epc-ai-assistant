"""URL map for the AI app.

Namespaces:
  public:*   marketing and legal pages
  auth:*     sign-in, sign-up, verification
  student:*  the student product
  payments:* checkout and receipts
  console:*  the admin console, mounted at /admin/
"""
from __future__ import annotations

from django.urls import path

from AI.views.search import search_meaning

from AI.views import (
    auth_views,
    chat,
    console,
    console_ai,
    console_bulk,
    console_canned,
    console_data,
    console_gateways,
    console_insights,
    console_library,
    console_promos,
    console_referrals,
    console_reports,
    console_search,
    console_staff,
    console_status,
    console_student_tools,
    console_limits,
    console_ops,
    console_support,
    console_telegram,
    console_tokens,
    panels,
    payments,
    public,
    student,
    study,
    support,
)

# --------------------------------------------------------------------------
public_patterns = (
    [
        path("", public.home, name="home"),
        path("plans/", public.plans, name="plans"),
        path("refund-policy/", public.refund_policy, name="refund_policy"),
        path("privacy/", public.privacy, name="privacy"),
        path("terms/", public.terms, name="terms"),
        path("share/<str:token>/", public.shared_chat, name="shared_chat"),
    ],
    "public",
)

auth_patterns = (
    [
        path("login/", auth_views.login_view, name="login"),
        path("signup/", auth_views.signup, name="signup"),
        path("logout/", auth_views.logout_view, name="logout"),
        path("otp/", auth_views.otp_verify, name="otp"),
        path("otp/resend/", auth_views.otp_resend, name="otp_resend"),
        path("forgot-password/", auth_views.forgot_password, name="forgot_password"),
        path("reset-password/", auth_views.reset_password, name="reset_password"),
        path("admin/login/", auth_views.admin_login, name="admin_login"),
    ],
    "auth",
)

student_patterns = (
    [
        # ------------------------------------------------------------------
        # The application shell.
        #
        # The chat *is* the product, so everything else is a panel inside it
        # rather than a page of its own: same sidebar, same header, same
        # theme, only the content region changes. Each of these answers with
        # a bare fragment when the client asks for one and with the whole
        # shell otherwise, so a click, a refresh and a bookmark all land in
        # the same place.
        # ------------------------------------------------------------------
        path("app/usage/", panels.usage, name="usage"),
        path("app/usage/export/", panels.usage_export, name="usage_export"),
        path("app/pricing/", panels.pricing, name="pricing"),
        path("app/help/", panels.help_panel, name="help"),
        path("app/help/request/", panels.help_submit, name="submit_request"),
        path("app/profile/", panels.profile, name="profile"),
        path("app/settings/", panels.account_settings, name="settings"),
        path("app/settings/export/", panels.account_export, name="account_export"),
        path("app/settings/close/", panels.account_close, name="account_close"),
        path("app/billing/", panels.billing, name="billing"),
        path("app/notifications/", panels.notifications, name="notifications"),
        path("app/notifications/read-all/", panels.notifications_read_all, name="notifications_read_all_panel"),
        path("app/prompts/", panels.prompts, name="prompts"),
        path("app/saved/", panels.saved_answers, name="saved"),
        path("app/archived/", panels.archived_chats, name="archived"),
        path("app/trash/", panels.trash, name="trash"),
        path("app/models/", panels.models_compare, name="models"),
        path("app/study/", study.study_panel, name="study"),
        path("api/study/generate/", study.study_generate, name="study_generate"),
        path("api/study/<int:pk>/", study.study_detail, name="study_detail"),
        path("api/study/<int:pk>/attempt/", study.study_attempt, name="study_attempt"),

        # The standalone pages these replaced. Kept so existing links and
        # bookmarks keep working — they resolve to the same content in the
        # place it now lives.
        path("dashboard/", panels.legacy_redirect("usage"), name="dashboard"),
        path("billing/", panels.legacy_redirect("billing"), name="billing_legacy"),
        path("usage/", panels.legacy_redirect("usage"), name="usage_legacy"),
        path("help/", panels.legacy_redirect("help"), name="help_legacy"),
        path("notifications/", panels.legacy_redirect("notifications"), name="notifications_legacy"),
        path("profile/", panels.legacy_redirect("profile"), name="profile_legacy"),

        # Support tickets keep their own pages: a ticket thread is a document
        # with its own URL, not a region of the shell.
        path("help/tickets/", support.ticket_list, name="tickets"),
        path("help/tickets/<str:reference>/", support.ticket_detail, name="ticket_detail"),
        path("help/tickets/<str:reference>/reply/", support.ticket_reply, name="ticket_reply"),
        path("help/tickets/<str:reference>/close/", support.ticket_close, name="ticket_close"),
        path("referrals/", student.referrals, name="referrals"),
        path("notifications/<int:pk>/read/", student.notification_read, name="notification_read"),
        path("notifications/<int:pk>/delete/", student.notification_delete, name="notification_delete"),
        path("api/notifications/", student.notifications_feed, name="notifications_feed"),
        path("api/notifications/read-all/", student.notifications_read_all, name="notifications_read_all"),
        # Chat
        path("chat/", chat.chat, name="chat"),
        path("chat/<int:conversation_id>/", chat.chat, name="chat_detail"),
        path("api/chat/send/", chat.send, name="chat_send"),
        path("api/chat/stream/<str:stream_id>/stop/", chat.stream_stop, name="chat_stream_stop"),
        path("api/chat/stream/<str:stream_id>/presence/", chat.stream_presence, name="chat_stream_presence"),
        path("api/chat/new/", chat.new_conversation, name="chat_new"),
        path("api/chat/search/", chat.search_conversations, name="chat_search"),
        path("api/chat/search/meaning/", search_meaning, name="chat_search_meaning"),
        path("api/chat/<int:conversation_id>/messages/", chat.conversation_messages, name="chat_messages"),
        path("api/chat/<int:conversation_id>/rename/", chat.rename_conversation, name="chat_rename"),
        path("api/chat/<int:conversation_id>/delete/", chat.delete_conversation, name="chat_delete"),
        path("api/chat/<int:conversation_id>/restore/", chat.restore_conversation, name="chat_restore"),
        path("api/chat/<int:conversation_id>/purge/", chat.purge_conversation, name="chat_purge"),
        path("api/chat/<int:conversation_id>/messages/<int:message_id>/branch/", chat.branch_conversation,
             name="chat_branch"),
        path("api/chat/<int:conversation_id>/pin/", chat.pin_conversation, name="chat_pin"),
        path("api/chat/<int:conversation_id>/archive/", chat.archive_conversation, name="chat_archive"),
        path("chat/<int:conversation_id>/export/", chat.export_conversation, name="chat_export"),
        path("api/chat/<int:conversation_id>/regenerate/", chat.regenerate, name="chat_regenerate"),
        path("api/chat/<int:conversation_id>/messages/<int:message_id>/edit/", chat.edit_message, name="chat_edit"),
        path("api/chat/balance/", chat.balance, name="chat_balance"),
        path("api/chat/transcribe/", chat.transcribe, name="chat_transcribe"),
        path("api/chat/speak/", chat.speak, name="chat_speak"),
        path("api/chat/messages/<int:message_id>/feedback/", chat.feedback, name="chat_feedback"),
        path("api/prompts/", chat.prompts_api, name="prompts_api"),
        path("api/chat/<int:conversation_id>/share/", chat.share_api, name="chat_share"),
        path("api/chat/<int:conversation_id>/folder/", chat.conversation_folder, name="chat_folder"),
        path("api/chat/messages/<int:message_id>/bookmark/", chat.bookmark, name="chat_bookmark"),
        path("api/folders/", chat.folders_api, name="folders_api"),
        path("api/folders/<int:folder_id>/", chat.folder_api, name="folder_api"),
        path("api/prompts/<int:prompt_id>/use/", chat.prompt_use, name="prompt_use"),
    ],
    "student",
)

payment_patterns = (
    [
        path("checkout/<slug:slug>/", payments.checkout, name="checkout"),
        path("checkout/<slug:slug>/promo/", payments.promo_check, name="promo_check"),
        path("payments/<str:reference>/", payments.detail, name="detail"),
        path("payments/<str:reference>/pay/", payments.pay, name="pay"),
        path("payments/<str:reference>/proof/", payments.submit_proof, name="submit_proof"),
        path("payments/<str:reference>/status/", payments.check_status, name="status"),
        path("payments/<str:reference>/cancel/", payments.cancel, name="cancel"),
        path("payments/<str:reference>/receipt/", payments.receipt, name="receipt"),
        path("payments/<str:reference>/return/", payments.provider_return, name="provider_return"),
    ],
    "payments",
)

# The referral landing link. Short on purpose — it goes on posters and into
# messages, where /r/AB12CD34/ survives being retyped and a long query string
# does not.
referral_patterns = [
    path("r/<str:code>/", public.referral_landing, name="referral_landing"),
]

# Browser notifications (Web Push). The worker and the manifest must be served
# from the site root: a service worker only controls pages at or below its own
# path, so it cannot live under /static/.
from AI.views import push as push_views  # noqa: E402

push_patterns = (
    [
        path("sw.js", push_views.service_worker, name="sw"),
        path("manifest.webmanifest", push_views.manifest, name="manifest"),
        path("api/push/key/", push_views.key, name="key"),
        path("api/push/subscribe/", push_views.subscribe, name="subscribe"),
        path("api/push/unsubscribe/", push_views.unsubscribe, name="unsubscribe"),
        path("api/push/test/", push_views.test, name="test"),
    ],
    "push",
)

console_patterns = (
    [
        path("", console.dashboard, name="dashboard"),
        path("search/", console_search.search, name="search"),
        path("reports/", console_reports.reports, name="reports"),
        path("reports/export/", console_reports.reports_export, name="reports_export"),
        # Students
        path("users/", console.users, name="users"),
        path("users/bulk/", console_bulk.users_bulk, name="users_bulk"),
        path("users/<int:pk>/", console.user_detail, name="user_detail"),
        path("users/<int:pk>/notes/", console_student_tools.note_add, name="student_note_add"),
        path("users/<int:pk>/notes/<int:note_id>/", console_student_tools.note_action, name="student_note_action"),
        path("users/<int:pk>/tags/", console_student_tools.tags_save, name="student_tags"),
        path("users/<int:pk>/message/", console_student_tools.message, name="student_message"),
        path("users/<int:pk>/grant-plan/", console_student_tools.grant_plan, name="student_grant_plan"),
        path("users/<int:pk>/sign-out/", console_student_tools.sign_out, name="student_sign_out"),
        path("low-tokens/<int:wallet_id>/warn/", console_student_tools.warn_now, name="low_token_warn"),
        path("expiring/<int:subscription_id>/", console_student_tools.expiring_action, name="expiring_action"),
        path("users/<int:pk>/edit/", console.user_edit, name="user_edit"),
        path("users/<int:pk>/toggle/", console.user_toggle, name="user_toggle"),
        path("users/<int:pk>/reset-password/", console.user_reset_password, name="user_reset_password"),
        # Account provisioning
        path("accounts/", console.accounts, name="accounts"),
        path("accounts/sample.csv", console.accounts_sample_csv, name="accounts_sample"),
        path("accounts/check-username/", console.accounts_check_username, name="accounts_check_username"),
        path("accounts/import/preview/", console.accounts_import_preview, name="accounts_import_preview"),
        path("accounts/import/confirm/", console.accounts_import_confirm, name="accounts_import_confirm"),
        path("accounts/import/clear/", console.accounts_import_clear, name="accounts_import_clear"),
        path("accounts/import/result/", console.accounts_import_result, name="accounts_import_result"),
        # AI token management — per-model supply, distribution and audit history.
        # Tokens are a separate currency per model, so this is where all
        # distribution happens; the legacy /tokens/ page below is the analytics
        # and catalogue view over the same data.
        path("ai-tokens/", console_ai.ai_tokens, name="ai_tokens"),
        path("ai-tokens/distribute/", console_ai.distribute, name="ai_distribute"),
        path("ai-tokens/distribute/preview/", console_ai.distribute_preview, name="ai_distribute_preview"),
        path("ai-tokens/distribute/apply/", console_ai.distribute_submit, name="ai_distribute_apply"),
        path("ai-tokens/distribute/bulk/", console_ai.bulk_distribute, name="ai_distribute_bulk"),
        path("ai-tokens/pool/", console_ai.pool_topup, name="ai_pool_topup"),
        path("ai-tokens/adjust/", console_ai.adjust_wallet, name="ai_adjust"),
        path("ai-tokens/student/<int:pk>/", console_ai.student_wallets, name="ai_student"),
        path(
            "ai-tokens/student/<int:pk>/model/<int:model_pk>/access/",
            console_ai.toggle_access, name="ai_toggle_access",
        ),
        path(
            "ai-tokens/student/<int:pk>/model/<int:model_pk>/recalculate/",
            console_ai.recalculate_wallet, name="ai_recalculate",
        ),
        path("ai-tokens/provider/<int:pk>/toggle/", console_ai.provider_toggle, name="ai_provider_toggle"),
        # Token management — students, allocations, AI models, activity, analytics
        path("tokens/", console_tokens.token_management, name="tokens"),
        path("tokens/assign/", console_tokens.assign_tokens, name="tokens_assign"),
        path("tokens/assign/bulk/", console_tokens.bulk_assign_tokens, name="tokens_bulk_assign"),
        path("tokens/pool/", console_tokens.pool_topup, name="tokens_pool_topup"),
        path("tokens/student/<int:pk>/", console_tokens.student_token_detail, name="tokens_student"),
        path("tokens/history/", console.token_history, name="token_history"),
        # AI model catalogue
        path("models/new/", console_tokens.model_save, name="model_create"),
        path("models/<int:pk>/save/", console_tokens.model_save, name="model_save"),
        path("models/<int:pk>/toggle/", console_tokens.model_toggle, name="model_toggle"),
        path("models/<int:pk>/default/", console_tokens.model_default, name="model_default"),
        path("models/<int:pk>/allocate/", console_tokens.model_allocate, name="model_allocate"),
        path("models/recalculate/", console_tokens.models_recalculate, name="models_recalculate"),
        # Who holds what, and how fast they may spend it.
        path("usage-limits/", console_limits.usage_limits, name="usage_limits"),
        # What students think of the answers, and the prompts they all see.
        path("quality/", console_library.quality, name="quality"),
        path("quality/<int:pk>/review/", console_library.feedback_review, name="feedback_review"),
        path("prompts/", console_library.prompt_library, name="prompt_library"),
        path("prompts/new/", console_library.prompt_save, name="prompt_create"),
        path("prompts/<int:pk>/save/", console_library.prompt_save, name="prompt_save"),
        path("prompts/<int:pk>/toggle/", console_library.prompt_toggle, name="prompt_toggle"),
        path("prompts/<int:pk>/delete/", console_library.prompt_delete, name="prompt_delete"),

        # Pages that answer "who needs attention, and what is it costing?".
        path("students/low-tokens/", console_insights.low_tokens, name="low_tokens"),
        path("students/expiring/", console_insights.expiring, name="expiring_tokens"),
        path("emails/", console_insights.emails, name="emails"),
        path("emails/<int:pk>/retry/", console_insights.email_retry, name="email_retry"),
        path("models/costs/", console_insights.model_costs, name="model_costs"),
        path("models/costs/export.csv", console_insights.model_costs_export, name="model_costs_export"),
        # Analytics
        path("usage/", console.usage, name="usage"),
        # Plans
        path("plans/", console.plans, name="plans"),
        path("plans/new/", console.plan_create, name="plan_create"),
        path("plans/<int:pk>/edit/", console.plan_edit, name="plan_edit"),
        path("plans/<int:pk>/toggle/", console.plan_toggle, name="plan_toggle"),
        # Promo codes
        path("promos/", console_promos.promos, name="promos"),
        path("promos/new/", console_promos.promo_form, name="promo_create"),
        path("promos/<int:pk>/", console_promos.promo_detail, name="promo_detail"),
        path("promos/<int:pk>/edit/", console_promos.promo_form, name="promo_edit"),
        path("promos/<int:pk>/toggle/", console_promos.promo_toggle, name="promo_toggle"),
        # Payments
        path("payments/", console.payments, name="payments"),
        path("payment-gateways/", console_gateways.gateways, name="gateways"),
        path("payment-gateways/<str:key>/test/", console_gateways.gateway_test, name="gateway_test"),
        path("payments/<str:reference>/", console.payment_detail, name="payment_detail"),
        path("payments/<str:reference>/action/", console.payment_action, name="payment_action"),
        # Conversations
        path("conversations/", console_data.conversations, name="conversations"),
        path("conversations/<int:pk>/", console_data.conversation_detail, name="conversation_detail"),
        path("conversations/<int:pk>/delete/", console_data.conversation_delete, name="conversation_delete"),
        path("conversations/<int:pk>/unshare/", console_data.conversation_unshare, name="conversation_unshare"),
        # Subscriptions
        path("subscriptions/", console_data.subscriptions, name="subscriptions"),
        path("subscriptions/<int:pk>/cancel/", console_data.subscription_cancel, name="subscription_cancel"),
        path("subscriptions/<int:pk>/extend/", console_data.subscription_extend, name="subscription_extend"),
        # Support tickets
        path("tickets/", console_support.tickets, name="tickets"),
        path("tickets/bulk/", console_support.tickets_bulk, name="tickets_bulk"),
        path("saved-replies/", console_canned.canned_replies, name="canned_replies"),
        path("saved-replies/new/", console_canned.canned_save, name="canned_create"),
        path("saved-replies/<int:pk>/save/", console_canned.canned_save, name="canned_save"),
        path("saved-replies/<int:pk>/delete/", console_canned.canned_delete, name="canned_delete"),
        path("tickets/<str:reference>/", console_support.ticket_detail, name="ticket_detail"),
        path("tickets/<str:reference>/reply/", console_support.ticket_reply, name="ticket_reply"),
        path("tickets/<str:reference>/update/", console_support.ticket_update, name="ticket_update"),
        # Growth & system
        path("referrals/", console.referrals, name="referrals"),
        path("referrals/campaigns/", console_referrals.campaigns, name="referral_campaigns"),
        path("referrals/campaigns/new/", console_referrals.campaign_create, name="referral_campaign_create"),
        path("referrals/campaigns/<int:pk>/", console_referrals.campaign_detail, name="referral_campaign_detail"),
        path("referrals/campaigns/<int:pk>/edit/", console_referrals.campaign_edit, name="referral_campaign_edit"),
        path("referrals/campaigns/<int:pk>/toggle/", console_referrals.campaign_toggle, name="referral_campaign_toggle"),
        path("notifications/", console.notifications, name="notifications"),
        path("notifications/<int:pk>/action/", console.announcement_action, name="announcement_action"),
        # Telegram — bot and channel status, setup guide, delivery log.
        path("telegram/", console_telegram.telegram, name="telegram"),
        path("telegram/action/", console_telegram.telegram_action, name="telegram_action"),
        path("telegram/send/", console_telegram.telegram_send, name="telegram_send"),
        path("telegram/send/<int:pk>/delete/", console_telegram.telegram_message_delete,
             name="telegram_message_delete"),
        path("audit-logs/", console.audit_logs, name="audit_logs"),
        # Operations — admin-only surfaces for running the platform
        path("health/", console_ops.health, name="health"),
        path("health/data/", console_ops.health_data, name="health_data"),
        # Model status — declared status per model, measured health, pause all chat, daily digest.
        path("model-status/", console_status.model_status, name="model_status"),
        path("model-status/<int:pk>/set/", console_status.model_status_set, name="model_status_set"),
        path("model-status/pause/", console_status.chat_pause, name="model_status_pause"),
        path("model-status/digest/", console_status.digest_action, name="model_status_digest"),
        path("exports/", console_ops.exports, name="exports"),
        path("exports/<str:key>.csv", console_ops.export_download, name="export_download"),
        path("settings/", console.console_settings, name="settings"),
        # Staff & roles — owners only (see AI/services/staff_permissions.py).
        path("staff/", console_staff.staff, name="staff"),
        path("staff/new/", console_staff.staff_create, name="staff_create"),
        path("staff/<int:pk>/role/", console_staff.staff_role, name="staff_role"),
        path("staff/<int:pk>/toggle/", console_staff.staff_toggle, name="staff_toggle"),
    ],
    "console",
)
