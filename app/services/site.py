"""
The site a draft or project describes, and the HTML it renders to.

The agent never writes HTML. It edits a small, validated spec and this module
renders it, so every page is well-formed and escaped and a model can never inject
markup or script into a customer's site.

Design variety comes from choices the spec can express, not from free-form code:
  structure  how the page is composed (editorial, split, centered, bento, poster)
  theme      type pairing and surface style (ten themes, open-licensed Google Fonts)
  motion     none, subtle, lively or cinematic — CSS only, so it runs in sandboxed
             previews and always honours prefers-reduced-motion
critique() is the quality gate: slop phrases, contrast, sameness and gaps the agent
must fix before the person sees the page.
"""

import hashlib
import html
import re
from urllib.parse import urlparse

HEX = re.compile(r"^#[0-9a-fA-F]{6}$")
TONES = ("friendly", "premium", "direct", "playful", "calm")
SECTION_KINDS = ("services", "about", "faq", "testimonials", "cta", "stats", "steps", "gallery")
LAYOUTS = ("editorial", "split", "centered", "bento", "poster")
MOTIONS = ("none", "subtle", "lively", "cinematic")
MAX_ITEMS = 12
IMAGE_HOSTS = ("fal.media", "creai.dev", "images.unsplash.com", "huggingface.co")

# Ten themes. Every font is on Google Fonts under the SIL Open Font License.
THEMES = {
    "atelier":    {"display": "Fraunces", "dw": "600", "body": "Inter Tight", "radius": 4,
                   "surface": "paper", "case": "none", "track": "-0.03em"},
    "industrial": {"display": "Space Grotesk", "dw": "700", "body": "IBM Plex Sans", "radius": 0,
                   "surface": "grid", "case": "none", "track": "-0.04em", "mono": "IBM Plex Mono"},
    "botanical":  {"display": "Cormorant Garamond", "dw": "600", "body": "Karla", "radius": 20,
                   "surface": "soft", "case": "none", "track": "-0.01em"},
    "civic":      {"display": "Archivo", "dw": "800", "body": "Source Serif 4", "radius": 2,
                   "surface": "rule", "case": "none", "track": "-0.03em"},
    "nocturne":   {"display": "Syne", "dw": "700", "body": "Manrope", "radius": 14,
                   "surface": "glow", "case": "none", "track": "-0.03em"},
    "heritage":   {"display": "Playfair Display", "dw": "700", "body": "Lora", "radius": 2,
                   "surface": "rule", "case": "none", "track": "-0.02em"},
    "studio":     {"display": "Instrument Serif", "dw": "400", "body": "Instrument Sans", "radius": 10,
                   "surface": "paper", "case": "none", "track": "-0.02em"},
    "playground": {"display": "Bricolage Grotesque", "dw": "800", "body": "DM Sans", "radius": 22,
                   "surface": "soft", "case": "none", "track": "-0.04em"},
    "brutal":     {"display": "Archivo Black", "dw": "400", "body": "JetBrains Mono", "radius": 0,
                   "surface": "hard", "case": "uppercase", "track": "-0.02em"},
    "tender":     {"display": "DM Serif Display", "dw": "400", "body": "Nunito Sans", "radius": 16,
                   "surface": "soft", "case": "none", "track": "-0.01em"},
}
THEME_NAMES = tuple(THEMES)
# What each tone leans toward when the agent hasn't chosen.
TONE_THEMES = {
    "friendly": ("playground", "tender", "studio", "botanical"),
    "premium": ("atelier", "heritage", "studio", "nocturne"),
    "direct": ("industrial", "civic", "brutal", "studio"),
    "playful": ("playground", "brutal", "nocturne", "tender"),
    "calm": ("botanical", "tender", "atelier", "studio"),
}

DEFAULT = {
    "business": "",
    "headline": "",
    "subline": "",
    "tone": "friendly",
    "cta": "Get in touch",
    "palette": {"bg": "#0F2A2E", "ink": "#F4F1EA", "accent": "#F2C14E"},
    "layout": "",
    "theme": "",
    "motion": "subtle",
    "hero_image": "",
    "hero_video": "",
    "sections": [],
    "contact": {},
}
DEFAULT_PALETTE = DEFAULT["palette"]


# ---------------------------------------------------------------- validation

def _text(v, limit: int) -> str:
    return str(v or "").strip()[:limit]


def _image(v) -> str:
    """Only https images from hosts CreAI trusts (generated images, its own CDN)."""
    url = _text(v, 600)
    try:
        p = urlparse(url)
    except ValueError:
        return ""
    host = (p.hostname or "").lower()
    from . import assets
    if assets.is_our_url(url):
        return url
    from ..core.config import settings
    if host == (urlparse(settings.public_url).hostname or "").lower():
        return ""                        # on our own host, only /f/ file links
    if p.scheme != "https" or not any(host == h or host.endswith("." + h) for h in IMAGE_HOSTS):
        return ""
    return url


def _video(v) -> str:
    """Videos only from the person's own uploads."""
    from . import assets
    url = _text(v, 600)
    return url if assets.is_our_url(url) else ""


def _items(raw, fields: dict) -> list[dict]:
    out = []
    for it in (raw or [])[:MAX_ITEMS]:
        if isinstance(it, dict):
            row = {}
            for k, n in fields.items():
                row[k] = _image(it.get(k)) if n == "image" else _text(it.get(k), n)
            out.append(row)
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
    elif kind == "stats":
        out["items"] = _items(sec.get("items"), {"value": 16, "label": 60})[:6]
    elif kind == "steps":
        out["items"] = _items(sec.get("items"), {"title": 60, "detail": 200})[:8]
    elif kind == "gallery":
        out["items"] = [i for i in _items(sec.get("items"), {"image": "image", "caption": 80})
                        if i["image"]][:9]
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
    if patch.get("layout") in LAYOUTS:
        site["layout"] = patch["layout"]
    if patch.get("theme") in THEMES:
        site["theme"] = patch["theme"]
    if patch.get("motion") in MOTIONS:
        site["motion"] = patch["motion"]
    if "hero_image" in patch:
        site["hero_image"] = _image(patch["hero_image"])
    if "hero_video" in patch:
        site["hero_video"] = _video(patch["hero_video"])
    if isinstance(patch.get("palette"), dict):
        pal = dict(site["palette"])
        for k in ("bg", "ink", "accent"):
            v = patch["palette"].get(k)
            if isinstance(v, str) and HEX.match(v):
                pal[k] = v.upper()
        site["palette"] = pal
    raw = patch["sections"] if isinstance(patch.get("sections"), list) else site.get("sections")
    site["sections"] = [s for s in (clean_section(x) for x in (raw or [])
                                    if isinstance(x, dict)) if s][:8]
    if isinstance(patch.get("contact"), dict):
        site["contact"] = {k: _text(patch["contact"].get(k), 120)
                           for k in ("email", "phone", "area") if patch["contact"].get(k)}
    return site


def design_of(site: dict) -> tuple[str, str]:
    """The structure and theme in use: chosen by the agent, or picked from the
    business name so two businesses never default to the same look."""
    seed = int(hashlib.sha256((site.get("business") or "creai").encode()).hexdigest(), 16)
    layout = site.get("layout") or LAYOUTS[seed % len(LAYOUTS)]
    options = TONE_THEMES.get(site.get("tone"), THEME_NAMES)
    theme = site.get("theme") or options[(seed // 7) % len(options)]
    return layout, theme


# ---------------------------------------------------------------- colour

def _rgb(hex_: str) -> tuple[float, float, float]:
    return tuple(int(hex_[i:i + 2], 16) / 255 for i in (1, 3, 5))


def _lum(hex_: str) -> float:
    def ch(c):
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
    r, g, b = (ch(c) for c in _rgb(hex_))
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast(a: str, b: str) -> float:
    la, lb = sorted((_lum(a), _lum(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


def _mix(a: str, b: str, t: float) -> str:
    ra, rb = _rgb(a), _rgb(b)
    return "#" + "".join(f"{round((x + (y - x) * t) * 255):02X}" for x, y in zip(ra, rb))


# ---------------------------------------------------------------- quality gate

SLOP = (
    "elevate", "unlock", "unleash", "seamless", "revolutioniz", "game-chang", "cutting-edge",
    "one-stop", "welcome to", "in today's", "look no further", "take it to the next level",
    "your journey", "empower", "supercharge", "world-class", "best-in-class", "synerg",
    "delve", "tapestry", "we pride ourselves", "passionate about", "embark",
    "discover the difference", "your trusted partner", "innovative solutions",
)
PLACEHOLDER = re.compile(r"lorem ipsum|\bTBD\b|\bXXX\b", re.I)
EMOJI = re.compile("[\U0001F300-\U0001FAFF\u2600-\u27BF]")


def _all_text(s: dict) -> str:
    bits = [s["headline"], s["subline"], s["cta"]]
    for sec in s["sections"]:
        bits += [sec.get("title", ""), sec.get("body", ""), sec.get("button", "")]
        for it in sec.get("items", []):
            bits += [v for k, v in it.items() if k != "image"]
    return " ".join(bits)


def critique(site: dict) -> list[str]:
    """Problems the agent must fix. Empty means the page passes."""
    s = merge(site, {})
    issues = []
    if not s["business"] and not s["headline"]:
        return issues
    text = _all_text(s).lower()
    hits = sorted({w for w in SLOP if w in text})
    if hits:
        issues.append("Replace generic AI phrasing (" + ", ".join(hits[:6]) +
                      ") with specific words about this business.")
    words = len(s["headline"].split())
    if words > 10:
        issues.append(f"Headline is {words} words; keep it to 10 or fewer.")
    if s["headline"].lower().startswith(("welcome", "we are", "we're")):
        issues.append("Lead the headline with what the customer gets, not a greeting.")
    if EMOJI.search(s["headline"] + s["cta"]):
        issues.append("Remove emoji from the headline and main button.")
    if s["headline"].count("!") or s["subline"].count("!") > 1:
        issues.append("Drop the exclamation marks.")
    if PLACEHOLDER.search(_all_text(s)):
        issues.append("Replace filler text; use [BRACKETED] placeholders only for facts the person must supply.")
    if len(s["sections"]) < 2:
        issues.append("Add at least two sections that fit this business.")
    kinds = [x["kind"] for x in s["sections"]]
    if s["sections"] and len(set(kinds)) == 1:
        issues.append("Vary the sections; every section is the same kind.")
    if s["sections"] and "cta" not in kinds and not s["contact"]:
        issues.append("Give people a way to act: add a cta section or contact details.")
    for sec in s["sections"]:
        firsts = [(i.get("detail") or i.get("a") or "").split(" ")[0].lower()
                  for i in sec.get("items", []) if i.get("detail") or i.get("a")]
        if len(firsts) >= 3 and len(set(firsts)) == 1:
            issues.append(f"The {sec['kind']} items all start with '{firsts[0]}'; vary them.")
    pal = s["palette"]
    if contrast(pal["bg"], pal["ink"]) < 4.5:
        issues.append(f"Text on the header is hard to read (contrast {contrast(pal['bg'], pal['ink']):.1f}:1, "
                      "needs 4.5:1). Adjust bg or ink.")
    if contrast(pal["accent"], pal["bg"]) < 1.6:
        issues.append("The accent colour is too close to the background; buttons won't stand out.")
    if pal == DEFAULT_PALETTE and s["business"]:
        issues.append("Choose a palette for this business; it still has the default colours.")
    if not s["layout"] or not s["theme"]:
        issues.append("Choose a layout and theme that fit this business (they are still on automatic).")
    return issues


# ---------------------------------------------------------------- render helpers

def _fonts(theme: dict) -> str:
    fams = []
    for key, weights in (("display", theme["dw"]), ("body", "400;500;600")):
        fams.append(f"family={theme[key].replace(' ', '+')}:wght@{weights}")
    if theme.get("mono"):
        fams.append(f"family={theme['mono'].replace(' ', '+')}:wght@400;500")
    return ("<link rel=\"preconnect\" href=\"https://fonts.googleapis.com\">"
            f"<link rel=\"stylesheet\" href=\"https://fonts.googleapis.com/css2?{'&amp;'.join(fams)}"
            "&amp;display=swap\">")


def _art(site: dict, pal: dict) -> str:
    """A generative hero panel from the palette, different per business."""
    seed = int(hashlib.sha256((site["business"] or "x").encode()).hexdigest(), 16)
    kind = seed % 4
    a, b = pal["accent"], _mix(pal["bg"], pal["ink"], 0.18)
    shapes = []
    if kind == 0:      # orbits
        for i in range(6):
            r = 30 + i * 22
            shapes.append(f'<circle cx="200" cy="200" r="{r}" fill="none" stroke="{a if i % 2 else b}" '
                          f'stroke-width="{2 + (seed >> i) % 4}"/>')
    elif kind == 1:    # stripes
        for i in range(12):
            shapes.append(f'<rect x="{i * 34}" y="0" width="{12 + (seed >> i) % 14}" height="400" '
                          f'fill="{a if i % 3 == 0 else b}"/>')
    elif kind == 2:    # tiles
        for i in range(25):
            x, y = (i % 5) * 80, (i // 5) * 80
            if (seed >> i) & 1:
                shapes.append(f'<rect x="{x + 8}" y="{y + 8}" width="64" height="64" rx="{(seed >> i) % 32}" '
                              f'fill="{a if (seed >> (i + 3)) & 1 else b}"/>')
    else:              # dots
        for i in range(64):
            x, y = (i % 8) * 50 + 25, (i // 8) * 50 + 25
            r = 4 + ((seed >> (i % 60)) & 7) * 2
            shapes.append(f'<circle cx="{x}" cy="{y}" r="{r}" fill="{a if i % 5 == 0 else b}"/>')
    initial = html.escape((site["business"] or "·")[:1].upper())
    return (f'<svg class="art" viewBox="0 0 400 400" preserveAspectRatio="xMidYMid slice" role="img" aria-label="">'
            f'<rect width="400" height="400" fill="{_mix(pal["bg"], "#000000", 0.15)}"/>{"".join(shapes)}'
            f'<text x="200" y="232" text-anchor="middle" font-size="120" fill="{pal["ink"]}" '
            f'opacity=".92" style="font-family:var(--display)">{initial}</text></svg>')


def _words(text: str) -> str:
    """Headline split into words, so cinematic motion can reveal them in turn."""
    return " ".join(f'<span class="w" style="--i:{i}">{html.escape(w)}</span>'
                    for i, w in enumerate(text.split()))


def _section(sec: dict, s: dict, n: int, layout: str) -> str:
    e = html.escape
    label = f'<span class="idx">{n:02d}</span>' if layout == "editorial" else ""
    title = f'<h2>{label}{e(sec["title"])}</h2>' if sec.get("title") else ""
    k = sec["kind"]
    if k == "services":
        cards = "".join(
            f'<article class="card" style="--i:{i}"><h3>{e(x["name"])}</h3><p>{e(x["detail"])}</p>'
            + (f'<span class="price">{e(x["price"])}</span>' if x["price"] else "") + "</article>"
            for i, x in enumerate(sec["items"]))
        body = f'<div class="grid">{cards}</div>'
    elif k == "faq":
        body = "".join(f'<details><summary>{e(x["q"])}</summary><p>{e(x["a"])}</p></details>'
                       for x in sec["items"])
    elif k == "testimonials":
        body = '<div class="grid">' + "".join(
            f'<figure class="card quote" style="--i:{i}"><blockquote>{e(x["quote"])}</blockquote>'
            f'<figcaption>{e(x["name"])}</figcaption></figure>' for i, x in enumerate(sec["items"])) + "</div>"
    elif k == "stats":
        body = '<dl class="stats">' + "".join(
            f'<div style="--i:{i}"><dt>{e(x["label"])}</dt><dd>{e(x["value"])}</dd></div>'
            for i, x in enumerate(sec["items"])) + "</dl>"
    elif k == "steps":
        body = '<ol class="steps">' + "".join(
            f'<li style="--i:{i}"><h3>{e(x["title"])}</h3><p>{e(x["detail"])}</p></li>'
            for i, x in enumerate(sec["items"])) + "</ol>"
    elif k == "gallery":
        body = '<div class="gallery">' + "".join(
            f'<figure style="--i:{i}"><img src="{e(x["image"], quote=True)}" alt="{e(x["caption"], quote=True)}" '
            f'loading="lazy">' + (f'<figcaption>{e(x["caption"])}</figcaption>' if x["caption"] else "")
            + "</figure>" for i, x in enumerate(sec["items"])) + "</div>"
    elif k == "cta":
        btn = f'<a class="btn" href="#contact">{e(sec.get("button") or s["cta"])}</a>'
        return (f'<section class="band reveal">{title}<p class="lede">{e(sec["body"])}</p>{btn}</section>')
    else:
        body = f'<p class="lede">{e(sec["body"])}</p>'
    return f'<section class="sec sec-{k} reveal">{title}{body}</section>'


def _marquee(s: dict) -> str:
    names = [x["name"] for sec in s["sections"] if sec["kind"] == "services" for x in sec["items"]]
    if len(names) < 2:
        return ""
    run = " ✦ ".join(html.escape(n) for n in names) + " ✦ "
    return f'<div class="marquee" aria-hidden="true"><div>{run * 3}</div></div>'


# ---------------------------------------------------------------- render

def render(site: dict | None) -> str:
    s = merge(site, {})
    e = html.escape
    layout, theme_name = design_of(s)
    poster_bg = ""
    t = THEMES[theme_name]
    pal = s["palette"]
    on_accent = "#141414" if _lum(pal["accent"]) > 0.4 else "#FFFFFF"
    name = s["business"] or "Your business"
    motion = s["motion"]

    parts = [_section(sec, s, n, layout) for n, sec in enumerate(s["sections"], 1)]
    c = s["contact"]
    contact = " · ".join(e(c[k]) for k in ("area", "phone", "email") if c.get(k))
    empty = "" if (s["headline"] or parts) else \
        '<p class="hint">Describe your business and the page fills in here.</p>'

    headline = s["headline"] or name
    if s["hero_video"]:
        poster = f' poster="{e(s["hero_image"], quote=True)}"' if s["hero_image"] else ""
        visual = (f'<video class="art" src="{e(s["hero_video"], quote=True)}"{poster} autoplay muted loop '
                  f'playsinline preload="metadata" aria-hidden="true"></video>')
    elif s["hero_image"]:
        visual = f'<img class="art" src="{e(s["hero_image"], quote=True)}" alt="">'
    else:
        visual = _art(s, pal)
    cta = f'<a class="btn" href="#contact">{e(s["cta"])}</a>'
    sub = f'<p class="sub">{e(s["subline"])}</p>' if s["subline"] else ""
    h1 = f'<h1 aria-label="{e(headline, quote=True)}"><span aria-hidden="true">{_words(headline)}</span></h1>'

    if layout == "split":
        hero = f'<div class="hero-copy">{h1}{sub}{cta}</div><div class="hero-visual">{visual}</div>'
    elif layout == "bento":
        stats = next((x for x in s["sections"] if x["kind"] == "stats"), None)
        tile3 = (f'<div class="tile t3"><strong>{e(stats["items"][0]["value"])}</strong>'
                 f'<span>{e(stats["items"][0]["label"])}</span></div>'
                 if stats and stats["items"] else f'<div class="tile t3">{e(c.get("area", "") or s["tone"])}</div>')
        hero = (f'<div class="tile t1">{h1}{sub}</div><div class="tile t2">{visual}</div>'
                f'{tile3}<div class="tile t4">{cta}</div>')
    elif layout == "poster":
        hero = f'{h1}<div class="poster-foot">{sub}{cta}</div>'
        if s["hero_video"]:
            hero = (f'<video class="poster-video" src="{e(s["hero_video"], quote=True)}" autoplay muted loop '
                    f'playsinline aria-hidden="true"></video>') + hero
        if s["hero_image"] and not s["hero_video"]:
            from urllib.parse import quote
            safe = quote(s["hero_image"], safe=":/?&=%.-_~")
            poster_bg = (f'.hero.poster{{background:linear-gradient(100deg,var(--accent) 38%,'
                         f'color-mix(in srgb,var(--accent) 35%,transparent) 75%),'
                         f'url("{safe}") right center/cover no-repeat}}')
    elif layout == "editorial":
        hero = (f'<div class="kicker">{e(c.get("area", "")) or e(s["tone"])}</div>{h1}'
                f'<div class="ed-row">{sub}{cta}</div><div class="hero-visual wide">{visual}</div>')
    else:
        hero = f'{h1}{sub}{cta}<div class="hero-visual small">{visual}</div>'

    paper = _mix(pal["ink"], "#FFFFFF", 0.55) if _lum(pal["ink"]) > 0.5 else "#FAFAF7"
    surface = {
        "paper": f"background:{paper}",
        "grid": "background:#F7F7F5;background-image:linear-gradient(#0000000A 1px,transparent 1px),"
                "linear-gradient(90deg,#0000000A 1px,transparent 1px);background-size:32px 32px",
        "soft": f"background:{_mix(pal['accent'], '#FFFFFF', 0.9)}",
        "rule": "background:#FFFFFF",
        "glow": f"background:{_mix(pal['bg'], '#000000', 0.35)};color:{pal['ink']}",
        "hard": "background:#FFFFFF",
    }[t["surface"]]
    dark_body = t["surface"] == "glow"
    line = "#FFFFFF22" if dark_body else "#1414141F"
    muted = _mix(pal["ink"], "#000000", 0.25) if dark_body else "#4A4A48"
    card_border = "3px solid #141414" if t["surface"] == "hard" else f"1px solid {line}"
    shadow = "6px 6px 0 #141414" if t["surface"] == "hard" else "none"

    css = f"""
:root{{--bg:{pal['bg']};--ink:{pal['ink']};--accent:{pal['accent']};--on:{on_accent};
--display:'{t['display']}',Georgia,serif;--body:'{t['body']}',system-ui,sans-serif;--r:{t['radius']}px;
--line:{line};--muted:{muted}}}
*{{box-sizing:border-box}}html{{scroll-behavior:smooth}}
body{{margin:0;font:17px/1.6 var(--body);color:{'#F4F4F4' if dark_body else '#161616'};{surface};overflow-x:hidden}}
img{{max-width:100%;display:block}}
.nav{{display:flex;justify-content:space-between;align-items:center;padding:22px 6vw;font-weight:600;
background:var(--bg);color:var(--ink)}}
.nav a{{color:inherit;text-decoration:none;font-size:14px;border:1px solid currentColor;padding:6px 14px;border-radius:999px}}
.hero{{background:var(--bg);color:var(--ink);padding:48px 6vw 72px;position:relative;overflow:hidden}}
h1{{font:{t['dw']} clamp(40px,7vw,96px)/.98 var(--display);letter-spacing:{t['track']};
text-transform:{t['case']};margin:0 0 20px;max-width:13ch;text-wrap:balance}}
h1 .w{{display:inline-block}}
.sub{{font-size:clamp(17px,1.6vw,21px);max-width:36ch;opacity:.86;margin:0 0 28px;text-wrap:pretty}}
.btn{{display:inline-flex;align-items:center;gap:10px;background:var(--accent);color:var(--on);padding:15px 26px;
border-radius:calc(var(--r) + 4px);font-weight:600;text-decoration:none;
box-shadow:{shadow.replace('#141414', 'var(--ink)') if t['surface'] == 'hard' else 'none'};
transition:transform .25s cubic-bezier(.2,.7,.2,1),box-shadow .25s}}
.btn::after{{content:"→";transition:transform .25s}}
.btn:hover{{transform:translateY(-2px)}}.btn:hover::after{{transform:translateX(4px)}}
.btn:focus-visible{{outline:3px solid var(--ink);outline-offset:3px}}
.art{{width:100%;height:100%;object-fit:cover;border-radius:var(--r)}}
.hero-visual{{aspect-ratio:1;max-width:520px}}
.hero-visual.small{{position:absolute;right:-60px;bottom:-80px;width:340px;opacity:.9;z-index:0}}
.hero-visual.wide{{aspect-ratio:21/8;max-width:none;margin-top:40px;overflow:hidden;border-radius:var(--r)}}
.hero.centered{{text-align:center}}.hero.centered h1,.hero.centered .sub{{margin-left:auto;margin-right:auto}}
.hero.centered>*:not(.hero-visual){{position:relative;z-index:1}}
.hero.split{{display:grid;grid-template-columns:1.1fr .9fr;gap:6vw;align-items:center}}
.hero.editorial .kicker{{font-size:13px;text-transform:uppercase;letter-spacing:.18em;opacity:.7;
border-bottom:1px solid currentColor;padding-bottom:14px;margin-bottom:36px}}
.hero.editorial h1{{max-width:16ch;font-size:clamp(48px,9vw,132px)}}
.ed-row{{display:flex;gap:40px;align-items:flex-end;justify-content:space-between;flex-wrap:wrap}}
.hero.bento{{display:grid;grid-template-columns:repeat(4,1fr);grid-auto-rows:minmax(120px,auto);gap:12px}}
.tile{{background:{_mix(pal['bg'], pal['ink'], 0.08)};border-radius:var(--r);padding:24px;overflow:hidden}}
.t1{{grid-column:span 3;grid-row:span 2}}.t2{{grid-row:span 2;padding:0;min-height:280px}}
.t2 .art{{position:relative;height:100%;min-height:280px}}
.t3{{grid-column:span 2;display:flex;flex-direction:column;justify-content:flex-end}}
.t3 strong{{font:{t['dw']} 48px/1 var(--display)}}.t4{{grid-column:span 2;display:flex;align-items:center;justify-content:flex-end}}
.hero.poster{{background:var(--accent);color:var(--on);min-height:82vh;display:flex;flex-direction:column;justify-content:space-between}}
.hero.poster h1{{font-size:clamp(52px,9.5vw,150px);max-width:14ch;text-transform:uppercase;line-height:.9}}
.hero.poster .btn{{background:var(--on);color:var(--accent)}}
.hero.poster{{position:relative;isolation:isolate}}
.poster-video{{position:absolute;inset:0;width:100%;height:100%;object-fit:cover;z-index:-1;opacity:.35;mix-blend-mode:luminosity}}
.poster-foot{{display:flex;justify-content:space-between;align-items:flex-end;gap:24px;flex-wrap:wrap}}
.marquee{{overflow:hidden;background:var(--ink);color:var(--bg);white-space:nowrap;padding:14px 0;
font:{t['dw']} 22px var(--display);text-transform:{t['case']}}}
.marquee div{{display:inline-block}}
main{{padding:0 6vw}}
.sec{{padding:88px 0;max-width:1180px;border-top:1px solid var(--line)}}
h2{{font:{t['dw']} clamp(30px,4vw,54px)/1.02 var(--display);letter-spacing:{t['track']};
text-transform:{t['case']};margin:0 0 36px;max-width:18ch;text-wrap:balance}}
.idx{{display:block;font:500 13px var(--body);letter-spacing:.2em;color:var(--muted);margin-bottom:14px}}
h3{{font:600 20px/1.25 var(--body);margin:0 0 8px}}
.lede{{font-size:clamp(19px,1.8vw,24px);max-width:40ch;line-height:1.5}}
.grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(250px,1fr));gap:16px}}
.card{{border:{card_border};border-radius:var(--r);padding:26px;margin:0;
background:{'#FFFFFF08' if dark_body else '#FFFFFF'};box-shadow:{shadow};
transition:transform .35s cubic-bezier(.2,.7,.2,1),border-color .35s}}
.card:hover{{transform:translateY(-4px);border-color:var(--accent)}}
.card p{{color:var(--muted);margin:0}}.price{{display:inline-block;margin-top:16px;font-weight:700}}
.quote blockquote{{margin:0 0 16px;font:400 21px/1.45 var(--display)}}
.quote figcaption{{color:var(--muted);font-size:14px}}
details{{border-top:1px solid var(--line);padding:20px 0;max-width:760px}}
summary{{font-weight:600;font-size:19px;cursor:pointer;list-style:none;display:flex;justify-content:space-between;gap:20px}}
summary::after{{content:"+";font-size:26px;line-height:1;transition:transform .3s}}
details[open] summary::after{{transform:rotate(45deg)}}details p{{color:var(--muted);margin:12px 0 0}}
.stats{{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:32px;margin:0}}
.stats dd{{font:{t['dw']} clamp(44px,5vw,72px)/1 var(--display);margin:0;letter-spacing:{t['track']}}}
.stats dt{{color:var(--muted);margin-top:8px}}.stats div{{display:flex;flex-direction:column-reverse;justify-content:flex-end}}
.steps{{list-style:none;padding:0;margin:0;display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));
gap:28px;counter-reset:s}}
.steps li{{counter-increment:s;border-top:2px solid var(--accent);padding-top:18px}}
.steps li::before{{content:counter(s,decimal-leading-zero);font:500 13px var(--body);letter-spacing:.2em;color:var(--muted)}}
.steps p{{color:var(--muted);margin:0}}
.gallery{{columns:3 260px;gap:14px}}.gallery figure{{margin:0 0 14px;break-inside:avoid}}
.gallery img{{border-radius:var(--r)}}.gallery figcaption{{font-size:14px;color:var(--muted);padding-top:6px}}
.band{{background:var(--bg);color:var(--ink);margin:40px -6vw 0;padding:96px 6vw;max-width:none;border:0}}
.band .lede{{opacity:.85}}
footer{{padding:36px 6vw;display:flex;justify-content:space-between;gap:16px;flex-wrap:wrap;
color:var(--muted);border-top:1px solid var(--line);font-size:14px}}
.hint{{padding:80px 6vw;color:#777}}
@media (max-width:760px){{
.hero.split,.hero.bento{{grid-template-columns:1fr}}.t1,.t2,.t3,.t4{{grid-column:auto;grid-row:auto}}
.hero-visual.small{{width:200px;right:-50px;bottom:-60px;opacity:.5}}.sec{{padding:64px 0}}}}
"""
    if motion != "none":
        css += """
@media (prefers-reduced-motion:no-preference){
@keyframes rise{from{opacity:0;transform:translateY(24px)}to{opacity:1;transform:none}}
@keyframes fade{from{opacity:0}to{opacity:1}}
.hero h1,.hero .sub,.hero .btn,.hero .kicker{animation:rise .9s cubic-bezier(.2,.7,.2,1) both}
.hero .sub{animation-delay:.12s}.hero .btn{animation-delay:.22s}
.hero-visual,.tile{animation:fade 1.2s ease both .15s}
@supports (animation-timeline:view()){
.reveal{animation:rise linear both;animation-timeline:view();animation-range:entry 0% entry 40%}}
}
"""
    if motion in ("lively", "cinematic"):
        css += """
@media (prefers-reduced-motion:no-preference){
@keyframes slide{to{transform:translateX(-33.333%)}}
.marquee div{animation:slide 28s linear infinite}
@supports (animation-timeline:view()){
.card,.steps li,.stats div,.gallery figure{animation:rise linear both;animation-timeline:view();
animation-range:entry calc(var(--i,0) * 4%) entry calc(30% + var(--i,0) * 4%)}}
.art circle,.art rect{transform-box:fill-box;transform-origin:center}
@keyframes breathe{50%{transform:scale(.92)}}
.art circle{animation:breathe 7s ease-in-out infinite}
}
"""
    if motion == "cinematic":
        css += """
@media (prefers-reduced-motion:no-preference){
@keyframes word{from{opacity:0;transform:translateY(.45em) rotate(2deg);filter:blur(8px)}to{opacity:1;transform:none;filter:none}}
.hero h1{animation:none}
.hero h1 .w{animation:word .9s cubic-bezier(.2,.7,.2,1) both;animation-delay:calc(var(--i) * 70ms)}
@supports (animation-timeline:scroll()){
@keyframes drift{to{transform:translateY(18%) scale(1.08)}}
.hero-visual{animation:drift linear both;animation-timeline:scroll();animation-range:0 90vh}
@keyframes shrink{to{letter-spacing:-.06em;opacity:.35}}
.hero.poster h1{animation:shrink linear both;animation-timeline:scroll();animation-range:0 70vh}}
body::after{content:"";position:fixed;inset:0;pointer-events:none;opacity:.06;mix-blend-mode:multiply;
background-image:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='160' height='160'%3E%3Cfilter id='n'%3E%3CfeTurbulence type='fractalNoise' baseFrequency='.9' numOctaves='2'/%3E%3C/filter%3E%3Crect width='160' height='160' filter='url(%23n)'/%3E%3C/svg%3E")}
}
"""

    marquee = _marquee(s) if (layout == "poster" or motion in ("lively", "cinematic")) else ""
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{e(name)}</title>
<meta name="description" content="{e(s['subline'][:155], quote=True)}">
<meta name="generator" content="CreAI · {layout} · {theme_name} · {motion}">
{_fonts(t)}
<style>{css}{poster_bg}</style></head><body>
<nav class="nav"><span>{e(name)}</span><a href="#contact">{e(s['cta'])}</a></nav>
<header class="hero {layout}">{hero}</header>
{marquee}
<main>{empty}{''.join(parts)}</main>
<footer id="contact"><span>{e(name)}</span><span>{contact}</span></footer>
</body></html>"""
