"""Drives one application page with Playwright: find the form, answer what it can, report the rest.

Safety rules, enforced here: this module never clicks a submit/next/continue button, never ticks consent boxes,
and only clicks an 'Apply' entry button to reach the form itself."""
from __future__ import annotations

import logging
import re
import time
from typing import Any, Dict, List, Optional, Tuple

from .matching import Answer, Question, choose_option, resolve
from .profile import Profile

log = logging.getLogger("jobhunter.apply")

COLLECT_JS = """
(frameIdx) => {
  const out = [];
  let n = 0;
  document.querySelectorAll('[data-jh]').forEach(e => e.removeAttribute('data-jh'));     // ids from an earlier scan are stale
  const txt = el => ((el && (el.innerText || el.textContent)) || '').replace(/\\s+/g, ' ').trim();
  const shown = el => {
    const r = el.getBoundingClientRect(); const s = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none';
  };
  const cleanLabel = el => {
    const c = el.cloneNode(true);
    c.querySelectorAll('input,select,textarea,button').forEach(x => x.remove());
    return txt(c);
  };
  const ownLabel = el => {
    const ids = (el.getAttribute('aria-labelledby') || '').split(/\\s+/).filter(Boolean);
    let t = ids.map(i => txt(document.getElementById(i))).join(' ').trim();
    if (t) return t;
    if (el.id) {
      const l = document.querySelector('label[for="' + CSS.escape(el.id) + '"]');
      if (l) { const tt = cleanLabel(l); if (tt) return tt; }
    }
    const wl = el.closest('label');
    if (wl) { const tt = cleanLabel(wl); if (tt) return tt; }
    return el.getAttribute('aria-label') || '';
  };
  const groupLabel = (el, controls) => {
    const fs = el.closest('fieldset');
    if (fs) { const lg = fs.querySelector('legend'); if (lg && txt(lg)) return txt(lg); }
    const rg = el.closest('[role=radiogroup],[role=group]');
    if (rg) {
      const a = rg.getAttribute('aria-label'); if (a) return a;
      const ids = (rg.getAttribute('aria-labelledby') || '').split(/\\s+/).filter(Boolean);
      const t = ids.map(i => txt(document.getElementById(i))).join(' ').trim(); if (t) return t;
    }
    let p = el.parentElement;
    for (let i = 0; i < 5 && p; i++, p = p.parentElement) {
      const cands = p.querySelectorAll('legend, label, [class*="label" i], [class*="question" i], [class*="title" i], h3, h4, h5');
      for (const l of cands) {
        if (controls.some(c => l.contains(c) || (l.htmlFor && l.htmlFor === c.id))) continue;
        const tt = txt(l);
        if (tt && tt.length < 300) return tt;
      }
    }
    return '';
  };
  const nearLabel = el => {
    let t = ownLabel(el);
    if (t) return t;
    let p = el.parentElement;
    for (let i = 0; i < 4 && p; i++, p = p.parentElement) {
      const l = p.querySelector('label, legend, [class*="label" i]');
      if (l && !l.contains(el)) { const tt = txt(l); if (tt && tt.length < 300) return tt; }
    }
    return el.getAttribute('placeholder') || el.getAttribute('name') || '';
  };
  const mark = el => { const id = 'jh' + (n++); el.setAttribute('data-jh', id); return id; };
  const isReq = (el, label) => el.required || el.getAttribute('aria-required') === 'true'
        || /[*\\u2731]/.test(label) || /\\brequired\\b/i.test(label);

  const seenRadio = {};
  document.querySelectorAll('input, select, textarea').forEach(el => {
    const type = (el.getAttribute('type') || (el.tagName === 'TEXTAREA' ? 'textarea' : 'text')).toLowerCase();
    if (['hidden', 'submit', 'button', 'image', 'reset', 'search', 'range', 'color', 'password'].includes(type)) return;
    if (el.disabled || el.readOnly) return;
    const name = el.getAttribute('name') || '';
    if (el.tagName === 'INPUT' && type === 'radio') {
      const key = name || el.id;
      if (seenRadio[key]) return;
      seenRadio[key] = true;
      const group = name ? Array.from(document.querySelectorAll('input[type=radio]')).filter(r => r.name === name) : [el];
      const opts = group.map(r => {
        let l = '';
        if (r.id) { const lf = document.querySelector('label[for="' + CSS.escape(r.id) + '"]'); if (lf) l = cleanLabel(lf); }
        if (!l && r.closest('label')) l = cleanLabel(r.closest('label'));
        if (!l) l = r.getAttribute('aria-label') || r.value || '';
        return {label: l, id: mark(r)};
      });
      const label = groupLabel(group[0], group);
      out.push({id: opts[0].id, kind: 'radio', label: label, options: opts, required: group.some(r => r.required) || isReq(group[0], label),
                input_type: 'radio', name: name, placeholder: '', value: group.some(r => r.checked) ? 'on' : '', frame: frameIdx});
      return;
    }
    if (el.tagName === 'INPUT' && type === 'checkbox') {
      const label = nearLabel(el);
      out.push({id: mark(el), kind: 'checkbox', label: label, options: [], required: isReq(el, label),
                input_type: 'checkbox', name: name, placeholder: '', value: el.checked ? 'on' : '', frame: frameIdx});
      return;
    }
    if (el.tagName === 'INPUT' && type === 'file') {
      const label = nearLabel(el);
      out.push({id: mark(el), kind: 'file', label: label, options: [], required: isReq(el, label),
                input_type: 'file', name: name + ' ' + (el.id || ''), placeholder: '',
                value: (el.files && el.files.length) ? 'file' : '', frame: frameIdx});
      return;
    }
    if (!shown(el)) return;
    const label = nearLabel(el);
    const role = (el.getAttribute('role') || '').toLowerCase();
    const combo = role === 'combobox' || el.getAttribute('aria-haspopup') === 'listbox'
                  || /select__input|combobox/i.test(el.className || '');
    if (el.tagName === 'SELECT') {
      const opts = Array.from(el.options).map((o, i) => ({label: (o.text || '').trim(), id: String(i)}));
      const so = el.options[el.selectedIndex];
      const chosen = (so && so.value && !/^(select|choose|please|--|pick)/i.test((so.text || '').trim())) ? (so.text || '').trim() : '';
      out.push({id: mark(el), kind: 'select', label: label, options: opts, required: isReq(el, label), input_type: 'select',
                name: name, placeholder: '', value: chosen, frame: frameIdx});
      return;
    }
    out.push({id: mark(el), kind: combo ? 'combobox' : (el.tagName === 'TEXTAREA' ? 'textarea' : 'text'), label: label,
              options: [], required: isReq(el, label), input_type: type, name: name,
              placeholder: el.getAttribute('placeholder') || '', value: el.value || '', frame: frameIdx,
              maxlength: el.maxLength > 0 ? el.maxLength : 0});
  });

  // custom widgets that are not <input>/<select>: button-style radio groups and dropdown buttons
  document.querySelectorAll('[role=radiogroup]').forEach(g => {
    const items = Array.from(g.querySelectorAll('[role=radio]')).filter(x => x.tagName !== 'INPUT');
    if (!items.length) return;
    const opts = items.map(it => ({label: txt(it) || it.getAttribute('aria-label') || '', id: mark(it)}));
    const label = g.getAttribute('aria-label') || groupLabel(g, items);
    out.push({id: opts[0].id, kind: 'radio', label: label, options: opts, required: isReq(g, label), input_type: 'radio',
              name: g.id || '', placeholder: '', value: items.some(it => it.getAttribute('aria-checked') === 'true') ? 'on' : '',
              frame: frameIdx});
  });
  document.querySelectorAll('button[aria-haspopup="listbox"], [role=combobox]:not(input):not(select)').forEach(el => {
    if (!shown(el)) return;
    const label = nearLabel(el);
    const t = txt(el);
    const empty = !t || /^(select|choose|please|--|pick)/i.test(t);
    out.push({id: mark(el), kind: 'combobox', label: label, options: [], required: isReq(el, label), input_type: 'combobox',
              name: el.getAttribute('name') || el.id || '', placeholder: '', value: empty ? '' : t, frame: frameIdx});
  });
  return out;
}
"""

ENTRY_JS = """
() => {
  const re = /^(apply( now| here| today)?( for (this|the) (job|role|position))?|apply to this job|i.?m interested|start (your )?application|apply for this job)$/i;
  const bad = /linkedin|indeed|google|facebook|github|with (a )?(resume|cv)|easy apply/i;
  for (const el of document.querySelectorAll('a, button, [role=button]')) {
    const t = ((el.innerText || el.getAttribute('aria-label') || '') + '').replace(/\\s+/g, ' ').trim();
    if (!t || t.length > 40 || bad.test(t) || !re.test(t)) continue;
    const r = el.getBoundingClientRect();
    if (r.width === 0 || r.height === 0) continue;
    document.querySelectorAll('[data-jh-entry]').forEach(x => x.removeAttribute('data-jh-entry'));
    el.setAttribute('data-jh-entry', '1');
    return t;
  }
  return '';
}
"""

CAPTCHA_JS = """
() => {
  const html = document.documentElement.innerHTML.slice(0, 400000).toLowerCase();
  const frames = Array.from(document.querySelectorAll('iframe')).map(f => (f.src || '').toLowerCase()).join(' ');
  const hit = /recaptcha|hcaptcha|turnstile|arkoselabs|funcaptcha|geetest|captcha-delivery/.test(frames)
           || document.querySelector('.g-recaptcha, .h-captcha, .cf-turnstile, [data-sitekey]') !== null
           || /verify (that )?you are (a )?human|i.?m not a robot|are you a robot|press and hold/.test(document.body.innerText.toLowerCase());
  return hit;
}
"""

LOGIN_JS = """
() => {
  const pw = Array.from(document.querySelectorAll('input[type=password]')).filter(e => {
    const r = e.getBoundingClientRect(); return r.width > 0 && r.height > 0; });
  return pw.length > 0;
}
"""

MIN_FIELDS = 3
FORBIDDEN_CLICK = re.compile(r"submit|send application|finish|confirm|next|continue|save", re.I)


def entry_url_guess(url: str) -> str:
    """Some job pages hold the form on a sub-page."""
    u = url.split("#")[0].split("?")[0].rstrip("/")
    if "jobs.lever.co/" in u and not u.endswith("/apply"):
        return u + "/apply"
    if "jobs.ashbyhq.com/" in u and not u.endswith("/application"):
        return u + "/application"
    return ""


def questions_from_dicts(items: List[Dict[str, Any]]) -> List[Question]:
    out = []
    for d in items:
        out.append(Question(id=d["id"], kind=d["kind"], label=(d.get("label") or "").strip(), options=d.get("options") or [],
                            required=bool(d.get("required")), input_type=d.get("input_type") or "", name=d.get("name") or "",
                            placeholder=d.get("placeholder") or "", value=d.get("value") or "", frame=int(d.get("frame", 0)),
                            maxlength=int(d.get("maxlength") or 0)))
    return out


def collect(page: Any) -> List[Tuple[Any, Question]]:
    """All questions on the page, including those inside iframes (embedded Greenhouse/Lever forms)."""
    found: List[Tuple[Any, Question]] = []
    for idx, frame in enumerate(page.frames):
        try:
            items = frame.evaluate(COLLECT_JS, idx)
        except Exception:
            continue
        for q in questions_from_dicts(items):
            found.append((frame, q))
    return found


def _real_fields(qs: List[Tuple[Any, Question]]) -> int:
    return sum(1 for _, q in qs if q.kind in ("text", "textarea", "select", "radio", "combobox", "file")
               and not re.search(r"search|keyword|filter", (q.label + " " + q.name).lower()))


def _sel(q: Question) -> str:
    return f'[data-jh="{q.id}"]'


def _settle(page: Any, ms: int = 1500) -> None:
    try:
        page.wait_for_load_state("domcontentloaded", timeout=15000)
    except Exception:
        pass
    try:
        page.wait_for_load_state("networkidle", timeout=6000)
    except Exception:
        pass
    page.wait_for_timeout(ms)


_OPTION_JS = r"""
() => {
  const sel = '[role="option"], [role="listbox"] li, [role="menu"] [role="menuitem"], ul[class*="option" i] li, '
            + 'div[class*="option" i], li[class*="option" i], [class*="menu" i] [class*="item" i]';
  const out = [];
  document.querySelectorAll(sel).forEach((e, i) => {
    const r = e.getBoundingClientRect(); const s = getComputedStyle(e);
    if (r.width < 4 || r.height < 4 || s.visibility === 'hidden' || s.display === 'none') return;
    const t = (e.innerText || e.textContent || '').replace(/\s+/g, ' ').trim();
    if (!t || t.length > 120) return;
    e.setAttribute('data-jh-opt', String(i));
    out.push({text: t, i: i});
  });
  return out;
}
"""


def _visible_options(frame: Any) -> List[Tuple[str, Any]]:
    pg = frame.page
    for fr in [frame] + [f for f in pg.frames if f is not frame]:
        try:
            items = fr.evaluate(_OPTION_JS)
        except Exception:
            continue
        if items:
            return [(it["text"], fr.locator(f'[data-jh-opt="{it["i"]}"]').first) for it in items]
    return []


def _pick_combobox(frame: Any, q: Question, value: str, key: str) -> bool:
    loc = frame.locator(_sel(q)).first
    loc.click(timeout=4000)
    frame.page.wait_for_timeout(300)
    opts = _visible_options(frame)
    if not opts:
        try:
            loc.press_sequentially(value[:40], delay=40)
        except Exception:
            return False
        frame.page.wait_for_timeout(600)
        opts = _visible_options(frame)
    if not opts:
        return False
    idx = choose_option(key, value, [t for t, _ in opts])
    if idx is None and key in ("location", "city", "school") and opts:
        idx = 0
    if idx is None:
        try:
            frame.page.keyboard.press("Escape")
        except Exception:
            pass
        return False
    opts[idx][1].click(timeout=3000)
    return True


def apply_answer(frame: Any, q: Question, a: Answer, resume_pdf: Optional[str]) -> bool:
    """Perform one answer. Returns True when it took effect."""
    sel = _sel(q)
    loc = frame.locator(sel).first
    try:
        if a.action == "upload":
            if not resume_pdf:
                return False
            loc.set_input_files(resume_pdf, timeout=8000)
            return True
        if a.action == "cover_upload":
            if not a.value:
                return False
            loc.set_input_files(a.value, timeout=8000)
            return True
        if a.action == "fill":
            if q.kind == "combobox":
                return _pick_combobox(frame, q, a.value, a.key)
            loc.scroll_into_view_if_needed(timeout=3000)
            loc.fill(a.value, timeout=5000)
            if a.note == "date":
                loc.evaluate("e => e.blur()")            # closes a custom date picker popup
            return True
        if a.action == "choose":
            if q.kind == "select":
                loc.select_option(index=int(q.options[a.option]["id"]), timeout=5000)
                return True
            if q.kind == "radio":
                rid = q.options[a.option]["id"]
                target = frame.locator(f'[data-jh="{rid}"]').first
                try:
                    target.check(timeout=2500, force=True)
                except Exception:
                    try:
                        frame.locator(f'label:has([data-jh="{rid}"])').first.click(timeout=2500)
                    except Exception:
                        target.click(timeout=2500, force=True)
                return True
            if q.kind == "combobox":
                return _pick_combobox(frame, q, a.value, a.key)
        if a.action == "check":
            loc.check(timeout=3000, force=True)
            return True
    except Exception as e:
        log.info("could not apply %s to %r: %s", a.action, q.label[:60], str(e).splitlines()[0][:120] if str(e) else e)
    return False


def _entry(page: Any) -> Any:
    """Click the 'Apply' button on a job page. Returns the page to continue on (a new tab if one opened)."""
    ctx = page.context
    before = list(ctx.pages)
    try:
        text = page.evaluate(ENTRY_JS)
    except Exception:
        return None
    if not text or FORBIDDEN_CLICK.search(text):
        return None
    try:
        page.locator('[data-jh-entry="1"]').first.click(timeout=5000)
    except Exception:
        return None
    page.wait_for_timeout(2500)
    after = [p for p in ctx.pages if p not in before]
    nxt = after[-1] if after else page
    _settle(nxt)
    return nxt


# ------------------------------------------------------------------ filling one page (or one step of a wizard)
def new_report(url: str = "") -> Dict[str, Any]:
    return {"status": "failed", "url": url, "final_url": url, "filled": [], "needs": [], "left": [], "drafts": [],
            "captcha": False, "login": False, "resume_uploaded": False, "cover_letter": False, "cover_pdf": "",
            "fields_seen": 0, "notes": []}


def password_visible(page: Any) -> bool:
    try:
        return bool(page.evaluate(LOGIN_JS))
    except Exception:
        return False


def pending_fp(qs: List[Tuple[Any, Question]]) -> Tuple[Any, ...]:
    """What is still empty on the page. When this changes (a new wizard step), there is something new to fill."""
    return tuple(sorted((q.kind, q.label[:60], q.name) for _, q in qs if not q.value and q.kind != "checkbox"))


def _need(rep: Dict[str, Any], label: str, why: str) -> None:
    if label not in [n["label"] for n in rep["needs"]]:
        rep["needs"].append({"label": label, "why": why})


def fill_questions(qs: List[Tuple[Any, Question]], prof: Profile, resume_pdf: Optional[str], ctx: Any,
                   rep: Dict[str, Any]) -> None:
    """Answer every empty question we know how to. Anything already filled (by you, by Chrome, by the site) is
    left exactly as it is."""
    from . import writer
    for frame, q in qs:
        if q.value and q.kind != "file":
            continue                                  # never overwrite what is already there
        if q.kind == "file" and q.value:
            continue
        a = resolve(q, prof)
        label = q.label[:90] or q.name or q.kind

        if a.action == "cover_upload":
            c = ctx.cover_letter() if ctx is not None else None
            if not c or not c[1]:
                if q.required:
                    _need(rep, label, "cover letter could not be generated")
                continue
            a = Answer("cover_upload", c[1], "cover_letter")
            if apply_answer(frame, q, a, resume_pdf):
                rep["cover_letter"], rep["cover_pdf"] = True, c[1]
                rep["filled"].append({"label": "Cover letter (tailored PDF)", "value": ""})
                rep["drafts"].append({"label": "Cover letter (attached as PDF)", "text": c[0], "how": c[2]})
            elif q.required:
                _need(rep, label, "could not attach the cover letter")
            continue
        if a.action == "cover_text":
            c = ctx.cover_letter() if ctx is not None else None
            from .writer import fit
            text = fit(c[0], q.maxlength) if c else None
            if text and apply_answer(frame, q, Answer("fill", text, "cover_letter"), resume_pdf):
                rep["filled"].append({"label": label, "value": "cover letter text"})
                rep["drafts"].append({"label": label, "text": text, "how": c[2]})
            elif q.required:
                _need(rep, label, "cover letter text did not fit or could not be typed")
            continue

        if a.action in ("ask", "skip") and a.note == "no saved answer" and ctx is not None \
                and writer.wants_draft(q.label, q.kind, q.required):
            d = ctx.draft(q.label, q.maxlength)
            if d and apply_answer(frame, q, Answer("fill", d[0], "draft"), resume_pdf):
                rep["filled"].append({"label": label, "value": "drafted answer: please read it"})
                rep["drafts"].append({"label": label, "text": d[0], "how": d[1]})
                continue

        if a.action == "skip":
            if a.note and ("consent" in a.note or "terms" in a.note):
                rep["left"].append({"label": label, "why": a.note})
            continue
        if a.action == "ask":
            if q.required:
                _need(rep, label, a.note or "no saved answer")
            continue
        ok = apply_answer(frame, q, a, resume_pdf)
        if ok:
            if a.action == "upload":
                rep["resume_uploaded"] = True
                rep["filled"].append({"label": "Resume (tailored PDF)", "value": ""})
            else:
                rep["filled"].append({"label": label, "value": a.value[:80]})
        elif q.required:
            _need(rep, label, "could not set it automatically")


def fill_page(page: Any, prof: Profile, resume_pdf: Optional[str], ctx: Any, rep: Dict[str, Any],
              qs: Optional[List[Tuple[Any, Question]]] = None) -> List[Tuple[Any, Question]]:
    """Resume first (many sites read it and pre-fill the form, which we then leave alone), then everything else.
    Never touches a page that shows a password box: log in or sign up yourself, then it carries on."""
    if password_visible(page):
        rep["login"] = True
        return qs if qs is not None else collect(page)
    qs = qs if qs is not None else collect(page)
    files = [(f, q) for f, q in qs if q.kind == "file" and not q.value]
    if files:
        fill_questions(files, prof, resume_pdf, ctx, rep)
        if rep["resume_uploaded"] or rep["cover_letter"]:
            _settle(page, 2500)                      # let the site finish parsing the resume
        qs = collect(page)
    rest = [(f, q) for f, q in qs if q.kind != "file"]
    fill_questions(rest, prof, resume_pdf, ctx, rep)
    return qs


def run_on_page_ex(page: Any, url: str, prof: Profile, resume_pdf: Optional[str], ctx: Any = None) -> Tuple[Dict[str, Any], Any]:
    """Open `url`, find the application form, fill it. Returns (report, the page the form ended up on)."""
    rep = new_report(url)
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=45000)
    except Exception as e:
        rep["notes"].append(f"could not open the page: {str(e).splitlines()[0][:150]}")
        return rep, page
    _settle(page)

    qs = collect(page)
    for hop in range(3):
        if _real_fields(qs) >= MIN_FIELDS:
            break
        nxt = None
        if hop == 0:
            guess = entry_url_guess(page.url)
            if guess:
                try:
                    page.goto(guess, wait_until="domcontentloaded", timeout=30000)
                    _settle(page)
                    nxt = page
                except Exception:
                    nxt = None
        if nxt is None:
            nxt = _entry(page)
        if nxt is None:
            break
        page = nxt
        qs = collect(page)
    rep["final_url"] = page.url
    rep["fields_seen"] = _real_fields(qs)

    try:
        rep["captcha"] = bool(page.evaluate(CAPTCHA_JS))
    except Exception:
        pass
    rep["login"] = password_visible(page) and rep["fields_seen"] <= MIN_FIELDS + 3
    if rep["fields_seen"] < MIN_FIELDS or rep["login"]:
        if rep["login"]:
            rep["status"] = "needs_you"
            rep["notes"].append("This site wants you to log in or create an account first. Do that in the open window; "
                                "I will keep watching the page and fill each step once you are in.")
        elif rep["captcha"]:
            rep["status"] = "needs_you"
            rep["notes"].append("A CAPTCHA or bot check is blocking the page.")
        else:
            rep["notes"].append("Could not find an application form on this page.")
        _show(page)
        return rep, page

    fill_page(page, prof, resume_pdf, ctx, rep, qs)
    if rep["captcha"]:
        rep["notes"].append("A CAPTCHA is on the page: solve it yourself before submitting.")
    rep["status"] = "needs_you" if (rep["needs"] or rep["captcha"] or rep["login"]) else "ready"
    if not rep["resume_uploaded"]:
        rep["notes"].append("No resume upload field was found; attach the PDF yourself if the form asks for it.")
    if rep["drafts"]:
        rep["notes"].append(f"I drafted {len(rep['drafts'])} written answer(s) from your facts. Read them before you submit.")
    _show(page)
    return rep, page


def run_on_page(page: Any, url: str, prof: Profile, resume_pdf: Optional[str], ctx: Any = None) -> Dict[str, Any]:
    return run_on_page_ex(page, url, prof, resume_pdf, ctx)[0]


def watch_tick(page: Any, prof: Profile, resume_pdf: Optional[str], ctx: Any, state: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Called every couple of seconds for a page we filled earlier. When you log in or press Next and a new set of
    empty fields appears, fill them. Returns a report for that step, or None when nothing changed."""
    try:
        if page.is_closed() or time.time() > state["deadline"]:
            state["done"] = True
            return None
        if password_visible(page):
            return None
        qs = collect(page)
        fp = pending_fp(qs)
        if fp == state.get("fp") or not fp:
            state["fp"] = fp
            return None
        step = new_report(page.url)
        fill_page(page, prof, resume_pdf, ctx, step, qs)
        state["fp"] = pending_fp(collect(page))
        if not (step["filled"] or step["needs"] or step["drafts"]):
            return None
        step["status"] = "needs_you" if step["needs"] else "ready"
        step["fields_seen"] = _real_fields(qs)
        return step
    except Exception as e:
        state["errors"] = state.get("errors", 0) + 1
        if state["errors"] >= 5:
            state["done"] = True
        log.info("watch tick failed: %s", str(e).splitlines()[0][:120] if str(e) else e)
        return None


def _show(page: Any) -> None:
    try:
        page.bring_to_front()
        page.evaluate("window.scrollTo(0, 0)")
    except Exception:
        pass
