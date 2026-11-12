"""Age-gate / consent-wall / overlay dismissal.

The motivating case: a page is fine, but a modal says "you must be 18" and the
only way in is to click a button. This module ranks every visible control on the
page by what clicking it *reveals*, clicks the best one, and refuses to click
anything that leaves the site, declines consent, or pays.

The ranking is deliberately biased: an ambiguous "continue" inside a modal
beats a naturally-scored link, and anything matching a refusal pattern
("exit", "under 18", "not now", "subscribe") can never win.
"""

from __future__ import annotations

from dataclasses import dataclass

# (regex, score). Higher wins. Ordering matters only for ties.
PATTERNS: list[tuple[str, int]] = [
    # --- unambiguous adult / entry confirmations ---
    (r"\bi\s*am\s*(over|above|at\s*least)?\s*(18|21)\b", 100),
    (r"\bi'?m\s*(over|above|at\s*least)?\s*(18|21)\b", 100),
    (r"\byes,?\s*i\s*am\s*(over|above|at\s*least)?\s*(18|21)?\b", 96),
    (r"\b(18|21)\s*(\+|plus)\b", 92),
    (r"\bof\s*legal\s*(drinking\s*)?age\b", 94),
    (r"^\s*(enter\s*(site|store|shop)?|enter\s*anyway)\s*$", 90),
    (r"\b(confirm|verify)\s*(my\s*)?age\b", 88),
    (r"\b(i\s*am\s*of\s*legal\s*age|old\s*enough|adult\s*content|enter)\b", 74),
    # --- consent walls ---
    (r"^\s*accept\s*(all|cookies|everything)?\s*$", 70),
    (r"^\s*(i\s*)?(agree|accept|consent)\s*$", 68),
    (r"^\s*(got\s*it|allow\s*all|ok(ay)?|understood|continue)\s*$", 60),
    (r"\baccept\s*(all|everything)\b", 66),
    # --- plain dismissal (lowest usable score) ---
    (r"^\s*(close|dismiss|no\s*thanks|not\s*now|maybe\s*later|skip|later)\s*$", 42),
    (r"^\s*[\u00d7\u2715\u2716x]\s*$", 40),
]

# Never click: leaving, declining, paying, or asserting under-age.
REFUSALS: list[str] = [
    r"\b(exit|leave|go\s*back|return\s*to)\b",
    r"\b(under|below)\s*(18|21)\b",
    r"\bi\s*am\s*(not|under)\b",
    r"\bno,?\s*(thanks|i\s*am)\b",
    r"\b(decline|reject|deny|refuse)\b",
    r"\b(unsubscribe|sign\s*out|log\s*out)\b",
    r"\b(subscribe|sign\s*up|register|create\s*account|log\s*in|sign\s*in)\b",
    r"\b(pay|purchase|buy|checkout|add\s*to\s*cart)\b",
    r"\b(download|install)\b",
]

# Clicked only when nothing better exists, and only after a real modal was seen.
MIN_SCORE = 40

GATE_JS = """
(args) => {
  const pats = args.patterns.map(p => [new RegExp(p[0], 'i'), p[1]]);
  const refusals = args.refusals.map(r => new RegExp(r, 'i'));
  const overlaySel = '[role="dialog"],[role="alertdialog"],[class*="modal"],[class*="Modal"],' +
    '[class*="overlay"],[class*="Overlay"],[class*="popup"],[class*="Popup"],' +
    '[class*="cookie"],[class*="Cookie"],[class*="consent"],[class*="Consent"],' +
    '[id*="modal"],[id*="overlay"],[id*="popup"],[id*="cookie"],[id*="consent"],' +
    '[id*="age"],[class*="age-gate"],[class*="agegate"],[class*="gdpr"]';

  const visible = (el) => {
    if (!el || !el.getBoundingClientRect) return false;
    const r = el.getBoundingClientRect();
    if (r.width < 4 || r.height < 4) return false;
    if (r.bottom < 0 || r.right < 0) return false;
    const s = getComputedStyle(el);
    if (s.visibility === 'hidden' || s.display === 'none' || parseFloat(s.opacity || '1') < 0.05) return false;
    if (el.disabled) return false;
    if (el.getAttribute('aria-hidden') === 'true') return false;
    return true;
  };

  const label = (el) => ((el.innerText || el.value || el.getAttribute('aria-label') ||
      el.getAttribute('title') || el.textContent || '') + '').replace(/\\s+/g, ' ').trim();

  const inOverlay = (el) => {
    let node = el;
    for (let i = 0; i < 12 && node && node !== document.body; i++) {
      try { if (node.matches && node.matches(overlaySel)) return true; } catch (e) {}
      const s = getComputedStyle(node);
      if (s.position === 'fixed' || s.position === 'sticky') return true;
      if (parseInt(s.zIndex || '0', 10) > 100) return true;
      node = node.parentElement;
    }
    return false;
  };

  const nodes = Array.from(document.querySelectorAll(
    'button,a,[role="button"],[role="link"],input[type="submit"],input[type="button"],[onclick]'
  ));
  let best = null;
  for (const el of nodes) {
    if (!visible(el)) continue;
    const text = label(el);
    if (!text || text.length > 90) continue;
    if (refusals.some(r => r.test(text))) continue;
    let score = 0;
    for (const [re, pts] of pats) { if (re.test(text)) { score = Math.max(score, pts); } }
    if (!score) continue;
    if (inOverlay(el)) score += 22;
    if (el.tagName === 'BUTTON' || el.getAttribute('role') === 'button') score += 4;
    const href = el.getAttribute('href') || '';
    if (href && /^https?:/i.test(href) && !href.includes(location.hostname)) score -= 45;
    if (href === '#' || href === '') score += 2;
    if (!best || score > best.score) best = {el, score, text};
  }

  // Date-of-birth gate: fill an adult date, then submit.
  let dobFilled = false;
  const dobInputs = Array.from(document.querySelectorAll('input,select'))
    .filter(el => visible(el) && /(age|year|birth|dob|bday)/i.test(
      (el.name || '') + ' ' + (el.id || '') + ' ' + (el.placeholder || '') + ' ' +
      (el.getAttribute('aria-label') || '') + ' ' + (el.title || '')));
  if (dobInputs.length) {
    for (const el of dobInputs) {
      const tag = el.tagName.toLowerCase();
      const type = (el.type || '').toLowerCase();
      try {
        if (type === 'date') { el.value = '1990-01-01'; dobFilled = true; }
        else if (tag === 'select') {
          const opts = Array.from(el.options || []);
          const hit = opts.find(o => /^(1990|1989|1988|1991)/.test((o.value || '') + (o.text || '')))
            || opts.find(o => /(19[6-9]\\d)/.test(o.text || ''));
          if (hit) { el.value = hit.value; dobFilled = true; }
        } else if (type === 'number' || type === 'text' || type === '' || type === 'tel') {
          const v = /year|19\\d\\d/i.test((el.name || '') + (el.id || '')) ? '1990' : '1990-01-01';
          el.value = v;
          el.dispatchEvent(new Event('input', {bubbles: true}));
          el.dispatchEvent(new Event('change', {bubbles: true}));
          dobFilled = true;
        }
      } catch (e) {}
    }
  }

  const click = (el) => {
    try {
      el.scrollIntoView({block: 'center'});
      el.click();
      if (!el.disabled) {
        el.dispatchEvent(new MouseEvent('mousedown', {bubbles: true}));
        el.dispatchEvent(new MouseEvent('mouseup', {bubbles: true}));
        el.dispatchEvent(new MouseEvent('click', {bubbles: true}));
      }
      return true;
    } catch (e) { return false; }
  };

  if (best && best.score >= args.minScore) {
    const ok = click(best.el);
    return {clicked: ok, text: best.text, score: best.score, dobFilled,
            candidates: nodes.length, overlayFound: inOverlay(best.el)};
  }
  if (dobFilled) {
    // A DOB form usually submits through the same gate button; pick the best
    // submit-ish control that survived the refusal filter.
    const submit = nodes.map(el => ({el, text: label(el)})).filter(o =>
      visible(o.el) && /(enter|continue|submit|confirm|verify|ok|go)\\b/i.test(o.text) &&
      !refusals.some(r => r.test(o.text))).sort((a, b) => a.text.length - b.text.length)[0];
    if (submit) { click(submit.el); return {clicked: true, text: submit.text, score: 50, dobFilled: true,
                                            candidates: nodes.length, overlayFound: true}; }
  }
  return {clicked: false, text: '', score: 0, dobFilled,
          candidates: nodes.length, overlayFound: false};
}
"""


@dataclass
class GateOutcome:
    clicked: bool = False
    label: str = ""
    score: int = 0
    dob_filled: bool = False
    overlay: bool = False
    candidates: int = 0


def pattern_payload() -> dict:
    return {
        "patterns": [[re, score] for re, score in PATTERNS],
        "refusals": list(REFUSALS),
        "minScore": MIN_SCORE,
    }


def outcome_from_js(data: dict) -> GateOutcome:
    if not isinstance(data, dict):
        return GateOutcome()
    return GateOutcome(
        clicked=bool(data.get("clicked")),
        label=str(data.get("text") or "")[:120],
        score=int(data.get("score") or 0),
        dob_filled=bool(data.get("dobFilled")),
        overlay=bool(data.get("overlayFound")),
        candidates=int(data.get("candidates") or 0),
    )


def looks_gated(html: str) -> bool:
    """Cheap pre-check so the browser only burns a click round when it should."""
    low = (html or "")[:40000].lower()
    signals = (
        "age verification", "age gate", "verify your age", "are you 18", "are you over 18",
        "must be 18", "18 years or older", "enter your date of birth", "date of birth",
        "confirm your age", "cookie consent", "we value your privacy", "gdpr",
        "before you continue", "consent to the use of cookies",
    )
    return any(s in low for s in signals)