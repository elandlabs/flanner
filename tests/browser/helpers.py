# ruff: noqa: E501 -- the page scripts are JavaScript, kept readable as one block
"""The few things more than one journey needs.

The two page scripts are lifted from `scripts/audit_ui.py`, which this suite
replaces. They are JavaScript on purpose: contrast and overflow are questions
about what the browser computed, and nothing outside the page can answer them.
"""

from __future__ import annotations

from typing import Any

from playwright.sync_api import Page

#: WCAG AA on every element that has text of its own, measured against the
#: background it actually sits on — composited up the ancestor chain, so a
#: translucent chip over a card over the page ground is measured against the
#: colour a reader sees. Returns the failures only.
CONTRAST_JS = r"""
() => {
  const parse = (s) => {
    const m = s.match(/rgba?\(([^)]+)\)/);
    if (!m) return null;
    const p = m[1].split(',').map(Number);
    return { r: p[0], g: p[1], b: p[2], a: p.length > 3 ? p[3] : 1 };
  };
  const over = (top, under) => {
    const a = top.a + under.a * (1 - top.a);
    const c = (t, u) => a === 0 ? 0 : (t * top.a + u * under.a * (1 - top.a)) / a;
    return { r: c(top.r, under.r), g: c(top.g, under.g), b: c(top.b, under.b), a };
  };
  const lum = ({ r, g, b }) => {
    const f = (v) => { v /= 255; return v <= 0.03928 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4; };
    return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b);
  };
  const ratio = (a, b) => {
    const [hi, lo] = [lum(a), lum(b)].sort((x, y) => y - x);
    return (hi + 0.05) / (lo + 0.05);
  };
  const ground = () => {
    const body = parse(getComputedStyle(document.body).backgroundColor);
    const html = parse(getComputedStyle(document.documentElement).backgroundColor);
    const white = { r: 255, g: 255, b: 255, a: 1 };
    return over(body || { r: 0, g: 0, b: 0, a: 0 }, over(html || { r: 0, g: 0, b: 0, a: 0 }, white));
  };
  // Null when a gradient or image sits in the stack: those cannot be reduced
  // to one colour, so the element is reported as unmeasured rather than
  // measured against the wrong ground.
  const background = (el) => {
    const layers = [];
    for (let n = el; n && n !== document.documentElement; n = n.parentElement) {
      const cs = getComputedStyle(n);
      if (cs.backgroundImage && cs.backgroundImage !== 'none') return null;
      const bg = parse(cs.backgroundColor);
      if (bg && bg.a > 0) layers.push(bg);
      if (bg && bg.a >= 1) break;
    }
    let out = ground();
    for (let i = layers.length - 1; i >= 0; i--) out = over(layers[i], out);
    return out;
  };
  const visible = (el) => {
    const cs = getComputedStyle(el);
    if (cs.visibility === 'hidden' || cs.display === 'none') return false;
    for (let n = el; n; n = n.parentElement) if (Number(getComputedStyle(n).opacity) === 0) return false;
    const r = el.getBoundingClientRect();
    return r.width > 0 && r.height > 0;
  };
  const failures = [];
  for (const el of document.body.querySelectorAll('*')) {
    if (['SCRIPT', 'STYLE', 'NOSCRIPT', 'OPTION', 'TEMPLATE', 'SVG', 'PATH'].includes(el.tagName)) continue;
    const text = [...el.childNodes].filter(n => n.nodeType === 3).map(n => n.textContent).join('').trim();
    if (!text || !visible(el)) continue;
    const cs = getComputedStyle(el);
    const fg = parse(cs.color);
    if (!fg) continue;
    const bg = background(el);
    if (!bg) continue;
    const size = parseFloat(cs.fontSize);
    const bold = parseInt(cs.fontWeight, 10) >= 700;
    const need = (size >= 24 || (size >= 18.66 && bold)) ? 3 : 4.5;
    const got = ratio(over(fg, bg), bg);
    if (got < need) {
      const cls = el.className && typeof el.className === 'string'
        ? '.' + el.className.trim().split(/\s+/).join('.') : '';
      failures.push({
        selector: el.tagName.toLowerCase() + (el.id ? '#' + el.id : '') + cls,
        text: text.slice(0, 40), ratio: Math.round(got * 100) / 100, need,
      });
    }
  }
  return failures;
}
"""

#: Wide content is meant to scroll inside its own frame. The page itself
#: never scrolls sideways, at any width.
OVERFLOW_JS = r"""
() => {
  const cw = document.documentElement.clientWidth;
  const sw = document.documentElement.scrollWidth;
  const offenders = [];
  if (sw > cw) {
    for (const el of document.body.querySelectorAll('*')) {
      const r = el.getBoundingClientRect();
      if (r.right > cw + 1 && r.width > 0) {
        const cls = typeof el.className === 'string' && el.className
          ? '.' + el.className.trim().split(/\s+/).slice(0, 3).join('.') : '';
        offenders.push(el.tagName.toLowerCase() + cls + ' +' + Math.round(r.right - cw));
        if (offenders.length >= 5) break;
      }
    }
  }
  return { overflow: sw - cw, offenders };
}
"""


def set_theme(page: Page, theme: str) -> None:
    """Light or dark, on the document.

    Not by clicking the toggle: that has a 400ms settle and writes to local
    storage, so one test would decide the next one's theme.

    Transitions are cut first. `reduced_motion` turns off the app's
    animations but not its colour transitions, and a background caught
    mid-transition reads as white text on white — which is a contrast
    failure the eye never sees, reported as if it were real.
    """
    page.add_style_tag(
        content="*, *::before, *::after { transition: none !important; animation: none !important; }"
    )
    page.evaluate("t => { document.documentElement.dataset.theme = t; }", theme)


def contrast_failures(page: Page) -> list[dict[str, Any]]:
    failures: list[dict[str, Any]] = page.evaluate(CONTRAST_JS)
    return failures


def horizontal_overflow(page: Page) -> dict[str, Any]:
    measured: dict[str, Any] = page.evaluate(OVERFLOW_JS)
    return measured


def focused_description(page: Page) -> str:
    """What the keyboard is on, in a form a failure message can print."""
    described: str = page.evaluate(
        """() => {
          const el = document.activeElement;
          if (!el) return 'nothing';
          return el.tagName.toLowerCase() + ' "' + (el.textContent || el.value || '').trim().slice(0, 40) + '"';
        }"""
    )
    return described


def focus_is_visible(page: Page) -> bool:
    """Whether the focused element draws something a sighted user can see.

    An outline, a ring, or a box-shadow: the app uses `:focus-visible` with a
    shadow in places and an outline in others, so accepting either is the
    check, not the implementation.
    """
    visible: bool = page.evaluate(
        """() => {
          const el = document.activeElement;
          if (!el || el === document.body) return false;
          const cs = getComputedStyle(el);
          const outline = cs.outlineStyle !== 'none' && parseFloat(cs.outlineWidth) > 0;
          const shadow = cs.boxShadow && cs.boxShadow !== 'none';
          return Boolean(outline || shadow);
        }"""
    )
    return visible


def tab_to(page: Page, name: str, *, limit: int = 60) -> bool:
    """Tab forward until the accessible name of the focused element matches.

    Returns whether it was reached, so the caller's assertion can say which
    control it was looking for.
    """
    for _ in range(limit):
        page.keyboard.press("Tab")
        reached: bool = page.evaluate(
            """(want) => {
              const el = document.activeElement;
              if (!el) return false;
              const label = (el.getAttribute('aria-label') || el.textContent || '').trim();
              return label === want;
            }""",
            name,
        )
        if reached:
            return True
    return False
