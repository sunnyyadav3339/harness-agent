// Indexes visible interactive elements in one frame, tags each with
// data-agent-id, and returns a compact description for the planner.
// Elements hidden behind a modal/backdrop are excluded via elementFromPoint,
// so the list always reflects what a person could actually click.
(args) => {
  const {
    maxElements = 180,
    idOffset = 0,
    includeOffscreen = false,
    textLimit = 1500,
    withText = true,
  } = args || {};

  for (const el of document.querySelectorAll('[data-agent-id]')) {
    el.removeAttribute('data-agent-id');
  }

  const vw = window.innerWidth, vh = window.innerHeight;
  const SEL = [
    'a[href]', 'button', 'input:not([type=hidden])', 'select', 'textarea', 'summary',
    '[role=button]', '[role=link]', '[role=checkbox]', '[role=radio]', '[role=tab]',
    '[role=option]', '[role=menuitem]', '[role=combobox]', '[role=switch]',
    '[role=treeitem]', '[role=gridcell]', '[onclick]', '[contenteditable=true]',
    '[tabindex]:not([tabindex="-1"])', 'label[for]',
  ].join(',');

  const candidates = Array.from(document.querySelectorAll(SEL));
  // Images inside forms/dialogs (captchas, QR codes) are not clickable but the
  // agent must be able to point OCR or a screenshot at them.
  for (const img of document.querySelectorAll('form img, [role=dialog] img, .ui-dialog img, .modal img, canvas')) {
    const r = img.getBoundingClientRect();
    if (r.width >= 50 && r.height >= 18) candidates.push(img);
  }
  const inSel = new Set(candidates);
  // Framework widgets often use plain divs/spans with cursor:pointer.
  let scanned = 0;
  for (const el of document.querySelectorAll('li,span,div,td,img,i,strong,p')) {
    if (++scanned > 4000) break;
    if (inSel.has(el)) continue;
    const cs = getComputedStyle(el);
    if (cs.cursor !== 'pointer') continue;
    const parent = el.parentElement;
    if (parent && getComputedStyle(parent).cursor === 'pointer') continue;
    candidates.push(el);
  }

  const clean = (s) => (s || '').replace(/\s+/g, ' ').trim();

  function isVisible(el, r) {
    if (r.width < 2 || r.height < 2) return false;
    const cs = getComputedStyle(el);
    if (cs.visibility === 'hidden' || cs.display === 'none' || parseFloat(cs.opacity) < 0.05) return false;
    const off = r.bottom < 0 || r.right < 0 || r.top > vh || r.left > vw;
    if (off && !includeOffscreen) return false;
    if (!off) {
      const x = Math.min(Math.max(r.left + r.width / 2, 0), vw - 1);
      const y = Math.min(Math.max(r.top + r.height / 2, 0), vh - 1);
      const hit = document.elementFromPoint(x, y);
      if (hit && hit !== el && !el.contains(hit) && !hit.contains(el)) {
        // Allow a label/wrapper that belongs to the same control.
        const lbl = hit.closest && hit.closest('label');
        if (!(lbl && (lbl.control === el || lbl.contains(el)))) return false;
      }
    }
    return true;
  }

  function describe(el) {
    const tag = el.tagName.toLowerCase();
    const aria = clean(el.getAttribute('aria-label'));
    if (tag === 'input' || tag === 'textarea') {
      const type = (el.getAttribute('type') || 'text').toLowerCase();
      const parts = [];
      if (aria) parts.push(aria);
      const lab = el.labels && el.labels[0] ? clean(el.labels[0].innerText) : '';
      if (lab && lab !== aria) parts.push(lab);
      if (el.placeholder) parts.push(`placeholder="${clean(el.placeholder)}"`);
      if (type === 'checkbox' || type === 'radio') {
        parts.push(el.checked ? 'checked' : 'unchecked');
        if (el.value && el.value !== 'on') parts.push(`value="${el.value}"`);
      } else if (el.value) {
        parts.push(type === 'password' ? 'value=***' : `value="${clean(el.value).slice(0, 60)}"`);
      }
      return parts.join(' ');
    }
    if (tag === 'select') {
      const sel = el.selectedOptions && el.selectedOptions[0];
      const opts = Array.from(el.options).slice(0, 12).map(o => clean(o.text)).join('|');
      return `${aria} selected="${sel ? clean(sel.text) : ''}" options=[${opts}]`.trim();
    }
    // Visible text first (what a person reads); aria only as a short supplement,
    // because frameworks often leak junk into aria-label.
    if (tag === 'img' || tag === 'canvas') {
      const cls = typeof el.className === 'string' ? el.className : '';
      return `image ${clean(el.alt) || clean(el.title) || cls.split(/\s+/)[0] || ''} ${Math.round(el.width)}x${Math.round(el.height)}`.trim();
    }
    const txt = clean(el.innerText || el.textContent);
    const goodAria = aria && aria.length <= 60 && !aria.includes('<') ? aria : '';
    let label = txt || goodAria || clean(el.title) || clean(el.alt) || clean(el.getAttribute('name'));
    if (txt && goodAria && goodAria.toLowerCase() !== txt.toLowerCase() && txt.length < 40) label = `${txt} (${goodAria})`;
    return label.slice(0, 100);
  }

  const out = [];
  const taken = new Set();
  const containerTags = new Set(['a', 'button']);
  let id = idOffset;
  for (const el of candidates) {
    if (out.length >= maxElements) break;
    if (taken.has(el)) continue;
    // Skip children of an already-indexed button/link (the parent is enough).
    let p = el.parentElement, skip = false;
    while (p) {
      if (taken.has(p) && (containerTags.has(p.tagName.toLowerCase()) || p.getAttribute('role') === 'button')) {
        skip = true; break;
      }
      p = p.parentElement;
    }
    if (skip) continue;
    const r = el.getBoundingClientRect();
    if (!isVisible(el, r)) continue;
    const tag = el.tagName.toLowerCase();
    const text = describe(el);
    if (!text && !['input', 'textarea', 'select', 'button'].includes(tag)) {
      if (!(tag === 'img' || tag === 'i' || el.querySelector('img,svg,i'))) continue;
    }
    el.setAttribute('data-agent-id', String(id));
    taken.add(el);
    const attrs = {};
    for (const a of ['type', 'name', 'id', 'role', 'href', 'aria-expanded', 'aria-selected', 'formcontrolname']) {
      const v = el.getAttribute(a);
      if (v) attrs[a] = a === 'href' ? v.slice(0, 80) : v.slice(0, 60);
    }
    if (el.disabled || el.getAttribute('aria-disabled') === 'true') attrs.disabled = true;
    if (typeof el.className === 'string' && el.className) attrs.class = el.className.split(/\s+/).slice(0, 3).join(' ');
    out.push({
      id, tag, text,
      attrs,
      rect: [Math.round(r.left), Math.round(r.top), Math.round(r.width), Math.round(r.height)],
      offscreen: r.bottom < 0 || r.top > vh,
    });
    id++;
  }

  const alerts = [];
  for (const el of document.querySelectorAll(
    '[role=alert],[role=alertdialog],[role=dialog],[aria-live=assertive],[aria-live=polite],.alert,.toast,.ui-toast,.ui-messages,.error,.ui-dialog,.modal.show')) {
    const r = el.getBoundingClientRect();
    if (r.width < 2 || r.height < 2) continue;
    const t = clean(el.innerText).slice(0, 300);
    if (t && !alerts.includes(t)) alerts.push(t);
    if (alerts.length >= 6) break;
  }

  return {
    elements: out,
    nextId: id,
    url: location.href,
    title: document.title,
    alerts,
    text: withText ? clean(document.body ? document.body.innerText : '').slice(0, textLimit) : '',
    scroll: [Math.round(window.scrollX), Math.round(window.scrollY)],
    pageHeight: document.documentElement.scrollHeight,
    viewport: [vw, vh],
  };
}
