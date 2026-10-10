"""Builds the client guide: guide.html (+ img/) -> EPC-AI-Assistant-User-Guide.pdf via Chromium."""
from __future__ import annotations

import html
import sys
from pathlib import Path

HERE = Path(__file__).parent
SITE = "https://pensionsync.online"
ADMIN = f"{SITE}/admin/login/"
E = html.escape

parts: list[str] = []
toc: list[tuple[int, str, str]] = []
counter = {"h1": 0, "h2": 0}


def add(s: str) -> None:
    parts.append(s)


def h1(title: str, intro: str = "") -> None:
    counter["h1"] += 1
    counter["h2"] = 0
    anchor = f"s{counter['h1']}"
    toc.append((1, f"{counter['h1']}. {title}", anchor))
    add(f'<section class="chapter" id="{anchor}"><div class="chapter-no">Part {counter["h1"]}</div>'
        f'<h1>{E(title)}</h1>' + (f'<p class="lead">{intro}</p>' if intro else "") + "</section>")


def h2(title: str, where: str = "", who: str = "") -> None:
    counter["h2"] += 1
    anchor = f"s{counter['h1']}-{counter['h2']}"
    toc.append((2, f"{counter['h1']}.{counter['h2']} {title}", anchor))
    meta = ""
    if where:
        meta += f'<div class="where"><span class="tag">Where to go</span>{where}</div>'
    if who:
        meta += f'<div class="where who"><span class="tag">Who can use it</span>{who}</div>'
    add(f'<h2 id="{anchor}"><span class="num">{counter["h1"]}.{counter["h2"]}</span>{E(title)}</h2>{meta}')


def h3(title: str) -> None:
    add(f"<h3>{E(title)}</h3>")


def p(text: str) -> None:
    add(f"<p>{text}</p>")


def steps(*items: str) -> None:
    add('<ol class="steps">' + "".join(f"<li>{i}</li>" for i in items) + "</ol>")


def bullets(*items: str) -> None:
    add('<ul class="bul">' + "".join(f"<li>{i}</li>" for i in items) + "</ul>")


def see(text: str) -> None:
    add(f'<div class="box see"><b>You should see:</b> {text}</div>')


def tip(text: str) -> None:
    add(f'<div class="box tip"><b>Tip:</b> {text}</div>')


def careful(text: str) -> None:
    add(f'<div class="box careful"><b>Be careful:</b> {text}</div>')


def shot(name: str, caption: str, phone: bool = False) -> None:
    cls = "shot phone" if phone else "shot"
    add(f'<figure class="{cls}"><img src="img/{name}.jpg" alt=""><figcaption>{caption}</figcaption></figure>')


def table(head: list[str], rows: list[list[str]], cls: str = "") -> None:
    t = f'<table class="{cls}"><thead><tr>' + "".join(f"<th>{h}</th>" for h in head) + "</tr></thead><tbody>"
    for r in rows:
        t += "<tr>" + "".join(f"<td>{c}</td>" for c in r) + "</tr>"
    add(t + "</tbody></table>")


def checklist(title: str, rows: list[tuple[str, str]]) -> None:
    add(f'<h3>{E(title)}</h3><table class="check"><thead><tr><th class="cb"></th><th>Do this</th>'
        f'<th>It works if…</th></tr></thead><tbody>'
        + "".join(f'<tr><td class="cb">&#9744;</td><td>{a}</td><td>{b}</td></tr>' for a, b in rows)
        + "</tbody></table>")


def b(x: str) -> str:
    return f"<b>{x}</b>"


def btn(x: str) -> str:
    return f'<span class="btn">{x}</span>'


def menu(*xs: str) -> str:
    return ' <span class="arrow">&rarr;</span> '.join(f'<span class="mi">{x}</span>' for x in xs)


def page_break() -> None:
    add('<div class="pb"></div>')


# ===========================================================================
# CONTENT
# ===========================================================================

# ---------------------------------------------------------------- Part 1
h1("Welcome", "This guide shows you how to use every part of the EPC AI Assistant website: "
   "what your students see, and how you run everything from the Admin console. "
   "No technical knowledge is needed. Just follow the steps.")

h2("What this website does")
p("EPC AI Assistant is a study helper for students. A student types a question (or sends a photo of "
  "their homework) and the AI explains the answer step by step, in any subject, at any time of day.")
bullets(
    f"Students use the website at {b(SITE)}.",
    f"You and your team run everything from the {b('Admin console')} at {b(ADMIN)}.",
    "Each question uses a small amount of " + b("tokens") + " (think of them as credit). "
    "New students get free tokens to try it. When they run out, they buy more online.",
    "Payments go straight to your bank account through ABA PayWay, ACLEDA Bank or Binance Pay.",
    "Students can also get alerts and sign-in codes on Telegram.",
)

h2("Words you will see")
table(["Word", "What it means"], [
    [b("Token"), "Credit that students spend when they ask questions. A short question uses a few hundred tokens; a long answer uses more."],
    [b("Model"), "The AI \"brain\" that writes the answer, for example GPT-5 Mini or Claude 4.5. Each model has its own token balance."],
    [b("Plan"), "A token package a student buys, for example \"100K tokens for $2.49\"."],
    [b("Free trial"), "Free tokens a new student gets after confirming their e-mail. Right now: 3,000 GPT-5 Mini tokens."],
    [b("Admin console"), "The private control room for you and your staff."],
    [b("Student"), "Anyone who uses the website to study."],
    [b("Staff / Role"), "People on your team. Each has a role (Owner, Support, Finance, Content, Operations) that decides what they can open."],
    [b("Code (OTP)"), "A 6-digit number sent by e-mail or Telegram to prove it's really the person signing in."],
    [b("Telegram bot"), "An automatic Telegram account (@EPCAIAssistantBot) that sends students their alerts and codes."],
    [b("Channel / Group"), "Your Telegram channel (announcements to everyone) and your Telegram group (where students chat)."],
    [b("Promo code"), "A discount code students type when buying tokens."],
    [b("Referral"), "When a student invites a friend with their personal link, both get bonus tokens."],
])

h2("The two doors into the website")
table(["", "Student website", "Admin console"], [
    [b("Address"), SITE, ADMIN],
    [b("Who uses it"), "Students", "You and your staff only"],
    [b("Sign in with"), "The student's username or e-mail + password", "Your administrator username + password"],
    [b("What it is for"), "Asking questions, buying tokens, settings", "Managing students, tokens, payments, messages and settings"],
])
tip("Bookmark both addresses in your browser so you can open them with one click.")

h2("Your first 10 minutes (as the owner)")
steps(
    f"Open {b(ADMIN)} and sign in with your owner username and password.",
    f"Go to {menu('Settings')} and check {b('Support e-mail')}, {b('Support Telegram')} and {b('WhatsApp number')} "
    f"under {b('Support &amp; contact')}. Students see these on the Help page. Press {btn('Save settings')}.",
    f"Go to {menu('Plans')} and check the prices of the plans students can buy.",
    f"Go to {menu('Payment gateways')} and check ABA PayWay, ACLEDA and Binance show as ready.",
    f"Go to {menu('Telegram')} and check the bot says {b('Online')}.",
    f"Open {b(SITE)} in another window (or on your phone) and create a test student account "
    "(see section 2.1). This is how you see exactly what students see.",
)

# ---------------------------------------------------------------- Part 2
h1("The student side", "Everything a student can do, in the order they usually do it. "
   "Use a test student account to try each step yourself.")

h2("Create a student account", where=f"{SITE} &rarr; {btn('Get started')}")
shot("p_home", "The home page. Students press <b>Get started</b> or <b>Open the assistant</b>.")
steps(
    f"Open {b(SITE)} and press {btn('Get started')}.",
    f"On the {b('Sign up')} tab, fill in {b('Full name')}, {b('Username')}, {b('E-mail address')} and "
    f"{b('Password')}. If a friend invited them, they can type the friend's code in {b('Referral code (optional)')}.",
    f"Tick {b('I agree to the Terms of Service and Privacy Policy')}.",
    f"Press {btn('Create account')}.",
    "A 6-digit code is e-mailed to the student. Type it into the boxes and press "
    f"{btn('Verify and continue')}.",
)
shot("p_signup", "The sign-up form.")
see("the chat page, with a welcome message saying free tokens were added (3,000 GPT-5 Mini tokens).")
tip(f"Prefer Telegram? On the code page, a student can press {btn('Start our Telegram bot')}, press "
    f"{b('Start')} in Telegram, come back, press {btn('I&#39;ve pressed Start')}, then "
    f"{btn('Send the code to Telegram instead')}. The code then arrives in Telegram.")

h2("Sign in and forgotten passwords", where=f"{SITE}/login/")
steps(
    f"Open {b(SITE + '/login/')}.",
    f"Type the {b('Username or e-mail')} and {b('Password')}, then press {btn('Sign in')}.",
    f"If sign-in codes are switched on, a code arrives by e-mail (and Telegram, if connected). Type it and press {btn('Verify and continue')}.",
)
shot("p_login", "The sign-in page.")
h3("If a student forgets their password")
steps(
    f"On the sign-in page, press {b('Forgot password?')}",
    f"Type the username or e-mail and press {btn('Send reset code')}.",
    "Type the code that arrives, then choose a new password twice and press " + btn("Save new password") + ".",
)
p("Only one device can be signed in at a time. Signing in on a phone signs the student out on their computer.")

h2("The chat page (asking questions)", where=f"{SITE}/chat/  (opens straight after signing in)")
shot("s_chat_conv", "The chat page. Left: the menu and past chats. Middle: the conversation. Bottom: the box where students type.")
h3("Ask a question")
steps(
    "Click the big box at the bottom (it says <i>Type a question, paste your notes…</i>).",
    "Type the question, for example <i>Explain photosynthesis in simple words</i>.",
    f"Press {b('Enter')} on the keyboard, or the round arrow button.",
    "The answer appears word by word. Press the square stop button (or Esc) to stop it early.",
)
see("the answer, with its token cost and time shown under it. The token box at the bottom left goes down a little.")
h3("Send a photo or a file")
steps(
    f"Press the {btn('+')} button at the bottom left of the box.",
    f"Choose {b('Photos')}, {b('Files')} (PDF, Word, Excel, text), {b('Camera')} (on a phone) or {b('Anything')}.",
    "Pick the file. It appears as a small chip above the box.",
    "Type a question about it (for example <i>Solve question 3</i>) and send.",
)
shot("s_plus_menu", "The + menu for photos, files, the camera and saved prompts.")
h3("Speak instead of typing")
steps(
    "Press the microphone button next to the send button.",
    "Speak your question. Press &#10003; to finish (or X to throw it away).",
    "Check the words that appear in the box, then send.",
)
h3("Choose which AI model answers")
p("Each model is good at different things, and each has its own tokens. A model the student has no tokens for shows <b>Locked</b>.")
steps(
    "Press the model name at the bottom right of the box (for example <b>GPT-5 Mini</b>).",
    "Pick a model from the list. Each one says what it's best at and how many tokens are left.",
)
shot("s_model_menu", "The model list. GPT-5 Mini is ready; the others are locked until the student buys tokens for them.")
h3("Things you can do with an answer")
p("Move the mouse over an answer (or tap it on a phone) to see these buttons:")
table(["Button", "What it does"], [
    [b("Copy"), "Copies the answer so it can be pasted somewhere else."],
    [b("Listen"), "Reads the answer out loud."],
    [b("Helpful") + " / " + b("Not helpful"), "Thumbs up or down. \"Not helpful\" asks what went wrong, and the team sees it in Answer quality."],
    [b("Save"), "Keeps the answer in <b>Saved answers</b> to find it later."],
    [b("Branch"), "Starts a new chat that continues from this answer."],
    [b("Regenerate"), "Writes the newest answer again, differently."],
    [b("Rewrite"), "Makes the newest answer <b>Shorter</b>, <b>Simpler</b>, <b>More detail</b>, <b>As steps</b>, or <b>Translate…</b> it."],
    [b("Edit") + " (on the student's own message)", "Changes the question and sends it again."],
])
shot("s_reply_actions", "An answer with its action buttons, and suggested follow-up questions under <b>Ask next</b>.")
h3("Make a picture")
p("There is no special button. The student simply asks, for example <i>Draw a diagram of the water cycle</i>. "
  "The picture appears in the answer with <b>Download</b> and <b>Share</b> buttons. Click it to see it bigger.")
h3("Organise chats")
p(f"Press {btn('New chat')} (top left) to start a fresh conversation. Past chats are listed on the left, newest first. "
  f"Press the {b('&middot;&middot;&middot;')} button next to a chat (or at the top of the page) for these options:")
table(["Option", "What it does"], [
    [b("Rename"), "Gives the chat a clearer name."],
    [b("Pin"), "Keeps it at the top of the list."],
    [b("Move to folder"), "Puts it in a folder (create folders with the + next to <b>Folders</b> on the left)."],
    [b("Archive"), "Hides it from the list without deleting it (find it under <b>Archived chats</b>)."],
    [b("Make flashcards") + " / " + b("Make a quiz"), "Turns the chat into study cards or a quiz (see 2.7)."],
    [b("Download"), "Saves the chat as a text file, or <b>Print or save as PDF</b>."],
    [b("Delete"), "Moves it to <b>Recently deleted</b>. It can be restored for 30 days."],
])
shot("s_chat_menu", "The chat options menu.")
h3("Share a chat")
steps(
    f"Open the chat and press {btn('Share')} at the top.",
    f"Press {btn('Create link')}, then {btn('Copy')}. Anyone with the link can read the chat (but not change it).",
    f"To stop sharing, open the same window and press {btn('Stop sharing')}.",
)
h3("Search and shortcuts")
p(f"Press {b('Ctrl + K')} (or the magnifying glass at the top) to search all chats and jump to any page. "
  f"Press {b('Ctrl + /')} to see every keyboard shortcut.")
shot("s_palette", "The search box. It finds chats by their words and by their meaning.")

h2("Buy tokens", where=f"Left menu &rarr; {menu('Pricing')}")
shot("s_pricing", "The Pricing page. Tabs at the top switch between models; each card is a token package.")
steps(
    f"Press {b('Pricing')} in the left menu.",
    "Pick the model at the top (for example <b>GPT-5 Mini</b>).",
    f"Press {btn('Buy 100k tokens')} (or another package).",
    f"Choose how to pay: {b('ABA PayWay')}, {b('ACLEDA Bank')} or {b('Binance Pay')}. "
    f"Have a discount code? Type it in {b('Promo code')} and press {btn('Apply')}.",
    f"Press {btn('Continue to payment')}.",
    "Pay on the bank page or by scanning the QR code with the banking app.",
    "Come back to the website. When the bank confirms, the tokens are added automatically "
    f"and a receipt appears. If it takes a moment, press {btn('I&#39;ve paid — check now')}.",
)
shot("s_checkout", "Checkout: choose the payment method on the left; the order summary is on the right.")
see(f"\"Payment confirmed\" and a receipt. The tokens appear in the token box at the bottom left. The receipt is also under {menu('Billing')}.")
careful("these are real payments into your bank account. To test, buy the smallest package. "
        "If a payment ever gets stuck, the admin can check and confirm it (see 3.12).")

h2("Usage, billing and comparing models")
table(["Page (left menu)", "What it shows"], [
    [b("Usage"), "Tokens left for each model, daily/weekly/monthly limits, a chart of the last 7/30/90 days, and every token spent."],
    [b("Billing") + " (account menu)", "Money spent, tokens bought, the current plan and every invoice with its receipt."],
    [b("Compare models"), "Each AI model side by side: price, whether it reads images, rating, and the student's balance. Buttons <b>Chat with it</b> and <b>Buy tokens</b>."],
])
shot("s_usage", "The Usage page.")

h2("Prompt library and saved answers", where=f"Left menu &rarr; {menu('Prompt library')} / {menu('Saved answers')}")
bullets(
    f"{b('Prompt library')}: ready-made questions. Press {btn('Use in chat')} to put one in the chat box. "
    f"Students can also save their own with {btn('Save prompt')}.",
    f"{b('Saved answers')}: every answer the student pressed {b('Save')} on. Each can be opened in its chat, copied, or given a note.",
)
shot("s_prompts", "The Prompt library.")

h2("Study: flashcards and quizzes", where=f"Left menu &rarr; {menu('Study')}")
steps(
    f"Open {b('Study')}. Under {b('Make a new set')}, choose a chat in {b('From this chat')}.",
    "Choose Flashcards or Quiz, and how many (5, 8 or 12).",
    f"Press {btn('Make it')}.",
    "Flashcards: tap the card to turn it, then press <b>Got it</b> or <b>Still learning</b>. "
    "Quiz: pick an answer for each question, then <b>See my score</b>.",
)
p("The Study page also shows the student's daily goal and streak (how many days in a row they studied).")

h2("Notifications", where=f"Left menu &rarr; {menu('Notifications')}")
p("Messages from the team and automatic alerts (payment confirmed, running low on tokens, plan ending, announcements). "
  f"The number next to the menu shows how many are unread. Press {btn('Mark all as read')} to clear them.")

h2("Help and support requests", where=f"Left menu &rarr; {menu('Help')}")
steps(
    f"Open {b('Help')}. Read {b('Common questions')} first; the search box finds answers quickly.",
    f"To contact the team, scroll to {b('Ask us anything')}, choose a topic, write a subject and the problem, "
    "add a screenshot if helpful, and press " + btn("Send request") + ".",
    f"Replies appear under {b('Your requests')}. Open a request to read the reply and answer back.",
)
shot("s_help", "The Help page with contact options and common questions.")

h2("Invite friends", where=f"Left menu &rarr; {menu('Invite friends')}")
p(f"Each student has a personal code and invite link. Press {btn('Copy link')} or {btn('Share on Telegram')}. "
  "When a friend signs up with it and verifies, both get bonus tokens. The page shows how many people they invited and the tokens earned.")
shot("s_referrals", "The Invite friends page.")

h2("Profile and settings", where=f"Bottom-left name &rarr; {menu('Profile')} or {menu('Settings')}")
p(f"{b('Profile')}: change name, e-mail and phone ({btn('Save changes')}), or change the password ({btn('Change password')}).")
p(f"{b('Settings')} has these sections:")
table(["Section", "What a student can change"], [
    [b("Appearance"), "Light, Dark or System look, and one of 6 colours."],
    [b("Chat defaults"), "Which model new chats start with, and a daily goal."],
    [b("Personalisation"), "Answer style (Balanced, Concise, Detailed, Step by step), what the AI should know about them, and the answer language."],
    [b("Notifications"), "Which alerts arrive by e-mail and Telegram."],
    [b("Notifications on this device"), "Phone/computer pop-up alerts: <b>Turn on</b>, <b>Send a test</b>."],
    [b("Telegram"), "Connect their Telegram (see below)."],
    [b("Security"), "<b>Ask for a code when I sign in</b>: an extra code at every sign-in."],
    [b("Privacy and your data"), "Download all their data, or <b>Delete my account</b>."],
])
shot("s_settings", "The Settings page.")
h3("Connect Telegram (student)")
steps(
    f"In {menu('Settings')}, find {b('Telegram')} and press {btn('Connect Telegram')}.",
    f"Press {btn('Open in Telegram')} (or scan the QR code with a phone).",
    f"In Telegram, press {b('Start')}. The bot replies that the account is connected.",
    f"Back on the website, press {btn('I&#39;ve pressed Start — refresh')}.",
    f"Press {btn('Send a test message')} to check it works.",
)
shot("s_settings_telegram", "The Telegram card in Settings.")

# ---------------------------------------------------------------- Part 3
h1("The Admin console", "How you and your team run the website. Everything below is done at "
   f"{b(ADMIN)}.")

h2("Sign in and find your way around", where=ADMIN)
steps(
    f"Open {b(ADMIN)}.",
    f"Type your {b('Administrator username')} and {b('Password')}, then press {btn('Enter console')}.",
)
shot("a_login", "The console sign-in page.")
p("Inside, the menu on the left has four groups:")
table(["Group", "What is inside"], [
    [b("People"), "Students, Running low, Expiring access, Create accounts, Referrals, Referral campaigns"],
    [b("Usage"), "Conversations, AI tokens, Token management, Usage limits, Answer quality, Prompt library, Usage analytics"],
    [b("Revenue"), "Revenue &amp; growth, Plans, Promo codes, Payments, Payment gateways, Subscriptions, Cost &amp; margin"],
    [b("System"), "Announcements, Telegram, Send on Telegram, Support tickets, Saved replies, Email log, Audit logs, Model status, System health, Data exports, Staff &amp; roles, Settings"],
])
bullets(
    f"{b('Search or jump to…')} (top): type a student's name, a payment number or a page name to go straight there.",
    f"{b('The bell')} (top): things that need you: support tickets, payments to confirm, students running low.",
    f"{b('The moon')} (top): switch between light and dark look.",
)

h2("Dashboard", where=f"{menu('Dashboard')}", who="Everyone")
shot("a_dashboard", "The Dashboard: a short daily briefing, live activity, payments waiting, and key numbers.")
bullets(
    f"{b('Daily briefing')}: a one-paragraph summary of today. Press a question chip (for example <i>What needs my attention?</i>) for more.",
    f"{b('Payments waiting')}: press {btn('Review now')} to confirm them.",
    f"{b('Numbers')}: total students, active students, tokens remaining, revenue, with charts underneath.",
    f"Quick buttons: {btn('Create accounts')}, {btn('Allocate tokens')}, {btn('System health')}.",
)

h2("Students: find and help a student", where=f"{menu('Students')}", who="Owner, Support, Finance, Operations")
shot("a_users", "The student list.")
steps(
    f"Open {b('Students')}. Type a name, username, e-mail or phone in {b('Search')} and press {btn('Apply filters')}.",
    "Click the student's name to open their page.",
)
shot("a_user_detail", "A student's page. The action buttons are at the top.")
table(["Button", "What it does", "How"], [
    [btn("Manage tokens"), "Give or take back tokens", "Choose the AI model, press <b>Add</b> or <b>Remove</b>, type the amount (or press 50K/100K…), write a reason, press <b>Apply change</b>."],
    [btn("Message"), "Send the student a message", "Type a title and message, choose the tone, tick <b>Also send by e-mail</b> if wanted, press <b>Send message</b>. It also goes to their Telegram if connected."],
    [btn("Grant plan"), "Give a plan for free (for example after a cash payment)", "Choose the plan, write a reason, press <b>Grant plan</b>."],
    [btn("Disable") + " / " + btn("Enable"), "Block or unblock the account", "Write a reason, press <b>Disable account</b>."],
    [btn("Reset password"), "Give the student a new password", "Press it and confirm; give them the new password."],
    [btn("Sign out everywhere"), "Signs the student out on every device", "Press it and confirm."],
    [btn("Edit"), "Change their name, e-mail, phone or status", "Change the fields, press <b>Save changes</b>."],
    [btn("Chats"), "Read their conversations", "Opens their chat list (viewing is recorded)."],
])
shot("a_manage_tokens", "The Manage tokens window.")
p(f"Lower on the page: {b('Models &amp; access')} (tokens and limits for every model, and "
  f"{b('Can this student chat? — every check')} if a student says it doesn't work), "
  f"{b('Notes &amp; tags')} (private notes about the student), token history, payments and conversations.")
h3("Do something for many students at once")
steps(
    "In the student list, tick the box next to each student (or the top box for all).",
    f"In the bar that appears, choose {btn('Give tokens')}, {btn('Message')}, {btn('Tag')}, "
    f"{btn('Enable')}, {btn('Disable')} or {btn('Export CSV')}.",
    "Check the list on the next page and press the final button (for example <b>Send to 12</b>).",
)

h2("Running low and expiring access", where=f"{menu('Running low')} / {menu('Expiring access')}",
   who="Owner, Support, Finance, Operations")
bullets(
    f"{b('Running low')}: students almost out of tokens. Press {b('Add tokens')} to top them up, or "
    f"{b('E-mail a warning now')} to remind them to buy more.",
    f"{b('Expiring access')}: plans ending in the next 7/14/30/90 days. Press {b('Send a renewal reminder')}, "
    f"or {b('Extend by 30 days')}.",
)
shot("a_low", "Students running low on tokens.")

h2("Create student accounts yourself", where=f"{menu('Create accounts')}", who="Owner")
h3("One account")
steps(
    f"Open {b('Create accounts')} and stay on the {b('One account')} tab.",
    f"{b('Step 1')}: type the username, full name, e-mail and phone, and choose where they came from.",
    f"{b('Step 2')}: press {btn('Generate secure password')}. Choose a plan if they paid. "
    f"Tick {b('Email the credentials')} to e-mail them their login.",
    f"{b('Step 3')}: choose starting tokens (or None) and the model.",
    "Press the create button. Copy the details from the <b>Handover card</b> to give to the student.",
)
shot("a_accounts", "Create accounts.")
h3("Many accounts from a spreadsheet")
steps(
    f"Switch to {b('Bulk import')} and press {btn('Download template')}.",
    "Fill in one student per row in Excel (username, full_name, email, password, source, tokens, phone, notes). Save it.",
    f"Upload it under {b('Upload a spreadsheet')} and press {btn('Validate &amp; preview')}.",
    f"Fix any rows marked blocked, then press {btn('Create accounts')}.",
    f"On the result page press {btn('Download CSV')} to keep the passwords. They are shown only once.",
)

h2("Referrals and referral campaigns", where=f"{menu('Referrals')} / {menu('Referral campaigns')}", who="Owner, Finance")
bullets(
    f"{b('Referrals')}: who invited whom, the top referrers, and the bonus amounts ({btn('Change amounts')}).",
    f"{b('Referral campaigns')}: special codes (for example for an influencer or school). Press {btn('New code')}, "
    "set what the new student and the sharer get, then share the link. You can see clicks, sign-ups and purchases, and "
    f"{b('Pause')} a code at any time.",
)

h2("Conversations", where=f"{menu('Conversations')}", who="Owner, Support")
p("Search every student chat by title, student or words. Press <b>Open</b> to read one. "
  "Use this only to help with a support problem; every view is recorded in the audit log.")

h2("Tokens: supplies, giving tokens and limits", where=f"{menu('AI tokens')} / {menu('Token management')} / {menu('Usage limits')}",
   who="Owner, Operations")
bullets(
    f"{b('AI tokens')}: how many tokens are left to hand out for each model ({b('Model supplies')}). "
    f"Top a supply up with {btn('Top up')}. Give tokens to many students under {b('Allocate to many students')}, "
    f"or to one student with {btn('Distribute tokens')}.",
    f"{b('Token management')} &rarr; {b('AI models')} tab: switch a model on or off, choose the {b('default')} model, "
    "and set each model's daily/weekly/monthly limits.",
    f"{b('Usage limits')}: a read-only list of each student's limits and how much they used.",
    f"{b('Usage analytics')}: charts of tokens and requests per day, and the top users.",
)
shot("a_ai_tokens", "AI tokens: the supply for each model.")

h2("Answer quality and the prompt library", where=f"{menu('Answer quality')} / {menu('Prompt library')}", who="Owner, Content")
bullets(
    f"{b('Answer quality')}: every answer a student marked \"Not helpful\", with the reason. Press {b('Review')}, "
    "mark it reviewed or resolved, and add a note.",
    f"{b('Prompt library')}: the ready-made questions students see. Add one under {b('New featured prompt')} "
    f"and press {btn('Publish prompt')}. Hide or delete any time.",
)

h2("Plans and prices", where=f"{menu('Plans')}", who="Owner, Finance")
steps(
    f"Open {b('Plans')}. Each card shows the price, tokens, how long it lasts, and how many were sold.",
    f"To change one, press {b('Edit')}, change the price or tokens, and press {btn('Save changes')}.",
    f"To add one, press {btn('New plan')}, fill in the name, model, price, tokens and duration, and press {btn('Create plan')}.",
    f"To stop selling one, press {b('Disable')} (students who already bought it keep their tokens).",
)
shot("a_plans", "Plans.")

h2("Promo codes", where=f"{menu('Promo codes')}", who="Owner, Finance")
steps(
    f"Press {btn('New code')}.",
    "Type the code (for example <b>WELCOME20</b>), choose <b>Percentage off</b> or <b>Fixed amount off</b> and the amount.",
    "Optional: limit total uses, uses per student, start and end dates, and which plans it works on.",
    "Save. Students type the code at checkout. Press <b>Pause</b> to stop it.",
)

h2("Payments: confirm, decline or refund", where=f"{menu('Payments')}", who="Owner, Finance")
shot("a_payments", "Payments: confirmed revenue at the top, then every order.")
p("Most payments (ABA, ACLEDA, Binance) are confirmed automatically by the bank, and the tokens arrive by themselves. "
  "You only need this page when something needs a person:")
table(["Situation", "What to do"], [
    ["A student says they paid but has no tokens", "Find the order (search by name or reference), open it, press <b>Re-check with provider</b>. If the bank shows it as paid, it confirms and adds the tokens."],
    ["A payment is waiting for review (receipt uploaded)", "Open it, look at the <b>Payment proof</b> picture, check your bank app, then press <b>Confirm &amp; release</b> (or <b>Decline</b>)."],
    ["Amount mismatch or duplicate warning", "Check your bank statement before confirming."],
    ["A refund is needed", "Open the paid order and press <b>Refund</b>. The tokens are taken back. (Send the money back from your bank yourself.)"],
])
careful("only confirm a payment after you see the money in your bank. Never confirm because of a screenshot alone.")

h2("Payment gateways", where=f"{menu('Payment gateways')}", who="Owner, Finance")
p(f"Shows whether ABA PayWay, ACLEDA Bank and Binance Pay are working, with a {btn('Test connection')} button for each, "
  "and the last 7 days of paid, pending, expired and failed payments. Changing bank keys is a technical job. Ask your developer.")

h2("Subscriptions, revenue and costs", where=f"{menu('Subscriptions')} / {menu('Revenue &amp; growth')} / {menu('Cost &amp; margin')}",
   who="Owner, Finance")
bullets(
    f"{b('Subscriptions')}: every student's plan. Press {b('End date')} to extend or shorten it, or {b('Cancel')}.",
    f"{b('Revenue &amp; growth')}: money and new students month by month, by plan and by payment method. Press {b('CSV')} to download.",
    f"{b('Cost &amp; margin')}: what the AI costs you versus what students paid, per model.",
)
shot("a_reports", "Revenue &amp; growth.")

h2("Announcements (message every student)", where=f"{menu('Announcements')}", who="Owner, Content")
steps(
    f"Under {b('New announcement')}, write a {b('Title')} and the message ({b('Body')}).",
    f"Choose the {b('Level')}: Info, Success, Warning or Critical.",
    f"Choose the {b('Audience')}: all students, students running low, students with no tokens, or students on a certain plan or model.",
    "Optional: add a button (text and link), or choose a time in <b>Send at</b> to send it later.",
    "Optional: tick <b>Post to the Telegram channel</b>, <b>Post to the Telegram group</b> and/or <b>Also send to students' Telegram chats</b>.",
    "Check the preview on the right, then publish.",
)
shot("a_notifications", "Writing an announcement, with the student preview.")
p(f"Under {b('Published')}, you can see how many opened it, and press {b('Retract')} to take it back (it is also deleted from Telegram).")

h2("Telegram and Send on Telegram", where=f"{menu('Telegram')} / {menu('Send on Telegram')}")
p(f"{b('Telegram')} shows whether the bot is {b('Online')}, the channel and group, which automatic alerts go to Telegram "
  "(switch each on or off and press <b>Save alerts</b>), and which students connected.")
h3("Send one message on Telegram")
steps(
    f"Open {b('Send on Telegram')}.",
    f"{b('1. Where should it go?')}: pick {b('Channel')}, {b('Community group')}, {b('One student')} (then choose the student) "
    f"or {b('All connected students')}.",
    f"{b('2. Write the message')}: an optional title and the message. Optional: {b('Add a link button')}.",
    "Check the <b>Preview</b>, which shows it as it will look in Telegram.",
    f"Press the send button (it says where it is going, for example {btn('Post to the channel')}).",
)
shot("a_tg_send", "Send on Telegram.")
p(f"{b('Recent messages')} at the bottom shows whether each message arrived. Channel and group posts have a "
  f"{b('Delete')} button for 48 hours.")
h3("Your own Telegram (for your sign-in codes and test messages)")
p(f"On the {b('Telegram')} page, under {b('Your Telegram')}, press {btn('Connect my Telegram')}, then "
  f"{b('Open in Telegram')} and {b('Start')}. Then press {btn('Send test to my chat')}.")

h2("Support tickets and saved replies", where=f"{menu('Support tickets')} / {menu('Saved replies')}", who="Owner, Support")
steps(
    f"Open {b('Support tickets')}. New requests from students appear here (and on the bell).",
    "Press <b>Open</b> on a ticket to read it.",
    "Type your answer in the reply box. Use <b>Insert a saved reply…</b> for common answers. Press <b>Send</b>.",
    "Under <b>Manage</b>, set the status (for example <b>Resolved</b>) and press <b>Save changes</b>.",
)
p(f"{b('Saved replies')}: write answers you use often once, and insert them in one click. "
  "Type <b>{name}</b> in a reply to fill in the student's first name.")
tip("Tick <b>Internal note</b> to write something only your team can see.")

h2("Keeping the system healthy", where=f"{menu('Model status')} / {menu('System health')} / {menu('Email log')}", who="Owner, Operations")
bullets(
    f"{b('Model status')}: if an AI model is having problems, set it to <b>Slow or unreliable</b> or <b>Paused</b> with a note for students. "
    f"In an emergency press {btn('Pause all chat…')} (and {btn('Resume chat')} afterwards). You can also turn on a daily summary e-mail.",
    f"{b('System health')}: green means fine. It warns you when a model's supply will run out soon.",
    f"{b('Email log')}: every e-mail the website sent. Failed ones have a <b>Retry</b> button.",
    f"{b('Data exports')}: download students, payments, tokens, tickets and more as spreadsheets (CSV).",
    f"{b('Audit logs')} (Owner only): a record of everything every staff member did.",
)

h2("Staff and roles", where=f"{menu('Staff &amp; roles')}", who="Owner")
steps(
    f"Under {b('New staff account')}, type the username, full name and e-mail, and choose a role.",
    f"Press {btn('Create staff account')}. Copy the temporary password and give it to them.",
    f"To change someone's role later, pick it in the list and press {btn('Save')}. To remove access, press {b('Disable')}.",
)
table(["Role", "Can open"], [
    [b("Owner"), "Everything, including staff, settings and the audit log."],
    [b("Support"), "Students (read and help), messages, tickets, conversations."],
    [b("Finance"), "Payments, plans, promo codes, subscriptions, revenue, referrals."],
    [b("Content"), "Announcements, Telegram channel/group posts, prompt library, answer quality."],
    [b("Operations"), "Tokens and models, usage limits, model status, health, e-mail log, exports, Telegram setup."],
])

h2("Settings", where=f"{menu('Settings')}", who="Owner")
p(f"Business rules you can change any time. Change what you need and press {btn('Save settings')} at the bottom.")
table(["Group", "Most useful settings"], [
    [b("Growth &amp; bonuses"), "Free trial tokens (now 3,000), turn the free trial on/off, referral bonus amounts, turn referrals on/off."],
    [b("Alerts &amp; reminders"), "When students are warned about low tokens, and how many days before a plan ends they are reminded."],
    [b("Security"), "<b>Sign-in code for every student</b> (an extra code at every sign-in), <b>Send codes to Telegram and e-mail</b>, code length and lifetime, one device at a time."],
    [b("Assistant behaviour"), "Follow-up suggestions, sharing chats, personalisation, image reading."],
    [b("Usage limits"), "Warning levels and whether students see the usage meter."],
    [b("Support &amp; contact"), "WhatsApp number and message, support hours."],
    [b("Image generation"), "Turn pictures on/off, quality, and how many tokens a picture costs."],
    [b("Payments"), "The riel exchange rate and how long a student has to pay."],
    [b("Telegram alerts"), "Which automatic alerts go to Telegram."],
    [b("General"), "App name, support e-mail and Telegram, open/close public sign-up, a banner message for all students, maintenance pause."],
])
careful("<b>Sign-in code for every student</b> makes every student enter a code at every sign-in. Make sure e-mail is working first (test with Forgot password).")

# ---------------------------------------------------------------- Part 4
h1("Test everything: checklist", "Go through this list once, top to bottom, to check every part works. "
   "Tick each box as you go. Use one test student account and your owner account. "
   "Tip: open the student side in a normal window and the console in a private/incognito window, "
   "so you can be signed in as both at the same time.")

checklist("A. Student: account", [
    ("Sign up as a new test student (2.1)", "A code arrives by e-mail; after typing it you reach the chat page."),
    ("Check the token box at the bottom left", "It shows 3,000 GPT-5 Mini tokens."),
    ("Sign out (bottom-left name &rarr; Sign out) and sign in again", "You are back in the chat."),
    ("Use <b>Forgot password?</b> on the sign-in page", "A reset code arrives; the new password works."),
])
checklist("B. Student: chat", [
    ("Ask a question", "The answer appears word by word; tokens go down."),
    ("Press stop while an answer is being written", "It stops."),
    ("Send a photo of a question (+ &rarr; Photos)", "The answer talks about the photo."),
    ("Speak a question with the microphone", "Your words appear in the box."),
    ("Ask <i>Draw a simple picture of a cat</i>", "A picture appears with Download and Share."),
    ("Press Copy, Listen, Helpful, Save and Regenerate on an answer", "Each does what 2.3 says."),
    ("Rename, pin, archive and delete a chat; then restore it from <b>Recently deleted</b>", "All work; the chat comes back."),
    ("Make flashcards from a chat (&middot;&middot;&middot; &rarr; Make flashcards)", "Cards appear and can be turned."),
    ("Share a chat and open the link in another browser", "The chat is readable there."),
    ("Press Ctrl + K and search for a word from an old chat", "The chat is found."),
])
checklist("C. Student: money", [
    ("Pricing &rarr; buy the smallest package, pay with ABA", "Receipt appears; tokens are added; Billing lists it."),
    ("Repeat with ACLEDA and with Binance (if you use them)", "Same result."),
    ("Try a promo code at checkout (create one first, 3.11)", "The price goes down."),
    ("Open Usage and Billing", "Spending and receipts are shown."),
])
checklist("D. Student: other pages", [
    ("Prompt library &rarr; Use in chat", "The prompt goes into the chat box."),
    ("Help &rarr; send a support request", "It appears in <b>Your requests</b> (and in the console's Support tickets)."),
    ("Invite friends &rarr; Copy link; sign up a second test account with it", "Both accounts get the referral bonus."),
    ("Settings &rarr; change Appearance to Dark", "The site turns dark."),
    ("Settings &rarr; Telegram &rarr; Connect, then Send a test message", "The test message arrives in Telegram."),
    ("Settings &rarr; Security &rarr; tick <b>Ask for a code when I sign in</b>; sign out and in", "A code is asked for and arrives."),
])
checklist("E. Admin: students and tokens", [
    ("Students &rarr; search for the test student and open them", "Their page shows their tokens and plan."),
    ("Manage tokens &rarr; Add 1,000 GPT-5 Mini tokens", "The student's token box goes up; they get a notification (and Telegram message)."),
    ("Message &rarr; send a message", "It appears in the student's Notifications (and Telegram)."),
    ("Grant plan &rarr; give a plan", "The plan shows on the student's page and in their Billing."),
    ("Disable the account, try to sign in as the student, then Enable", "Sign-in is refused while disabled, works after."),
    ("Create accounts &rarr; create one account with tokens", "The new student can sign in with the password shown."),
])
checklist("F. Admin: money and messages", [
    ("Payments &rarr; find your test payment", "It shows as Paid."),
    ("Plans &rarr; edit a price, check Pricing on the student side, then change it back", "The student sees the new price."),
    ("Announcements &rarr; publish one to all students", "It appears in the student's Notifications."),
    ("Send on Telegram &rarr; One student &rarr; your test student", "It arrives in their Telegram; shows <b>Delivered</b>."),
    ("Send on Telegram &rarr; Channel", "The post appears in your channel; Delete removes it."),
    ("Support tickets &rarr; open the test request and reply", "The student sees the reply under Help &rarr; Your requests."),
])
checklist("G. Admin: settings and staff", [
    ("Settings &rarr; change the Dashboard banner text and save", "Students see the banner at the top."),
    ("Staff &amp; roles &rarr; create a Support account and sign in with it", "It can open Students and Support tickets, but not Settings or Payments."),
    ("Model status &rarr; set a model to <b>Slow or unreliable</b> with a note", "Students see the note in the model list. Set it back to <b>Working normally</b>."),
    ("Audit logs", "Everything you just did is listed."),
])

# ---------------------------------------------------------------- Part 5
h1("Problems and quick answers")
table(["Problem", "What to do"], [
    ["A student didn't get the e-mail code", "Ask them to check Spam/Junk. On the code page they can press <b>Send a new code</b>. Check the console's <b>Email log</b>: a failed e-mail has <b>Retry</b>."],
    ["A student paid but has no tokens", "Payments &rarr; open the order &rarr; <b>Re-check with provider</b>. If your bank shows the money, press <b>Confirm</b>."],
    ["A student says <i>No tokens left</i>", "They need to buy more (Pricing), or you can add tokens on their page (Manage tokens)."],
    ["A model says <i>Locked</i>", "The student has no tokens for that model. They can buy that model's tokens or switch back to GPT-5 Mini."],
    ["A student is signed out on their computer", "Only one device can be signed in at a time. Signing in on a phone signs out the computer."],
    ["Telegram messages don't arrive", "Telegram page &rarr; check the bot is <b>Online</b>. Ask the student to open the bot and press <b>Start</b> again (they may have blocked it)."],
    ["Students can't chat at all", "Model status: check <b>Pause all chat</b> is off. System health: look for red warnings."],
    ["You forgot your console password", "Another Owner can reset it on your account page, or ask your developer."],
    ["Something else", "Write down what you did, take a screenshot, and send it to your developer."],
])
tip("Every action in the console is recorded in <b>Audit logs</b>, so you can always see who changed what and when.")
add('<div class="end">EPC AI Assistant &middot; User guide</div>')


# ===========================================================================
# PAGE
# ===========================================================================
CSS = r"""
@page { size: A4; margin: 18mm 16mm 20mm 16mm; }
* { box-sizing: border-box; }
:root { --brand: #4F46E5; --brand-2: #7C3AED; --ink: #0F172A; --ink-2: #334155; --ink-3: #64748B; --line: #E2E8F0; --soft: #F8FAFC; }
html { -webkit-print-color-adjust: exact; print-color-adjust: exact; }
body { font-family: "Inter", "Segoe UI", Arial, sans-serif; color: var(--ink); font-size: 10.6pt; line-height: 1.6; margin: 0; }
.cover { height: 257mm; display: flex; flex-direction: column; justify-content: space-between; padding: 14mm 6mm 6mm; page-break-after: always;
  background: radial-gradient(120% 80% at 100% 0%, #EEF2FF 0%, #fff 55%), #fff; border-radius: 6mm; }
.cover .brand { display: flex; align-items: center; gap: 12px; font-weight: 700; font-size: 15pt; }
.cover .mark { width: 40px; height: 40px; border-radius: 11px; background: linear-gradient(135deg, var(--brand), var(--brand-2)); display: grid; place-items: center; color: #fff; font-size: 18pt; }
.cover h1 { font-size: 38pt; line-height: 1.08; letter-spacing: -1px; margin: 0 0 6mm; }
.cover h1 .hl { display: inline-block; padding-right: 1mm; background: linear-gradient(90deg, var(--brand), var(--brand-2)); -webkit-background-clip: text; background-clip: text; color: transparent; }
.cover .sub { font-size: 14pt; color: var(--ink-2); max-width: 140mm; margin: 0; }
.cover .meta { display: grid; grid-template-columns: 1fr 1fr; gap: 4mm; font-size: 10pt; }
.cover .meta div { background: #fff; border: 1px solid var(--line); border-radius: 4mm; padding: 4mm 5mm; }
.cover .meta b { display: block; font-size: 8pt; letter-spacing: .12em; text-transform: uppercase; color: var(--ink-3); margin-bottom: 1mm; }
.toc { page-break-after: always; }
.toc h1 { font-size: 22pt; margin: 0 0 6mm; }
.toc ol { list-style: none; padding: 0; margin: 0; columns: 2; column-gap: 10mm; }
.toc li { break-inside: avoid; padding: 1.2mm 0; border-bottom: 1px dotted var(--line); font-size: 9.6pt; }
.toc li.l1 { font-weight: 700; color: var(--brand); padding-top: 3mm; border-bottom: 0; }
.toc li.l2 { padding-left: 4mm; color: var(--ink-2); }
.toc a { color: inherit; text-decoration: none; }
.chapter { page-break-before: always; padding: 10mm 8mm; margin-bottom: 6mm; border-radius: 5mm; color: #fff;
  background: linear-gradient(135deg, var(--brand), var(--brand-2)); }
.chapter-no { font-size: 9pt; letter-spacing: .2em; text-transform: uppercase; opacity: .85; }
.chapter h1 { font-size: 26pt; margin: 1mm 0 2mm; letter-spacing: -.5px; }
.chapter .lead { margin: 0; font-size: 11.5pt; opacity: .95; max-width: 150mm; }
h2 { font-size: 15pt; margin: 9mm 0 3mm; padding-bottom: 2mm; border-bottom: 2px solid var(--line); page-break-after: avoid; letter-spacing: -.2px; }
h2 .num { color: var(--brand); margin-right: 3mm; }
h3 { font-size: 11.5pt; margin: 6mm 0 2mm; color: var(--ink); page-break-after: avoid; }
p { margin: 0 0 3mm; }
.where { display: inline-flex; align-items: center; gap: 2mm; font-size: 9.4pt; background: #EEF2FF; color: #3730A3; border-radius: 3mm; padding: 1.5mm 3mm; margin: 0 2mm 3mm 0; }
.where.who { background: #F0FDF4; color: #166534; }
.where .tag { font-weight: 700; font-size: 7.6pt; letter-spacing: .1em; text-transform: uppercase; opacity: .8; }
ol.steps { counter-reset: s; list-style: none; padding: 0; margin: 2mm 0 4mm; }
ol.steps li { counter-increment: s; position: relative; padding: 1.4mm 0 1.4mm 10mm; page-break-inside: avoid; }
ol.steps li::before { content: counter(s); position: absolute; left: 0; top: 1.2mm; width: 6.5mm; height: 6.5mm; border-radius: 50%;
  background: var(--brand); color: #fff; font-weight: 700; font-size: 8.6pt; display: grid; place-items: center; }
ul.bul { padding-left: 5mm; margin: 1mm 0 4mm; }
ul.bul li { margin: 1.2mm 0; }
.btn { display: inline-block; padding: 0 2mm; border-radius: 1.6mm; background: #EEF2FF; color: #3730A3; font-weight: 600; font-size: 9.6pt; white-space: nowrap; border: 1px solid #C7D2FE; }
.mi { font-weight: 600; color: var(--ink); }
.arrow { color: var(--ink-3); }
.box { border-radius: 3mm; padding: 3mm 4mm; margin: 3mm 0 4mm; font-size: 10pt; page-break-inside: avoid; }
.see { background: #F0FDF4; border-left: 4px solid #16A34A; }
.tip { background: #EFF6FF; border-left: 4px solid #2563EB; }
.careful { background: #FFF7ED; border-left: 4px solid #EA580C; }
figure.shot { margin: 3mm 0 5mm; page-break-inside: avoid; }
figure.shot img { width: 100%; border-radius: 3mm; border: 1px solid var(--line); box-shadow: 0 2mm 6mm rgba(15,23,42,.08); display: block; }
figure.shot.phone img { width: 62mm; margin: 0 auto; }
figure.shot figcaption { font-size: 8.8pt; color: var(--ink-3); margin-top: 1.5mm; text-align: center; }
table { width: 100%; border-collapse: collapse; margin: 2mm 0 5mm; font-size: 9.6pt; page-break-inside: auto; }
th { text-align: left; background: var(--soft); color: var(--ink-2); font-size: 8.4pt; text-transform: uppercase; letter-spacing: .06em; padding: 2mm 3mm; border-bottom: 1px solid var(--line); }
td { padding: 2mm 3mm; border-bottom: 1px solid var(--line); vertical-align: top; }
tr { page-break-inside: avoid; }
table.check td.cb, table.check th.cb { width: 7mm; text-align: center; font-size: 12pt; color: var(--brand); }
.pb { page-break-after: always; }
.end { margin-top: 12mm; text-align: center; color: var(--ink-3); font-size: 9pt; }
"""

toc_html = '<section class="toc"><h1>Contents</h1><ol>' + "".join(
    f'<li class="l{level}"><a href="#{anchor}">{E(text)}</a></li>' for level, text, anchor in toc
) + "</ol></section>"

cover = f"""
<section class="cover">
  <div class="brand"><div class="mark">&#10022;</div>EPC AI Assistant</div>
  <div>
    <h1>The complete <span class="hl">user guide</span></h1>
    <p class="sub">Every feature, step by step: what students see, how to run everything from the Admin console,
    and a checklist to test it all. Written in plain words, no technical knowledge needed.</p>
  </div>
  <div class="meta">
    <div><b>Student website</b>{SITE}</div>
    <div><b>Admin console</b>{ADMIN}</div>
    <div><b>For</b>Owner and staff</div>
    <div><b>Version</b>October 2026</div>
  </div>
</section>"""

doc = f"""<!doctype html><html><head><meta charset="utf-8"><title>EPC AI Assistant: User Guide</title>
<link rel="preconnect" href="https://fonts.googleapis.com"><link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;600;700&display=swap" rel="stylesheet">
<style>{CSS}</style></head><body>{cover}{toc_html}{''.join(parts)}</body></html>"""

out_html = HERE / "guide.html"
out_html.write_text(doc, encoding="utf-8")
print("wrote", out_html, "sections:", len(toc))

if "--pdf" in sys.argv:
    from playwright.sync_api import sync_playwright

    pdf = HERE / "EPC-AI-Assistant-User-Guide.pdf"
    footer = ('<div style="width:100%;font-size:7.5pt;color:#94A3B8;padding:0 16mm;display:flex;justify-content:space-between;'
              'font-family:Segoe UI,Arial,sans-serif"><span>EPC AI Assistant &middot; User guide</span>'
              '<span><span class="pageNumber"></span> / <span class="totalPages"></span></span></div>')
    with sync_playwright() as pw:
        br = pw.chromium.launch()
        page = br.new_page()
        page.goto(out_html.as_uri(), wait_until="networkidle")
        page.wait_for_timeout(800)
        page.pdf(path=str(pdf), format="A4", print_background=True, display_header_footer=True,
                 header_template="<span></span>", footer_template=footer,
                 margin={"top": "16mm", "bottom": "18mm", "left": "16mm", "right": "16mm"})
        br.close()
    print("wrote", pdf, round(pdf.stat().st_size / 1024 / 1024, 1), "MB")
