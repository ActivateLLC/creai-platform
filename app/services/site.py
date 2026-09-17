"""
The site a draft or project describes, and the HTML it renders to.

The agent never writes HTML. It edits a small, validated spec — headline, tone,
palette, sections — and this module renders it. That keeps every generated page
well-formed and escaped, and means a model can never inject markup or script
into a customer's site.
"""

import html
import re

HEX = re.compile(r"^#[0-9a-fA-F]{6}$")
TONES = ("friendly", "premium", "direct", "playful", "calm")
SECTION_KINDS = ("services", "about", "faq", "testimonials", "cta")
MAX_ITEMS = 12

DEFAULT = {
    "business": "",
    "headline": "",
    "subline": "",
    "tone": "friendly",
    "cta": "Get in touch",
    "palette": {"bg": "#0F2A2E", "ink": "#F4F1EA", "accent": "#F2C14E"},
    "sections": [],
    "contact": {},
}


def _text(v, limit: int) -> str:
    return str(v or "").strip()[:limit]


def _items(raw, fields: dict) -> list[dict]:
    out = []
    for it in (raw or [])[:MAX_ITEMS]:
        if isinstance(it, dict):
            out.append({k: _text(it.get(k), n) for k, n in fields.items()})
    return out


def clean_section(sec: dict) -> dict | None:
    kind = sec.get("kind")
    if kind not in SECTION_KINDS:
        return None
    out = {"kind": kind, "title": _text(sec.get("title"), 80)}
    if kind == "services":
        out["items"] = _items(sec.get("items"), {"name": 60, "detail": 160, "price": 24})
    elif kind == "faq":
        out["items"] = _items(sec.get("items"), {"q": 140, "a": 400})
    elif kind == "testimonials":
        out["items"] = _items(sec.get("items"), {"quote": 280, "name": 60})
    else:
        out["body"] = _text(sec.get("body"), 900)
        if kind == "cta":
            out["button"] = _text(sec.get("button"), 32)
    return out


def merge(current: dict | None, patch: dict) -> dict:
    """Apply an agent's patch to a spec, keeping only valid fields."""
    site = {**DEFAULT, **(current or {})}
    for key, limit in (("business", 80), ("headline", 120), ("subline", 300), ("cta", 32)):
        if key in patch:
            site[key] = _text(patch[key], limit)
    if patch.get("tone") in TONES:
        site["tone"] = patch["tone"]
    if isinstance(patch.get("palette"), dict):
        pal = dict(site["palette"])
        for k in ("bg", "ink", "accent"):
            v = patch["palette"].get(k)
            if isinstance(v, str) and HEX.match(v):
                pal[k] = v
        site["palette"] = pal
    if isinstance(patch.get("sections"), list):
        site["sections"] = [s for s in (clean_section(x) for x in patch["sections"]
                                        if isinstance(x, dict)) if s][:8]
    if isinstance(patch.get("contact"), dict):
        site["contact"] = {k: _text(patch["contact"].get(k), 120)
                           for k in ("email", "phone", "area") if patch["contact"].get(k)}
    return site


def _lum(hex_: str) -> float:
    r, g, b = (int(hex_[i:i + 2], 16) / 255 for i in (1, 3, 5))
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def render(site: dict | None) -> str:
    s = merge(site, {})
    e = html.escape
    pal = s["palette"]
    on_accent = "#141414" if _lum(pal["accent"]) > 0.55 else "#FFFFFF"
    name = s["business"] or "Your business"

    parts = []
    for sec in s["sections"]:
        title = f'<h2>{e(sec["title"])}</h2>' if sec.get("title") else ""
        if sec["kind"] == "services":
            cards = "".join(
                f'<div class="card"><strong>{e(i["name"])}</strong>'
                f'<span>{e(i["detail"])}</span>'
                + (f'<em>{e(i["price"])}</em>' if i["price"] else "") + "</div>"
                for i in sec["items"])
            parts.append(f'<section>{title}<div class="grid">{cards}</div></section>')
        elif sec["kind"] == "faq":
            qa = "".join(f'<details><summary>{e(i["q"])}</summary><p>{e(i["a"])}</p></details>'
                         for i in sec["items"])
            parts.append(f"<section>{title}{qa}</section>")
        elif sec["kind"] == "testimonials":
            qs = "".join(f'<blockquote>“{e(i["quote"])}”<cite>{e(i["name"])}</cite></blockquote>'
                         for i in sec["items"])
            parts.append(f'<section>{title}<div class="grid">{qs}</div></section>')
        elif sec["kind"] == "cta":
            btn = f'<a class="btn" href="#contact">{e(sec.get("button") or s["cta"])}</a>'
            parts.append(f'<section class="band">{title}<p>{e(sec["body"])}</p>{btn}</section>')
        else:
            parts.append(f'<section>{title}<p>{e(sec["body"])}</p></section>')

    c = s["contact"]
    contact = " · ".join(e(c[k]) for k in ("area", "phone", "email") if c.get(k))
    empty = "" if (s["headline"] or parts) else \
        '<p class="hint">Describe your business and the page fills in here.</p>'

    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{e(name)}</title>
<style>
:root{{--bg:{pal['bg']};--ink:{pal['ink']};--accent:{pal['accent']};--on:{on_accent}}}
*{{box-sizing:border-box}}body{{margin:0;font:16px/1.6 system-ui,-apple-system,sans-serif;color:#1b1b1b;background:#fff}}
header{{background:var(--bg);color:var(--ink);padding:28px 6vw 56px}}
nav{{display:flex;justify-content:space-between;align-items:center;font-weight:700}}
h1{{font-size:clamp(32px,5vw,52px);line-height:1.05;letter-spacing:-.03em;margin:48px 0 14px;max-width:14em}}
header p{{max-width:34em;opacity:.85;margin:0 0 24px}}
.btn{{display:inline-block;background:var(--accent);color:var(--on);padding:12px 22px;border-radius:10px;font-weight:600;text-decoration:none}}
section{{padding:44px 6vw;max-width:1100px}}h2{{letter-spacing:-.02em;margin:0 0 18px}}
.grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(220px,1fr));gap:12px}}
.card{{border:1px solid #e3e0d8;border-radius:12px;padding:16px;display:flex;flex-direction:column;gap:4px}}
.card span{{color:#555;font-size:14px}}.card em{{font-style:normal;font-weight:600}}
details{{border-bottom:1px solid #e3e0d8;padding:12px 0}}summary{{font-weight:600;cursor:pointer}}
blockquote{{margin:0;border:1px solid #e3e0d8;border-radius:12px;padding:16px}}cite{{display:block;margin-top:8px;color:#555;font-style:normal}}
.band{{background:#f6f4ef;max-width:none}}footer{{padding:28px 6vw;color:#555;border-top:1px solid #eee}}
.hint{{padding:60px 6vw;color:#777}}
</style></head><body>
<header><nav><span>{e(name)}</span></nav>
<h1>{e(s['headline']) or e(name)}</h1><p>{e(s['subline'])}</p>
<a class="btn" href="#contact">{e(s['cta'])}</a></header>
{empty}{''.join(parts)}
<footer id="contact">{e(name)}{' · ' + contact if contact else ''}</footer>
</body></html>"""
