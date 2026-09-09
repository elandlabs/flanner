"""Walk the local web UI and fail on text that cannot be read or a page that
scrolls sideways.

Two checks, both the kind a screenshot hides:

- **Contrast.** Every element with its own text is measured against the
  background it actually sits on — composited up the ancestor chain, so a
  translucent chip over a card over the page ground is measured against
  the colour a reader sees. Normal text needs 4.5:1, large text 3:1 (WCAG
  AA). Run in both themes: light mode fails in different places from dark.
- **Overflow.** `documentElement.scrollWidth` against `clientWidth` at a
  laptop, a small laptop and a phone. Wide content is meant to scroll
  inside its own frame; the page itself never scrolls sideways.

Pages are found by crawling same-origin links from a few starting routes,
so plan, memory and skill detail pages are covered without hard-coding ids.

    python -m flanner.cli web --port 8099          # in a repo with plans
    python scripts/audit_ui.py --base http://127.0.0.1:8099

Exit status is 1 when anything fails, so it can gate a check.
"""

from __future__ import annotations

import argparse
import json
import sys
from urllib.parse import urljoin, urlparse

from playwright.sync_api import sync_playwright

START = ["/", "/projects", "/plans", "/freshness", "/memory", "/memory/pending", "/skills",
         "/skills/proposals", "/mesh", "/review", "/integrations", "/settings"]
# Not pages: streams, downloads, form targets, and the static tree.
SKIP = ("/events", "/stream", "/download", "/static/", "/api/", "/attachments/", "/delete",
        "/revision", "/nav/", "/freshness-mix")
WIDTHS = (1280, 1024, 375)
MAX_PAGES = 40

# Runs in the page. Composites each text element's background up the tree,
# then measures WCAG contrast. Returns the failures only.
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
  const ratio = (a, b) => { const [hi, lo] = [lum(a), lum(b)].sort((x, y) => y - x); return (hi + 0.05) / (lo + 0.05); };
  const ground = () => {
    const body = parse(getComputedStyle(document.body).backgroundColor);
    const html = parse(getComputedStyle(document.documentElement).backgroundColor);
    const white = { r: 255, g: 255, b: 255, a: 1 };
    return over(body || { r: 0, g: 0, b: 0, a: 0 }, over(html || { r: 0, g: 0, b: 0, a: 0 }, white));
  };
  // The composited background, or null when a gradient or image sits in
  // the stack: those cannot be reduced to one colour, so the element is
  // reported as unmeasured rather than measured against the wrong ground.
  const background = (el) => {
    const layers = [];
    for (let n = el; n && n !== document.documentElement; n = n.parentElement) {
      const cs = getComputedStyle(n);
      if (cs.backgroundImage && cs.backgroundImage !== 'none') return null;
      const bg = parse(cs.backgroundColor);
      if (bg && bg.a > 0) layers.push(bg);
      if (bg && bg.a >= 1) break;
    }
    // Bottom layer first.
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
  const unmeasured = [];
  for (const el of document.body.querySelectorAll('*')) {
    if (['SCRIPT', 'STYLE', 'NOSCRIPT', 'OPTION', 'TEMPLATE', 'SVG', 'PATH'].includes(el.tagName)) continue;
    const text = [...el.childNodes].filter(n => n.nodeType === 3).map(n => n.textContent).join('').trim();
    if (!text || !visible(el)) continue;
    const cs = getComputedStyle(el);
    const fg = parse(cs.color);
    if (!fg) continue;
    const bg = background(el);
    if (!bg) { unmeasured.push(el.tagName.toLowerCase() + (el.className && typeof el.className === 'string' ? '.' + el.className.trim().split(/\s+/)[0] : '')); continue; }
    const fgOn = over(fg, bg);
    const size = parseFloat(cs.fontSize);
    const bold = parseInt(cs.fontWeight, 10) >= 700;
    const large = size >= 24 || (size >= 18.66 && bold);
    const need = large ? 3 : 4.5;
    const got = ratio(fgOn, bg);
    if (got < need) {
      const id = el.id ? '#' + el.id : '';
      const cls = el.className && typeof el.className === 'string' ? '.' + el.className.trim().split(/\s+/).join('.') : '';
      failures.push({ selector: el.tagName.toLowerCase() + id + cls, text: text.slice(0, 40), ratio: Math.round(got * 100) / 100, need, size });
    }
  }
  return { failures, unmeasured: [...new Set(unmeasured)] };
}
"""

OVERFLOW_JS = r"""
() => {
  const cw = document.documentElement.clientWidth;
  const sw = document.documentElement.scrollWidth;
  const offenders = [];
  if (sw > cw) {
    for (const el of document.body.querySelectorAll('*')) {
      const r = el.getBoundingClientRect();
      if (r.right > cw + 1 && r.width > 0) {
        const cls = typeof el.className === 'string' && el.className ? '.' + el.className.trim().split(/\s+/).slice(0, 3).join('.') : '';
        offenders.push(el.tagName.toLowerCase() + cls + ' +' + Math.round(r.right - cw));
        if (offenders.length >= 5) break;
      }
    }
  }
  return { overflow: sw - cw, offenders };
}
"""


def crawl(page, base: str) -> list[str]:
    seen: list[str] = []
    queue = list(START)
    host = urlparse(base).netloc
    while queue and len(seen) < MAX_PAGES:
        path = queue.pop(0)
        if path in seen or any(s in path for s in SKIP):
            continue
        response = page.goto(urljoin(base, path), wait_until="domcontentloaded")
        if response is None or response.status >= 400:
            continue
        seen.append(path)
        for href in page.eval_on_selector_all("a[href]", "els => els.map(e => e.getAttribute('href'))"):
            if not href or href.startswith(("#", "mailto:", "javascript:")):
                continue
            parsed = urlparse(urljoin(base, href))
            if parsed.netloc != host:
                continue
            candidate = parsed.path
            if candidate not in seen and candidate not in queue:
                queue.append(candidate)
    return seen


def set_theme(page, theme: str) -> None:
    page.evaluate("t => { document.documentElement.dataset.theme = t; }", theme)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--base", default="http://127.0.0.1:8099")
    parser.add_argument("--json", action="store_true", help="machine-readable report")
    args = parser.parse_args()

    report: dict = {"pages": [], "contrast": [], "overflow": [], "unmeasured": set()}
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1280, "height": 900})
        pages = crawl(page, args.base)
        report["pages"] = pages

        for path in pages:
            for theme in ("light", "dark"):
                page.goto(urljoin(args.base, path), wait_until="domcontentloaded")
                set_theme(page, theme)
                page.wait_for_timeout(150)
                measured = page.evaluate(CONTRAST_JS)
                for failure in measured["failures"]:
                    report["contrast"].append({"page": path, "theme": theme, **failure})
                report["unmeasured"].update(measured["unmeasured"])

        for width in WIDTHS:
            page.set_viewport_size({"width": width, "height": 900})
            for path in pages:
                page.goto(urljoin(args.base, path), wait_until="domcontentloaded")
                page.wait_for_timeout(100)
                result = page.evaluate(OVERFLOW_JS)
                if result["overflow"] > 0:
                    report["overflow"].append({"page": path, "width": width, **result})
        browser.close()

    report["unmeasured"] = sorted(report["unmeasured"])
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print(f"{len(pages)} pages")
        if report["unmeasured"]:
            print("  unmeasured (gradient or image behind the text):", ", ".join(report["unmeasured"]))
        for f in report["contrast"]:
            print(f"  contrast  {f['page']:<32} {f['theme']:<5} {f['ratio']:>5} < {f['need']}  {f['selector']}  \"{f['text']}\"")
        for f in report["overflow"]:
            print(f"  overflow  {f['page']:<32} @{f['width']}  +{f['overflow']}px  {', '.join(f['offenders'])}")
        print("contrast failures:", len(report["contrast"]), " overflow failures:", len(report["overflow"]))
    return 1 if report["contrast"] or report["overflow"] else 0


if __name__ == "__main__":
    sys.exit(main())
