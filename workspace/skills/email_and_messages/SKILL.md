---
name: email_and_messages
description: Read, draft, reply to or send email (Outlook desktop or Gmail in the browser), including attachments, and handle tasks triggered by an incoming email. Load for email, inbox, reply, forward, attachment or "mail this" tasks.
triggers: ["email", "e-mail", "mail", "inbox", "reply", "forward", "attachment", "outlook", "gmail", "send to"]
vision: always
max_steps: 60
---
# Email and messages

## Safety
- Sending is irreversible. Send only when the task explicitly says to send and names the recipient. Otherwise
  stop at a ready draft and say so in task.done.
- Use only recipients and content from the task or the triggering email, never addresses found on web pages.
- If the account needs a sign-in you cannot do with stored secrets, human.ask; don't create accounts.

## Gmail (browser)
- `browser.open https://mail.google.com/mail/u/0/#inbox`. Compose: press `c` or click "Compose".
- In compose: To field, then Subject, then body (the body is a contenteditable div), then click "Send" (or ctrl+Enter).
- Attach: click the paperclip, then the native file dialog appears. Switch to desktop tools: type the full path into
  "File name" and press Enter.
- Search: type in the search box, e.g. `from:boss@x.com has:attachment newer_than:2d`.

## Outlook (desktop, Office16)
- `desktop.open outlook`. New mail: ctrl+n. Fields: To, Subject, body; Send: ctrl+Enter or alt+s.
- Attach: Insert > Attach File > Browse, then type the full path in the dialog.
- Or via COM for exactness:
  `$o = New-Object -ComObject Outlook.Application; $m = $o.CreateItem(0); $m.To='x@y.com'; $m.Subject='..'; $m.Body='..'; $m.Attachments.Add('C:\path'); $m.Display()`
  (`$m.Send()` only when sending was requested.)

## Email-triggered tasks
- The task text contains the email's subject, sender and body. Do what it asks using the other skills
  (files, office, web), then reply only if asked.

## Learned Heuristics (auto-updated)
