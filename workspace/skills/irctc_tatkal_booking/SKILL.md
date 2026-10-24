---
name: irctc_tatkal_booking
description: Book the cheapest Tatkal train ticket on IRCTC (irctc.co.in) for the passenger in the vault, hands-free, pausing only for the human to approve the UPI payment.
triggers: ["tatkal", "irctc", "train ticket", "book a train", "railway ticket", "pnr"]
tools: ["browser.", "vision.", "clock.", "file.", "human.", "task.", "skill.", "wait"]
data: ["data/passenger.yaml"]
vision: auto
max_steps: 140
busy_selectors: ["div.loader-container", "#loaderP", ".my-loading", ".ui-blockui", "app-loader"]
---
# IRCTC Tatkal booking: cheapest ticket, hands-free

Journey, passenger and timing come from the vault (`vault.passenger.*`). Credentials and UPI id are
secrets: type `{{secret.IRCTC_USERNAME}}`, `{{secret.IRCTC_PASSWORD}}`, `{{secret.UPI_ID}}`.
If the task says REHEARSAL: run every phase but stop on the payment page, without paying, then task.done.

## Ground rules
- Tatkal for 2S/SL opens at `journey.window_opens` (11:00:00 IST); AC classes at 10:00:00. Journey date is `journey.date` (tomorrow).
- **Cheapest rule (a disqualifier if wrong):** the ticket must be the lowest total fare among all Tatkal options that are actually bookable (AVL). Compare real fares on screen; never assume. 2S (Second Sitting) is normally far cheaper than SL or any AC class, so check 2S options first, then SL, then others.
- Never press F5/reload on the booking flow: it drops the session. Use in-app buttons (Modify Search, Back).
- The site is an Angular SPA. Autocompletes need real key events and clicking the dropdown option.
- Speed matters after the window opens: batch actions when safe, use macros, keep thoughts short.

## Phase plan
**STATE_HOME**: browser.open `https://www.irctc.co.in/nget/train-search`. Close any popup: for the language dialog click "English"; for alerts click OK or the ×.

**STATE_LOGIN** (start no earlier than ~4 minutes before the window; sessions idle out):
- If the header shows the user's name or "Logout", you are logged in; skip ahead.
- `skill.run_macro login_fill`, then click "SIGN IN".
- A captcha image may appear in the dialog. Read it with `vision.read_text(id=<captcha image id>)`, type the text into the captcha input, and submit. On "Invalid Captcha", try the alternate reading if one was given, otherwise click the refresh icon next to the image and read it again. Never ask the human for a captcha.
- Verify the login dialog has closed and the header shows the account.

**STATE_SEARCH_STAGING** (before the window opens):
- `skill.run_macro stage_search`. Then verify on screen that From shows ROORKEE - RK, To shows HARIDWAR JN. - HW, the date is `journey.date` and the quota is TATKAL. Fix any field by id if it is wrong.
- Optional fare ranking, only if you have more than 60 s before the strike: click Search Trains, and for each train listed note (task.note) its number, departure time and which classes it offers (2S/SL/CC...). Fares do not change at 11:00; availability does. Then click "Modify Search" or go back to the staged form.
- `clock.wait_until(time=journey.strike_time)`, then click "Search Trains" immediately.

**STATE_TRAIN_LIST** (after the strike):
- Each train is a card (`app-train-avl-enq`) titled like "HW JANSHATABDI (12054)" with class tabs such as "Second Sitting (2S)", "Sleeper (SL)" and "AC Chair car (CC)", each showing "Refresh" until clicked.
- Run `skill.run_macro check_class` with params {train, cls} for each candidate. It clicks the class tab and returns the card text: the date cell shows e.g. "Sun, 25 Oct AVAILABLE-37" and the fare next to Book Now (e.g. "₹ 105"). AVAILABLE-n / AVL-n is bookable; WL, TQWL, REGRET or NOT AVAILABLE is not. task.note every fare and availability seen.
- Route facts verified 24-Oct-2026: of 15 RK→HW trains, only 12054 HW JANSHATABDI (dep 13:04) and 12055 DDN JANSHTBDI offer 2S. 12054 2S Tatkal showed ₹105. All others are SL/3E/3A/2A/1A/CC/EC, which cost more. So check 2S on 12054 and 12055 first, and book the cheaper one that is AVAILABLE. Only if neither is AVAILABLE, check SL on the SL trains and take the cheapest AVAILABLE, then other classes.
- Make sure the journey-date cell is selected (click it if Book Now looks disabled), then click "Book Now" on that card (`skill.run_macro book_now` with {train}). Accept any confirmation dialog ("I Agree"/"Yes"/"OK").
- "Tatkal booking not yet started" (or similar) means it was clicked too early: wait 600 ms and click the class tab again, up to ~10 times.

**STATE_PASSENGER_FORM**:
- `skill.run_macro fill_passenger`, then check the screenshot: name, age and gender match the vault; "Book only if confirm berths/seats are allotted" is checked; travel insurance is "No"; payment type is "BHIM/ UPI". Fix anything the macro missed by element id.
- If the name field opens a saved-passenger suggestion that matches the vault name exactly, picking it is fine.
- Click "Continue".

**STATE_REVIEW**:
- Verify train, class, date, passenger and total fare. If the status now shows WL/TQWL for a "confirm only" booking, go Back and take the next cheapest AVL option.
- If a captcha is shown, solve it exactly as for login, then click "Continue".

**STATE_PAYMENT** (REHEARSAL stops here with task.done):
- Choose the BHIM/UPI option (e.g. "IRCTC iPay" or "Pay through BHIM/UPI"), choose to pay by UPI ID / VPA, type `{{secret.UPI_ID}}`, and click Pay to send the collect request.
- Immediately call `human.ask(question="Approve the UPI collect request of ₹<fare> for <train> <class> on your phone", options=["Paid", "Cancel"], reason="payment", wait_for={"text": "PNR"}, timeout_s=420)`. The run resumes on its own when the confirmation page shows a PNR.

**STATE_CONFIRMATION**:
- Wait for the booking confirmation with PNR and status (CNF/AVL). Read the PNR (browser.read).
- `browser.screenshot(path="ticket_pnr_proof.png", full_page=true)`, and task.note the PNR, train, class and fare.
- task.done with PNR, train, class, fare and the proof path.

## Recovery playbook
- Spinner for over 10 s after Search or Book Now: wait up to 20 s more (browser.wait_for with gone=true on the spinner), then click "Modify Search" → "Search" again. Do not reload.
- "Unable to process current transaction", "Something went wrong" or an HTTP error toast: close the toast and repeat the last click after 1 s, up to 5 times.
- Logged out mid-flow (login dialog reappears or the URL has /error): log in again (STATE_LOGIN), then return to the train list via Search with the same staged values.
- Chosen option turns WL/TQWL/REGRET before payment: go back and take the next cheapest AVL option from your notes.
- Payment page error or timeout before the human approved: retry the UPI collect once; if it fails again, human.ask whether to retry with the QR code.
- Any unexpected modal: read it, and if it is informational, close it and continue.

## Fast-path macros
```yaml macros
login_fill:
  description: open the login dialog and fill the username and password (you handle the captcha and SIGN IN)
  steps:
    - {tool: browser.click, args: {selector: "a:has-text('LOGIN')"}, optional: true}
    - {tool: browser.wait_for, args: {selector: "input[formcontrolname=userid]", timeout_s: 8}}
    - {tool: browser.type, args: {selector: "input[formcontrolname=userid]", text: "{{secret.IRCTC_USERNAME}}"}}
    - {tool: browser.type, args: {selector: "input[formcontrolname=password]", text: "{{secret.IRCTC_PASSWORD}}"}}
stage_search:
  description: fill From, To, date and TATKAL quota on the train-search form; does NOT click Search
  steps:
    - {tool: browser.type, args: {selector: "#origin input", text: "{{vault.passenger.journey.from_query}}", slow: true}}
    - {tool: browser.wait_for, args: {selector: ".ui-autocomplete-items li", timeout_s: 6}}
    - {tool: browser.click, args: {selector: ".ui-autocomplete-items li:has-text('{{vault.passenger.journey.from_label}}')"}}
    - {tool: browser.type, args: {selector: "#destination input", text: "{{vault.passenger.journey.to_query}}", slow: true}}
    - {tool: browser.wait_for, args: {selector: ".ui-autocomplete-items li", timeout_s: 6}}
    - {tool: browser.click, args: {selector: ".ui-autocomplete-items li:has-text('{{vault.passenger.journey.to_label}}')"}}
    - {tool: browser.click, args: {selector: "#journeyQuota"}}
    - {tool: browser.click, args: {selector: "#journeyQuota li[aria-label='{{vault.passenger.journey.quota}}']"}}
    - {tool: browser.type, args: {selector: "#jDate input", text: "{{vault.passenger.journey.date}}", slow: true}}
    - {tool: browser.eval, args: {script: "(() => { document.activeElement && document.activeElement.blur(); document.body.click(); return 'date=' + document.querySelector('#jDate input').value; })()"}}
check_class:
  description: "open class tab cls (e.g. 2S) on train (e.g. 12054) and return its availability + fare; params {train, cls}"
  steps:
    # Matches the class label both before ("Sleeper (SL) Refresh" cell) and after a tab is opened.
    - {tool: browser.click, args: {selector: "app-train-avl-enq:has-text('({{param.train}})') >> text=/\\({{param.cls}}\\)$/"}}
    - {tool: browser.wait_for, args: {selector: "app-train-avl-enq:has-text('({{param.train}})') :text-matches('AVAILABLE|AVL|WL|REGRET|NOT AVAILABLE|DEPARTED|₹', 'i')", timeout_s: 15}, optional: true}
    - {tool: browser.read, args: {selector: "app-train-avl-enq:has-text('({{param.train}})')", max_chars: 700}}
book_now:
  description: "click Book Now on the card of train {train}"
  steps:
    - {tool: browser.click, args: {selector: "app-train-avl-enq:has-text('({{param.train}})') button:has-text('Book Now')"}}
fill_passenger:
  description: best-effort fill of passenger 1 + options; verify on screen afterwards
  steps:
    - {tool: browser.type, args: {selector: "input[formcontrolname=passengerName], app-passenger input[placeholder='Name']", text: "{{vault.passenger.passenger.name}}"}, optional: true}
    - {tool: browser.press, args: {key: Tab}, optional: true}
    - {tool: browser.type, args: {selector: "input[formcontrolname=passengerAge]", text: "{{vault.passenger.passenger.age}}"}, optional: true}
    - {tool: browser.select, args: {selector: "select[formcontrolname=passengerGender]", value: "{{vault.passenger.passenger.gender}}"}, optional: true}
    - {tool: browser.eval, args: {script: "(() => { const c = document.querySelector('#confirmberths'); if (c && !c.checked) c.click(); return c ? 'confirm-only=' + c.checked : 'no confirm checkbox'; })()"}, optional: true}
    - {tool: browser.eval, args: {script: "(() => { const r = Array.from(document.querySelectorAll('input[type=radio][name*=nsurance]')).find(x => String(x.value) === 'false'); if (r) r.click(); return r ? 'insurance=no' : 'no insurance radio'; })()"}, optional: true}
    - {tool: browser.eval, args: {script: "(() => { const labels = Array.from(document.querySelectorAll('label, p-radiobutton, .ui-radiobutton-label')); const l = labels.find(x => /BHIM\\s*\\/?\\s*UPI/i.test(x.innerText || '')); if (l) l.click(); return l ? 'payment=UPI' : 'no UPI option found'; })()"}, optional: true}
```

## Learned Heuristics (auto-updated)
- The language-choice dialog's buttons have junk aria-labels; identify them by visible text ("English").
- Typing a station code lists similar codes (RK → RKMP, RKY...), and "HW" lists nothing. Type the station name (ROORKEE, HARIDWAR) and click the option containing the exact label ("ROORKEE - RK", "HARIDWAR JN. - HW", note the period).
- Login inputs have random numeric DOM ids; target them by formcontrolname (userid / password).
- Journey date (p-calendar): type it with real keystrokes (slow=true), then click outside the calendar. A pasted/filled value is cleared on blur, Escape clears it, Enter submits the search early, and the open calendar covers the quota dropdown, so set the quota before the date.
- Quota dropdown items: select by aria-label (`#journeyQuota li[aria-label='TATKAL']`). Text matching also hits "PREMIUM TATKAL".
