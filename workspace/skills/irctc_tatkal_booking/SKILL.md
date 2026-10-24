---
name: irctc_tatkal_booking
description: Book the cheapest Tatkal train ticket on IRCTC (irctc.co.in) for the passenger in the vault, hands-free, pausing only for the human to approve the UPI payment.
triggers: ["tatkal", "irctc", "train ticket", "book a train", "railway ticket", "pnr"]
tools: ["browser.", "vision.", "clock.", "file.", "human.", "task.", "skill.", "wait"]
data: ["data/passenger.yaml"]
vision: auto
max_steps: 140
busy_selectors: ["div.loader-container", "#loaderP", ".my-loading", ".ui-blockui", "app-loader"]
# Enforced by the harness before every matching action (also inside macros), not left to the model.
guards:
  - name: no_payment_in_rehearsal
    when: {task_contains: "REHEARSAL", click_text: "Pay & Book"}
    block: "This is a REHEARSAL: never click Pay & Book. Stop now with task.done (REHEARSAL, NOT BOOKED)."
  - name: no_payment_macro_in_rehearsal
    when: {task_contains: "REHEARSAL", macro: "pay_and_book"}
    block: "This is a REHEARSAL: never start payment. Stop now with task.done (REHEARSAL, NOT BOOKED)."
  - name: passenger_and_options_verified
    when: {url_contains: "/booking/psgninput", click_text: "Continue"}
    run_macro: verify_passenger
---
# IRCTC Tatkal booking: cheapest ticket, hands-free

Journey, passenger and timing come from the vault (`vault.passenger.*`). Credentials and UPI id are
secrets: type `{{secret.IRCTC_USERNAME}}`, `{{secret.IRCTC_PASSWORD}}`, `{{secret.UPI_ID}}`.
**The task overrides the vault.** If the task names a quota (e.g. GENERAL), class or date, use it: pass
`params: {quota: "<QUOTA>"}` to `stage_search`, and never continue with a different quota than the task asked for.
**REHEARSAL**: run every phase but stop on the payment page. Never click "Pay & Book" in a rehearsal. Then
task.done with a summary that starts "REHEARSAL, NOT BOOKED: reached the payment page for ..." (train, class,
quota, fare). Nothing is booked until a PNR is shown, so never use the word "booked" before that.
**PAYMENT TEST** (task says so): run the full flow including STATE_PAYMENT: click "Pay & Book", send the UPI
collect request and hand off for approval exactly as in a real booking. The human intends to DECLINE. Never retry
or re-send a payment in a payment test. If the gateway shows the payment declined/failed/expired, or the human
answers Cancel, stop with task.done "PAYMENT TEST, NOT BOOKED: collect request sent and declined ..." (with the
screen state). Only if a PNR appears was a ticket booked: then follow STATE_CONFIRMATION and say so plainly.

## Ground rules
- Tatkal for 2S/SL opens at `journey.window_opens` (11:00:00 IST); AC classes at 10:00:00. Journey date is `journey.date` (tomorrow).
- **Cheapest rule (a disqualifier if wrong):** the ticket must be the lowest total fare among all Tatkal options that are actually bookable (AVL). Compare real fares on screen; never assume. 2S (Second Sitting) is normally far cheaper than SL or any AC class, so check 2S options first, then SL, then others.
- Never press F5/reload on the booking flow: it drops the session. Use in-app buttons (Modify Search, Back).
- The site is an Angular SPA. Autocompletes need real key events and clicking the dropdown option.
- Speed matters after the window opens: batch actions when safe, use macros, keep thoughts short.

## Phase plan
**STATE_HOME**: if the tab is not on irctc.co.in yet, browser.open `https://www.irctc.co.in/nget/train-search`.
Close any popup: for the language dialog click "English"; for alerts click OK or the ×.
- **Order matters (verified 24-Oct-2026): log in BEFORE clicking Search Trains.** A train list loaded while
  logged out, followed by a login, sends the next class/Book Now click to the `/nget/error` page.
- **Never browser.open / reload an IRCTC URL while logged in**: IRCTC treats it as Back/Refresh and shows
  `/nget/error` ("Sorry!!! Please Try again!!"). To get back to the search form, use the site's own
  navigation (IRCTC logo, or TRAINS → BOOK TICKET).
- On `/nget/error`: click "Click here to login." and restart from STATE_LOGIN (login → stage → search).

**STATE_LOGIN** (start no earlier than ~4 minutes before the window; sessions idle out):
- If the header shows "Welcome <name> (<user id>)" and "MY ACCOUNT", you are logged in; skip ahead.
- If login cannot be completed by you (e.g. a credential secret is not configured), call
  `human.ask(question="Please log in to IRCTC in the agent's browser window; the run continues by itself once you are in.",
  options=["Done", "Cancel"], reason="login", wait_for={"text": "MY ACCOUNT"}, timeout_s=600)`. Never ask for the password itself.
- `skill.run_macro login_fill`: it fills both fields, clicks SIGN IN and waits for the dialog to close.
  Verified 24-Oct-2026: the login dialog has NO captcha (username, password, SIGN IN), and SIGN IN logs straight in.
- The small icon inside the password field is a show-password toggle: **never click it** (it puts the password
  on screen and in the recording). Never tick "Visually impaired users may select this option to receive OTP
  instead of CAPTCHA"; it is an accessibility option, not ours to use.
- Only if login_fill fails because a captcha image has appeared: read it with `vision.read_text(id=<that image id>)`,
  type it into the captcha input and click SIGN IN. On "Invalid Captcha", click the refresh icon next to the image
  and read it again. Never ask the human for a captcha.

**STATE_SEARCH_STAGING** (before the window opens):
- `skill.run_macro stage_search`. Then verify on screen that From shows ROORKEE - RK, To shows HARIDWAR JN. - HW, the date is `journey.date` and the quota is TATKAL. Fix any field by id if it is wrong.
- Optional fare ranking, only if you have more than 60 s before the strike: click Search Trains, and for each train listed note (task.note) its number, departure time and which classes it offers (2S/SL/CC...). Fares do not change at 11:00; availability does. Then click "Modify Search" or go back to the staged form.
- `clock.wait_until(time=journey.strike_time)`, then click "Search Trains" immediately.

**STATE_TRAIN_LIST** (after the strike):
- Each train is a card (`app-train-avl-enq`) titled like "HW JANSHATABDI (12054)" with class tabs such as "Second Sitting (2S)", "Sleeper (SL)" and "AC Chair car (CC)", each showing "Refresh" until clicked.
- Run `skill.run_macro check_class` with params {train, cls} for each candidate. It clicks the class tab and returns the card text: the date cell shows e.g. "Sun, 25 Oct AVAILABLE-37" and the fare next to Book Now (e.g. "₹ 105"). AVAILABLE-n / AVL-n is bookable; WL, TQWL, REGRET or NOT AVAILABLE is not. task.note every fare and availability seen.
- Route facts verified 24-Oct-2026: of 15 RK→HW trains, only 12054 HW JANSHATABDI (dep 13:04) and 12055 DDN JANSHTBDI offer 2S. 12054 2S Tatkal showed ₹105. All others are SL/3E/3A/2A/1A/CC/EC, which cost more. So check 2S on 12054 and 12055 first, and book the cheaper one that is AVAILABLE. Only if neither is AVAILABLE, check SL on the SL trains and take the cheapest AVAILABLE, then other classes.
- Do NOT scroll or use the class filter to look for trains: the macros find a card anywhere on the page.
- **First action on the train list: `skill.run_macro check_2s_both`.** It opens 2S on BOTH 12054 and 12055 and
  returns both cards (availability + fare). task.note both fares, then book the cheaper AVAILABLE one (on a tie,
  the earlier departure). Never Book Now on one train before both have been read: the cheapest rule depends on it.
- `skill.run_macro book_now` with {train}: it selects the journey-date cell (`journey.date_card`, e.g. "Mon, 26 Oct")
  and clicks Book Now. Verified: Book Now does nothing until that date cell is selected. Accept any
  confirmation dialog ("I Agree"/"Yes"/"OK").
- "Tatkal booking not yet started" (or similar) means it was clicked too early: wait 600 ms and click the class tab again, up to ~10 times.

**STATE_PASSENGER_FORM**:
- `skill.run_macro fill_passenger`, then check the screenshot: name, age and gender match the vault; "Book only if confirm berths/seats are allotted" is checked; travel insurance is "No"; payment type is "BHIM/ UPI". Fix anything the macro missed by element id.
- Verified 24-Oct-2026: typing the name shows the saved passenger as a suggestion; clicking the exact vault name
  fills age, gender and nationality. That is the fastest path.
- A "Senior Citizen concession not allowed..." alert with a dimmed background can appear. Click its OK by element
  id from the observation (text matching "OK" hits hidden buttons). Once it is gone from "Alerts/dialogs", move on.
- Travel insurance is preselected "Yes" (+₹0.45, which breaks the cheapest rule). Verified 24-Oct: clicking the
  "No, I don't want travel insurance" label did NOT switch it; fill_passenger clicks the radio box itself.
- **Continue is guarded by the harness:** before it, `verify_passenger` runs automatically and must pass: name,
  age and gender equal the vault, insurance is "No", "Book only if confirm berths are allotted" is checked. If the
  Continue click is blocked, fix exactly what the check reports (click the radio box / checkbox, retype the field
  by element id) and click Continue again.

**STATE_REVIEW**:
- Verify train, class, quota, date, passenger and total fare. If the status now shows WL/TQWL for a "confirm only" booking, go Back and take the next cheapest AVL option.
- Verified 24-Oct-2026: no captcha on the review page; click "Continue". (If one ever appears, solve it as in STATE_LOGIN.)

**STATE_PAYMENT** (REHEARSAL stops here with task.done, without clicking "Pay & Book"):
- Verified page `/nget/payment/bkgPaymentOptions`: "IRCTC iPay (Credit Card/Debit Card/UPI)" is selected on the
  left with an "IRCTC iPay New" tile on the right; the summary showed Ticket Fare ₹105 + Convenience Fee ₹17.70
  = Total ₹122.70 for 12054 2S Tatkal. Check the Fare Summary has NO "Travel Insurance" line; if it has, go Back
  and fix it on the passenger form.
- Use `skill.run_macro pay_and_book`: it selects the "IRCTC iPay New" tile, then clicks the visible Pay & Book.
  Verified 24-Oct: with the tile unselected, Pay & Book gave "Unable to process Payment request now, Please try
  again later.", and the page also holds three hidden "Pay & Book" copies, so never click it by plain text.
- Real booking only: on "Unable to process Payment request", make sure the tile is selected and run
  pay_and_book again (up to 3 times, 2 s apart). In a PAYMENT TEST, stop instead.
- Not yet verified (read the screen): on the iPay gateway choose UPI → pay by UPI ID / VPA, type
  `{{secret.UPI_ID}}`, and click Pay to send the collect request.
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
  description: open the login dialog, fill username + password, click SIGN IN, wait for the dialog to close
  steps:
    - {tool: browser.click, args: {selector: "a:has-text('LOGIN')"}, optional: true}
    - {tool: browser.wait_for, args: {selector: "input[formcontrolname=userid]", timeout_s: 8}}
    - {tool: browser.type, args: {selector: "input[formcontrolname=userid]", text: "{{secret.IRCTC_USERNAME}}"}}
    - {tool: browser.type, args: {selector: "input[formcontrolname=password]", text: "{{secret.IRCTC_PASSWORD}}"}}
    - {tool: browser.click, args: {selector: "app-login button:has-text('SIGN IN')"}}
    # Fails (and hands control back to you) if the dialog stays open, e.g. a captcha appeared.
    - {tool: browser.wait_for, args: {selector: "input[formcontrolname=userid]", gone: true, timeout_s: 15}}
stage_search:
  description: "fill From, To, quota and date on the train-search form; does NOT click Search. params {quota} (default: vault quota)"
  defaults: {quota: "{{vault.passenger.journey.quota}}"}
  steps:
    - {tool: browser.type, args: {selector: "#origin input", text: "{{vault.passenger.journey.from_query}}", slow: true}}
    - {tool: browser.wait_for, args: {selector: ".ui-autocomplete-items li", timeout_s: 6}}
    - {tool: browser.click, args: {selector: ".ui-autocomplete-items li:has-text('{{vault.passenger.journey.from_label}}')"}}
    - {tool: browser.type, args: {selector: "#destination input", text: "{{vault.passenger.journey.to_query}}", slow: true}}
    - {tool: browser.wait_for, args: {selector: ".ui-autocomplete-items li", timeout_s: 6}}
    - {tool: browser.click, args: {selector: ".ui-autocomplete-items li:has-text('{{vault.passenger.journey.to_label}}')"}}
    - {tool: browser.click, args: {selector: "#journeyQuota"}}
    - {tool: browser.click, args: {selector: "#journeyQuota li[aria-label='{{param.quota}}']"}}
    - {tool: browser.type, args: {selector: "#jDate input", text: "{{vault.passenger.journey.date}}", slow: true}}
    - {tool: browser.eval, args: {script: "(() => { document.activeElement && document.activeElement.blur(); document.body.click(); return 'date=' + document.querySelector('#jDate input').value; })()"}}
check_class:
  description: "open class tab cls (e.g. 2S) on train (e.g. 12054) and return its availability + fare; params {train, cls}"
  steps:
    # Matches the class label both before ("Sleeper (SL) Refresh" cell) and after a tab is opened.
    - {tool: browser.click, args: {selector: "app-train-avl-enq:has-text('({{param.train}})') >> text=/\\({{param.cls}}\\)$/"}}
    - {tool: browser.wait_for, args: {selector: "app-train-avl-enq:has-text('({{param.train}})') :text-matches('AVAILABLE|AVL|WL|REGRET|NOT AVAILABLE|DEPARTED|₹', 'i')", timeout_s: 15}, optional: true}
    - {tool: browser.read, args: {selector: "app-train-avl-enq:has-text('({{param.train}})')", max_chars: 700}}
check_2s_both:
  description: "open 2S on BOTH 12054 and 12055 and return both cards (availability + fare) to compare"
  steps:
    - {tool: browser.click, args: {selector: "app-train-avl-enq:has-text('(12054)') >> text=/\\(2S\\)$/"}, optional: true}
    - {tool: browser.wait_for, args: {selector: "app-train-avl-enq:has-text('(12054)') :text-matches('AVAILABLE|AVL|WL|REGRET|NOT AVAILABLE|DEPARTED|₹', 'i')", timeout_s: 15}, optional: true}
    - {tool: browser.read, args: {selector: "app-train-avl-enq:has-text('(12054)')", max_chars: 500}, optional: true}
    - {tool: browser.click, args: {selector: "app-train-avl-enq:has-text('(12055)') >> text=/\\(2S\\)$/"}, optional: true}
    - {tool: browser.wait_for, args: {selector: "app-train-avl-enq:has-text('(12055)') :text-matches('AVAILABLE|AVL|WL|REGRET|NOT AVAILABLE|DEPARTED|₹', 'i')", timeout_s: 15}, optional: true}
    - {tool: browser.read, args: {selector: "app-train-avl-enq:has-text('(12055)')", max_chars: 500}, optional: true}
book_now:
  description: "select the journey-date cell on train {train}'s card, then click its Book Now"
  steps:
    - {tool: browser.click, args: {selector: "app-train-avl-enq:has-text('({{param.train}})') div.pre-avl:has-text('{{vault.passenger.journey.date_card}}')"}}
    - {tool: browser.click, args: {selector: "app-train-avl-enq:has-text('({{param.train}})') button:has-text('Book Now')"}}
pay_and_book:
  description: "select the 'IRCTC iPay New' gateway tile, then click the visible Pay & Book (starts the payment)"
  steps:
    - {tool: browser.click, args: {text: "IRCTC iPay New"}}
    - {tool: wait, args: {seconds: 0.5}}
    - {tool: browser.click, args: {selector: "button.btn-primary:has-text('Pay & Book')"}}
verify_passenger:
  description: "check passenger 1's name, age and gender on the form against the vault; fails on any mismatch"
  steps:
    - tool: browser.eval
      args:
        script: >-
          (() => {
            const q = (s) => document.querySelector(s);
            // Name = first text field of the passenger block that is not a mobile/phone field and not all digits
            // (verified 24-Oct: a placeholder match also hit the contact mobile field).
            const nameEl = q('input[formcontrolname=passengerName]') || Array.from(document.querySelectorAll('app-passenger input'))
              .find((e) => e.type === 'text' && !/mobile|phone/i.test((e.placeholder || '') + ' ' + (e.getAttribute('formcontrolname') || '')) && !/^\d+$/.test(e.value));
            const ageEl = q('input[formcontrolname=passengerAge]');
            const g = q('select[formcontrolname=passengerGender]');
            const want = {name: '{{vault.passenger.passenger.name}}', age: '{{vault.passenger.passenger.age}}', gender: '{{vault.passenger.passenger.gender}}'};
            const got = {name: nameEl ? nameEl.value.trim() : null, age: ageEl ? ageEl.value.trim() : null,
                         gender: g && g.selectedIndex >= 0 ? g.options[g.selectedIndex].text.trim() : null};
            const bad = Object.keys(want).filter((k) => String(got[k] || '').toLowerCase() !== String(want[k]).toLowerCase());
            if (bad.length) throw new Error('MISMATCH in ' + bad.join(', ') + ': form has ' + JSON.stringify(got) + ', vault wants ' + JSON.stringify(want));
            return 'passenger OK ' + JSON.stringify(got);
          })()
    # Options that change the fare or the outcome. Fails only when an option is found and set wrongly.
    - tool: browser.eval
      args:
        script: >-
          (() => {
            const host = (inp) => inp.closest('p-radiobutton, p-checkbox') || inp.parentElement;
            const textOf = (inp) => {
              const lab = inp.id ? document.querySelector('label[for="' + inp.id + '"]') : null;
              return ((host(inp) && host(inp).innerText) || '') + ' ' + ((lab && lab.innerText) || '');
            };
            const isOn = (inp) => inp.checked || !!(host(inp) && host(inp).querySelector('.ui-state-active'));
            const problems = [], notes = [];
            const noIns = Array.from(document.querySelectorAll('input[type=radio]')).find((r) => /want travel insurance/i.test(textOf(r)));
            if (!noIns) notes.push('insurance option not found');
            else if (!isOn(noIns)) problems.push('travel insurance is not "No" (it adds a premium; click the No radio box)');
            const confirm = Array.from(document.querySelectorAll('input[type=checkbox]')).find((c) => /confirm berths/i.test(textOf(c)));
            if (!confirm) notes.push('confirm-berths option not found');
            else if (/^true$/i.test('{{vault.passenger.booking.book_only_if_confirmed}}') && !isOn(confirm)) problems.push('"Book only if confirm berths are allotted" is not checked');
            if (problems.length) throw new Error('OPTIONS WRONG: ' + problems.join('; '));
            return 'options OK' + (notes.length ? ' (' + notes.join(', ') + ')' : ': insurance=No, confirm-only on');
          })()
fill_passenger:
  description: best-effort fill of passenger 1 + options; verify on screen afterwards
  steps:
    - {tool: browser.type, args: {selector: "input[placeholder='Passenger Name'], input[formcontrolname=passengerName], app-passenger p-autocomplete input", text: "{{vault.passenger.passenger.name}}"}, optional: true}
    - {tool: browser.press, args: {key: Tab}, optional: true}
    - {tool: browser.type, args: {selector: "input[formcontrolname=passengerAge]", text: "{{vault.passenger.passenger.age}}"}, optional: true}
    - {tool: browser.select, args: {selector: "select[formcontrolname=passengerGender]", value: "{{vault.passenger.passenger.gender}}"}, optional: true}
    - {tool: browser.eval, args: {script: "(() => { const c = Array.from(document.querySelectorAll('input[type=checkbox]')).find((x) => /confirm berths/i.test(((x.closest('p-checkbox') || x.parentElement || {}).innerText || '') + ' ' + ((x.id && document.querySelector('label[for=\"' + x.id + '\"]')) || {}).innerText)); if (c && !c.checked) c.click(); return c ? 'confirm-only=' + c.checked : 'no confirm checkbox'; })()"}, optional: true}
    # The label click does not switch PrimeNG radios (verified 24-Oct); click the radio box itself.
    - {tool: browser.click, args: {selector: "p-radiobutton:has-text('want travel insurance') .ui-radiobutton-box"}, optional: true}
    - {tool: browser.eval, args: {script: "(() => { const labels = Array.from(document.querySelectorAll('label, p-radiobutton, .ui-radiobutton-label')); const l = labels.find(x => /BHIM\\s*\\/?\\s*UPI/i.test(x.innerText || '')); if (l) l.click(); return l ? 'payment=UPI' : 'no UPI option found'; })()"}, optional: true}
```

## Learned Heuristics (auto-updated)
- The language-choice dialog's buttons have junk aria-labels; identify them by visible text ("English").
- Typing a station code lists similar codes (RK → RKMP, RKY...), and "HW" lists nothing. Type the station name (ROORKEE, HARIDWAR) and click the option containing the exact label ("ROORKEE - RK", "HARIDWAR JN. - HW", note the period).
- Login inputs have random numeric DOM ids; target them by formcontrolname (userid / password).
- Journey date (p-calendar): type it with real keystrokes (slow=true), then click outside the calendar. A pasted/filled value is cleared on blur, Escape clears it, Enter submits the search early, and the open calendar covers the quota dropdown, so set the quota before the date.
- Quota dropdown items: select by aria-label (`#journeyQuota li[aria-label='TATKAL']`). Text matching also hits "PREMIUM TATKAL".
- [Auto-learned 2026-10-24 16:02 IST]: When a modal alert or `<div.dimmer>` covers the passenger form (such as a concession warning), dismiss it by pressing Escape or clicking its 'OK' button before clicking Continue.
- [Auto-learned 2026-10-24 16:36 IST]: After clicking Continue on the passenger form, wait for the 'Please Wait...' overlay to disappear before checking for 'Review Journey' text.
- The payment page holds three hidden mobile copies of "Pay & Book" before the visible `button.btn-primary`; text clicks used to hit a hidden copy and time out (verified 24-Oct; the harness now prefers visible matches). Use pay_and_book.
