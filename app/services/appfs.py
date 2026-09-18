"""
App projects: working web apps the agent writes as code.

Apps are small ES-module projects (Preact + htm, no build step) stored as files in
the database. The preview runs them inside an iframe sandboxed WITHOUT
allow-same-origin, so app code gets an opaque origin: it cannot read Creai's
cookies, storage or session, and the page's CSP limits where it can connect.
Data an app saves goes through the app-data API with a token scoped to that one
app. Styling comes from the same themes as sites, via a component kit.
"""

import base64
import hashlib
import hmac
import json
import re
import time

from ..core.config import settings
from ..core.db import conn
from . import site as site_spec

PREACT = "10.29.8"
HTM = "3.1.1"
ESM = "https://esm.sh"
PHASER = "3.90.0"
THREE = "0.170.0"
GSAP = "3.13.0"
CHART = "4.5.0"
DATEFNS = "4.1.0"
MARKED = "15.0.12"
FUSE = "7.1.0"
SORTABLE = "1.15.6"
CONFETTI = "1.9.3"
LUCIDE = "0.544.0"
MOTION = "12.23.12"
FLOATING = "1.7.4"
ZOD = "4.1.5"
D3 = "7.9.0"
EMBLA = "8.6.0"
IMPORTS = {
    "preact": f"{ESM}/preact@{PREACT}",
    "preact/hooks": f"{ESM}/preact@{PREACT}/hooks",
    "htm/preact": f"{ESM}/htm@{HTM}/preact?external=preact",
    # motion: the difference between a form on a page and something that feels built
    "gsap": f"{ESM}/gsap@{GSAP}",
    "gsap/ScrollTrigger": f"{ESM}/gsap@{GSAP}/ScrollTrigger",
    # The right well-known library for the job, pinned. Each is plain JS with no
    # stylesheet of its own, so nothing fights the kit or the sandbox's CSP.
    "lucide": f"{ESM}/lucide@{LUCIDE}",                # icons — a UI without them looks unfinished
    "motion": f"{ESM}/motion@{MOTION}",                # the modern animate(); GSAP for timelines
    "@floating-ui/dom": f"{ESM}/@floating-ui/dom@{FLOATING}",  # menus, tooltips, popovers that fit
    "zod": f"{ESM}/zod@{ZOD}",                         # validate a form before it saves
    "d3": f"{ESM}/d3@{D3}",                            # a chart Chart.js cannot draw
    "embla-carousel": f"{ESM}/embla-carousel@{EMBLA}",  # galleries and sliders that feel right
    "chart.js/auto": f"{ESM}/chart.js@{CHART}/auto",   # dashboards, totals over time
    "date-fns": f"{ESM}/date-fns@{DATEFNS}",           # bookings, invoices, "3 days ago"
    "marked": f"{ESM}/marked@{MARKED}",                # notes, rich descriptions
    "fuse.js": f"{ESM}/fuse.js@{FUSE}",                # search a list that got long
    "sortablejs": f"{ESM}/sortablejs@{SORTABLE}",      # drag to reorder, kanban
    "canvas-confetti": f"{ESM}/canvas-confetti@{CONFETTI}",
    # games and 3D, pinned like everything else
    "phaser": f"{ESM}/phaser@{PHASER}",
    "three": f"{ESM}/three@{THREE}",
    "three/addons/": f"{ESM}/three@{THREE}/examples/jsm/",
    "creai/game": "",                        # filled in per preview from GAME_KIT
}

# Emoji are never an icon: a different typeface on every device, off the baseline,
# unable to take a brand colour, and inconsistent between a phone and a desktop.
# lucide is in the import map for exactly this.
EMOJI = re.compile("[\U0001F000-\U0001FAFF\U0001F1E6-\U0001F1FF"
                   "\u2600-\u27BF\u2B00-\u2BFF\uFE0F\u2049\u203C]")

PATH = re.compile(r"^(?:[a-z0-9][a-z0-9_-]{0,40}/){0,3}[a-z0-9][a-z0-9_.-]{0,60}\.(js|css|json|md)$")
MAX_FILE = 120_000
MAX_FILES = 40
MAX_TOTAL = 800_000
ENTRY = "app.js"
TOKEN_TTL = 12 * 3600

STARTER = {
    "app.js": """import { html, render } from 'htm/preact';
import { useState } from 'preact/hooks';

function App() {
  const [count, setCount] = useState(0);
  return html`
    <main class="shell">
      <header class="topbar"><strong>New app</strong></header>
      <section class="page">
        <h1>Describe your app in the chat</h1>
        <p class="muted">Creai will build the screens, forms and data here.</p>
        <button class="btn" onClick=${() => setCount(count + 1)}>Clicked ${count}</button>
      </section>
    </main>`;
}

render(html`<${App} />`, document.getElementById('root'));
""",
}


class AppError(ValueError):
    pass


# ---------------------------------------------------------------- files

def check(path: str, content: str) -> None:
    if not PATH.match(path or "") or ".." in path:
        raise AppError(f"'{path}' isn't an allowed file name (lowercase folders, .js .css .json .md)")
    if len(content.encode()) > MAX_FILE:
        raise AppError(f"{path} is too large ({len(content)} characters; limit {MAX_FILE})")
    if path.endswith(".js") and re.search(r"\b(eval|Function)\s*\(|document\.cookie|localStorage|"
                                          r"sessionStorage|indexedDB|importScripts", content):
        raise AppError(f"{path} uses a blocked API (eval, Function, cookies, browser storage). "
                       "Save data with the creai.db helper instead.")


async def files(project_id: int, org_id: int) -> dict[str, str]:
    async with conn() as c:
        rows = await c.fetch(
            "SELECT path, content FROM project_files WHERE project_id=$1 AND org_id=$2 ORDER BY path",
            project_id, org_id)
    return {r["path"]: r["content"] for r in rows}


async def write(project_id: int, org_id: int, changes: dict[str, str]) -> list[str]:
    current = await files(project_id, org_id)
    for path, content in changes.items():
        check(path, content)
    merged = {**current, **changes}
    if len(merged) > MAX_FILES:
        raise AppError(f"an app can have at most {MAX_FILES} files")
    if sum(len(v.encode()) for v in merged.values()) > MAX_TOTAL:
        raise AppError("the app is too large; simplify or split it")
    async with conn() as c:
        async with c.transaction():
            for path, content in changes.items():
                await c.execute(
                    """INSERT INTO project_files (org_id, project_id, path, content)
                       VALUES ($1,$2,$3,$4)
                       ON CONFLICT (project_id, path) DO UPDATE
                         SET content=EXCLUDED.content, updated_at=now()""",
                    org_id, project_id, path, content)
    return sorted(changes)


async def delete(project_id: int, org_id: int, path: str) -> bool:
    if path == ENTRY:
        raise AppError("app.js is the entry point and can't be deleted")
    async with conn() as c:
        r = await c.execute("DELETE FROM project_files WHERE project_id=$1 AND org_id=$2 AND path=$3",
                            project_id, org_id, path)
    return r.endswith("1")


async def seed(project_id: int, org_id: int) -> None:
    if not await files(project_id, org_id):
        await write(project_id, org_id, STARTER)


# ---------------------------------------------------------------- app tokens

ROLES = ("owner", "public")
PUBLIC_TTL = 7 * 24 * 3600


def token(project_id: int, org_id: int, role: str = "owner") -> str:
    """The app's own token: who the app is, not who is signed into it. A signed-in
    person is carried separately, by appauth's session, so signing in and out never
    changes the app's identity."""
    ttl = PUBLIC_TTL if role == "public" else TOKEN_TTL
    body = f"{project_id}.{org_id}.{role}.{int(time.time()) + ttl}"
    sig = hmac.new(settings.secret_key.encode(), b"appdata:" + body.encode(), hashlib.sha256).hexdigest()[:32]
    return base64.urlsafe_b64encode(f"{body}.{sig}".encode()).decode().rstrip("=")


def verify(tok: str) -> tuple[int, int, str]:
    try:
        raw = base64.urlsafe_b64decode(tok + "=" * (-len(tok) % 4)).decode()
        pid, oid, role, exp, sig = raw.split(".")
    except (ValueError, UnicodeDecodeError):
        raise AppError("invalid app token")
    want = hmac.new(settings.secret_key.encode(), f"appdata:{pid}.{oid}.{role}.{exp}".encode(),
                    hashlib.sha256).hexdigest()[:32]
    if not hmac.compare_digest(sig, want) or int(exp) < time.time() or role not in ROLES:
        raise AppError("invalid or expired app token")
    return int(pid), int(oid), role


# Who may touch a collection, least to most trusted. "own" is the one that makes
# member apps possible: signed-in people reach their own rows and nobody else's,
# which is what a booking, an order or a timesheet actually needs.
LEVELS = ("public", "user", "own", "owner")


def rules(app_files: dict[str, str]) -> dict:
    """Access rules from app.json. Anything not declared is owner-only when published."""
    try:
        raw = json.loads(app_files.get("app.json") or "{}")
    except ValueError:
        return {}
    out = {}
    for name, r in (raw.get("collections") or {}).items():
        if isinstance(r, dict) and re.match(r"^[a-z][a-z0-9_]{0,40}$", str(name)):
            out[name] = {k: (r.get(k) if r.get(k) in LEVELS else "owner")
                         for k in ("read", "write", "manage")}
    return out


def signin_required(app_files: dict[str, str]) -> bool:
    """True when any collection expects a signed-in person, so the preview and the
    published app know to offer sign-in at all."""
    return any(v in ("user", "own")
               for rule in rules(app_files).values() for v in rule.values())


# ---------------------------------------------------------------- preview

def kit_css(site: dict) -> str:
    """Components styled by the project's theme, so apps look designed."""
    s = site_spec.merge(site or {}, {})
    layout, theme_name = site_spec.design_of(s)
    t = site_spec.THEMES[theme_name]
    pal = s["palette"]
    on = "#141414" if site_spec._lum(pal["accent"]) > 0.4 else "#FFFFFF"
    return f"""
:root{{--bg:{pal['bg']};--ink:{pal['ink']};--accent:{pal['accent']};--on:{on};--r:{t['radius']}px;
--display:'{t['display']}',Georgia,serif;--body:'{t['body']}',system-ui,sans-serif;
--surface:#FFFFFF;--canvas:#F6F5F1;--text:#161616;--muted:#5A5A57;--line:#1414141F;
/* Layered surfaces: a card sits on the canvas, a raised thing sits on the card. */
--sunk:#00000008;--raised:#FFFFFF;--glass:#FFFFFFB8;
/* Depth that reads as light from above, not a grey blur. */
--lift-1:0 1px 2px #14141412,0 1px 1px #1414140A;
--lift-2:0 4px 12px -2px #14141418,0 2px 6px -2px #1414140F;
--lift-3:0 18px 40px -12px #14141426,0 8px 16px -8px #14141414;
--ring:0 0 0 1px #1414140F;
--ease:cubic-bezier(.2,.7,.2,1);--quick:.18s;--calm:.42s}}
@media (prefers-color-scheme:dark){{
:root{{--surface:#17181A;--canvas:#101113;--text:#F2F1EE;--muted:#A0A09C;--line:#FFFFFF1A;
--sunk:#00000040;--raised:#1F2023;--glass:#17181AC0;
--lift-1:0 1px 2px #00000060;--lift-2:0 6px 16px -4px #00000070;
--lift-3:0 22px 48px -16px #00000090;--ring:0 0 0 1px #FFFFFF14}}}}
*{{box-sizing:border-box}}html,body{{margin:0;height:100%}}
body{{font:16px/1.55 var(--body);color:var(--text);background:var(--canvas);
-webkit-font-smoothing:antialiased;text-rendering:optimizeLegibility}}
h1,h2,h3{{font-family:var(--display);font-weight:{t['dw']};letter-spacing:{t['track']};line-height:1.08;margin:0 0 .5em}}
h1{{font-size:clamp(30px,5vw,50px)}}h2{{font-size:clamp(22px,3vw,30px)}}
h3{{font-size:19px;font-family:var(--body);font-weight:600;letter-spacing:0}}
.shell{{min-height:100%;display:flex;flex-direction:column}}
/* The bar stays put and separates from content by blur and a hairline, not a slab. */
.topbar{{position:sticky;top:0;z-index:20;display:flex;align-items:center;justify-content:space-between;
gap:12px;padding:14px 20px;background:var(--bg);color:var(--ink);
box-shadow:var(--lift-2);backdrop-filter:saturate(140%) blur(10px)}}
.topbar nav{{display:flex;gap:6px;flex-wrap:wrap}}
.topbar nav button,.tab{{background:transparent;color:inherit;border:1px solid transparent;padding:7px 12px;
border-radius:999px;font:inherit;cursor:pointer;transition:background var(--quick) var(--ease)}}
.topbar nav button:hover,.tab:hover{{background:#FFFFFF1F}}
.topbar nav button[aria-current=page],.tab[aria-selected=true]{{border-color:currentColor;background:#FFFFFF14}}
.page{{padding:clamp(20px,4vw,40px) 20px;max-width:1080px;width:100%;margin:0 auto}}
.muted{{color:var(--muted)}}
.btn{{position:relative;display:inline-flex;align-items:center;gap:8px;background:var(--accent);
color:var(--on);border:0;padding:11px 18px;border-radius:calc(var(--r) + 4px);
font:600 15px var(--body);cursor:pointer;box-shadow:var(--lift-1);
transition:transform var(--quick) var(--ease),box-shadow var(--quick) var(--ease),filter var(--quick)}}
.btn:hover{{transform:translateY(-1px);box-shadow:var(--lift-2);filter:brightness(1.04)}}
.btn:active{{transform:translateY(0);box-shadow:var(--lift-1)}}
.btn.ghost{{background:var(--surface);color:var(--text);box-shadow:var(--ring),var(--lift-1)}}
.btn.danger{{background:#B42318;color:#fff}}
.btn[disabled]{{opacity:.5;cursor:not-allowed;transform:none;box-shadow:none}}
/* Cards lift toward the reader on hover, and arrive in sequence rather than all at once. */
.card{{background:var(--surface);border-radius:var(--r);padding:clamp(16px,3vw,24px);
box-shadow:var(--ring),var(--lift-1);
transition:transform var(--calm) var(--ease),box-shadow var(--calm) var(--ease);
animation:rise var(--calm) var(--ease) both}}
.card:hover{{transform:translateY(-2px);box-shadow:var(--ring),var(--lift-3)}}
.card.flat:hover{{transform:none;box-shadow:var(--ring),var(--lift-1)}}
.grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(240px,1fr));gap:clamp(12px,2vw,18px)}}
.grid>*:nth-child(1){{animation-delay:.02s}}.grid>*:nth-child(2){{animation-delay:.06s}}
.grid>*:nth-child(3){{animation-delay:.1s}}.grid>*:nth-child(4){{animation-delay:.14s}}
.grid>*:nth-child(5){{animation-delay:.18s}}.grid>*:nth-child(n+6){{animation-delay:.22s}}
.stack{{display:flex;flex-direction:column;gap:clamp(10px,2vw,16px)}}
.row{{display:flex;gap:10px;align-items:center;flex-wrap:wrap}}
label{{display:flex;flex-direction:column;gap:6px;font-size:14px;font-weight:500}}
input,select,textarea{{font:inherit;padding:11px 13px;border:0;border-radius:var(--r);
background:var(--surface);color:var(--text);box-shadow:var(--ring);
transition:box-shadow var(--quick) var(--ease)}}
input:hover,select:hover,textarea:hover{{box-shadow:var(--ring),var(--lift-1)}}
input:focus,select:focus,textarea:focus,.btn:focus-visible{{outline:0;
box-shadow:0 0 0 2px var(--accent),var(--lift-1)}}
table{{width:100%;border-collapse:separate;border-spacing:0;background:var(--surface);
border-radius:var(--r);overflow:hidden;box-shadow:var(--ring),var(--lift-1)}}
th,td{{text-align:left;padding:12px 14px;border-bottom:1px solid var(--line)}}
tr:last-child td{{border-bottom:0}}
tbody tr{{transition:background var(--quick) var(--ease)}}
tbody tr:hover{{background:var(--sunk)}}
th{{font-size:13px;color:var(--muted);font-weight:600;background:var(--sunk)}}
.badge{{display:inline-block;padding:3px 10px;border-radius:999px;font-size:12px;font-weight:600;
background:color-mix(in srgb,var(--accent) 18%,transparent);
box-shadow:inset 0 0 0 1px color-mix(in srgb,var(--accent) 30%,transparent)}}
.stat{{font:{t['dw']} clamp(30px,5vw,40px)/1 var(--display)}}
.empty{{text-align:center;padding:56px 24px;color:var(--muted);border-radius:var(--r);
background:var(--sunk);box-shadow:inset 0 0 0 1px var(--line)}}
.toast{{position:fixed;bottom:18px;left:50%;transform:translateX(-50%);background:var(--text);
color:var(--canvas);padding:11px 18px;border-radius:999px;box-shadow:var(--lift-3);
animation:rise var(--quick) var(--ease) both;z-index:40}}
/* A skeleton beats a spinner: the page keeps its shape while data lands. */
.skeleton{{background:linear-gradient(90deg,var(--sunk),#8882 40%,var(--sunk) 80%);
background-size:200% 100%;animation:sweep 1.4s linear infinite;border-radius:var(--r);
min-height:14px;color:transparent}}
@keyframes rise{{from{{opacity:0;transform:translateY(10px) scale(.985)}}to{{opacity:1;transform:none}}}}
@keyframes sweep{{to{{background-position:-200% 0}}}}
@media (prefers-reduced-motion:reduce){{*{{animation:none!important;transition:none!important}}
.card:hover,.btn:hover{{transform:none}}}}
"""


SDK = """
const API = __API__, AUTH = __AUTH__, TOKEN = __TOKEN__;
// The session lives here, not in app code: app code is blocked from browser
// storage, and in the preview the frame has an opaque origin where storage
// throws anyway. Published apps have a real origin, so it persists there.
let SESSION = null;
try { SESSION = sessionStorage.getItem('creai_app_session'); } catch (e) {}
function remember(s) {
  SESSION = s || null;
  try { s ? sessionStorage.setItem('creai_app_session', s) : sessionStorage.removeItem('creai_app_session'); } catch (e) {}
}
function headers() {
  const h = { 'Content-Type': 'application/json', 'X-App-Token': TOKEN };
  if (SESSION) h['X-App-Session'] = SESSION;
  return h;
}
async function call(method, path, body) {
  const r = await fetch(API + path, { method, headers: headers(),
    body: body ? JSON.stringify(body) : undefined });
  const data = await r.json().catch(() => ({}));
  if (r.status === 401 && SESSION) remember(null);   // expired session: sign out cleanly
  if (!r.ok) throw new Error(data.detail || ('request failed ' + r.status));
  return data;
}
async function authCall(path, body) {
  const r = await fetch(AUTH + path, { method: body ? 'POST' : 'GET', headers: headers(),
    body: body ? JSON.stringify(body) : undefined });
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.detail || 'that did not work');
  return data;
}
const collection = (name) => ({
  list: () => call('GET', '/' + name).then(d => d.items),
  get: (id) => call('GET', '/' + name + '/' + id),
  add: (data) => call('POST', '/' + name, { data }),
  update: (id, data) => call('PATCH', '/' + name + '/' + id, { data }),
  remove: (id) => call('DELETE', '/' + name + '/' + id),
});
const report = (message) => fetch(API + '/_report', { method: 'POST',
  headers: { 'Content-Type': 'application/json', 'X-App-Token': TOKEN },
  body: JSON.stringify({ message }) }).catch(() => {});

// People who use this app. Their accounts belong to this app alone.
let CURRENT = null;
const auth = {
  // Forgotten passwords. forgot() always succeeds, so a stranger can't learn who
  // has an account; the code arrives by email.
  forgot: (email) => authCall('/forgot', { email }),
  async reset(token, password) {
    const d = await authCall('/reset', { token, password });
    remember(d.session); CURRENT = d.user; return d.user;
  },
  async signUp(email, password, name) {
    const d = await authCall('/signup', { email, password, name });
    remember(d.session); CURRENT = d.user; return d.user;
  },
  async signIn(email, password) {
    const d = await authCall('/signin', { email, password });
    remember(d.session); CURRENT = d.user; return d.user;
  },
  signOut() { remember(null); CURRENT = null; },
  // Who is signed in. Returns null when nobody is — never throws, so a screen can
  // simply ask for it and render accordingly.
  async me() {
    if (!SESSION) { CURRENT = null; return null; }
    try { CURRENT = (await authCall('/me')).user; } catch (e) { CURRENT = null; }
    return CURRENT;
  },
  get user() { return CURRENT; },
  get signedIn() { return !!SESSION; },
};
window.creai = { db: { collection }, auth, report };
"""

RUNNER = """
const files = JSON.parse(document.getElementById('files').textContent);
const urls = {};
const report = (msg) => {
  const message = String(msg).slice(0, 500);
  parent.postMessage({ type: 'creai-app-error', message }, '*');
  if (window.creai && window.creai.report) window.creai.report(message);
};
window.addEventListener('error', e => report(e.message + (e.filename ? ' (' + e.filename.split('/').pop() + ':' + e.lineno + ')' : '')));
window.addEventListener('unhandledrejection', e => report(e.reason && e.reason.message || e.reason));
function resolve(from, spec) {
  const base = from.split('/').slice(0, -1);
  for (const part of spec.split('/')) {
    if (part === '..') base.pop(); else if (part !== '.') base.push(part);
  }
  return base.join('/');
}
function load(path) {
  if (urls[path]) return urls[path];
  if (!(path in files)) throw new Error('missing file ' + path);
  const src = files[path].replace(/(from\\s+|import\\s*\\(\\s*|import\\s+)(['"])(\\.{1,2}\\/[^'"]+)\\2/g,
    (m, kw, q, spec) => kw + q + load(resolve(path, spec)) + q);
  urls[path] = URL.createObjectURL(new Blob([src + '\\n//# sourceURL=' + path], { type: 'text/javascript' }));
  return urls[path];
}
// In-page links stay in the app; anything else opens outside the preview.
document.addEventListener('click', (e) => {
  const a = e.target.closest && e.target.closest('a[href]');
  if (!a || e.defaultPrevented) return;
  const href = a.getAttribute('href') || '';
  if (href.startsWith('#')) {
    e.preventDefault();
    const t = href.length > 1 && document.getElementById(decodeURIComponent(href.slice(1)));
    if (t) t.scrollIntoView({ behavior: 'smooth' });
  } else if (/^https:[/][/]/.test(href)) {
    e.preventDefault(); window.open(href, '_blank', 'noopener');
  } else {
    e.preventDefault();
  }
});
for (const [p, css] of Object.entries(files)) if (p.endsWith('.css')) {
  const s = document.createElement('style'); s.textContent = css; document.head.append(s);
}
try { import(load('app.js')).catch(report); } catch (e) { report(e.message); }
"""


def preview(app_files: dict[str, str], site: dict, app_token: str, api_base: str) -> str:
    s = site_spec.merge(site or {}, {})
    _, theme_name = site_spec.design_of(s)
    t = site_spec.THEMES[theme_name]
    code = {p: c for p, c in app_files.items() if p.endswith((".js", ".css", ".json"))}
    blob = json.dumps(code).replace("</", "<\\/")
    sdk = SDK.replace("__API__", json.dumps(api_base.rstrip("/") + "/v1/appdata")) \
             .replace("__AUTH__", json.dumps(api_base.rstrip("/") + "/v1/appauth")) \
             .replace("__TOKEN__", json.dumps(app_token))
    csp = ("default-src 'none'; script-src 'unsafe-inline' blob: data: https://esm.sh; "
           "style-src 'unsafe-inline' https://fonts.googleapis.com; font-src https://fonts.gstatic.com; "
           f"img-src data: blob: https:; media-src blob: https:; connect-src {api_base.rstrip('/')} https://esm.sh; "
           "form-action 'none'; base-uri 'none'")
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="{csp}">
<title>{site_spec.html.escape(s['business'] or 'App')}</title>
{site_spec._fonts(t)}
<style>{kit_css(s)}</style>
<script type="importmap">{json.dumps({"imports": _imports()})}</script>
</head><body><div id="root"></div>
<script id="files" type="application/json">{blob}</script>
<script>{sdk}</script>
<script type="module">{RUNNER}</script>
</body></html>"""


def _imports() -> dict:
    """The sandbox's module map. The game kit rides along as a pinned data module so a
    game can `import { loop, canvas } from 'creai/game'` with no network call."""
    import base64
    return dict(IMPORTS, **{"creai/game": "data:text/javascript;base64,"
                            + base64.b64encode(GAME_KIT.encode()).decode()})


# ---------------------------------------------------------------- self-review

IMPORT_RE = re.compile(r"""^\s*import\s+(?:(?P<what>[\w$*{}\s,]+?)\s+from\s+)?['"](?P<spec>[^'"]+)['"]""", re.M)
DYNAMIC_RE = re.compile(r"""import\(\s*['"](?P<spec>[^'"]+)['"]\s*\)""")
EXPORT_NAMED_RE = re.compile(r"^\s*export\s+(?:async\s+)?(?:function\*?|const|let|var|class)\s+([A-Za-z_$][\w$]*)", re.M)
EXPORT_LIST_RE = re.compile(r"^\s*export\s*\{([^}]*)\}", re.M)
HOOKS = ("useState", "useEffect", "useMemo", "useCallback", "useRef", "useReducer", "useContext", "useLayoutEffect")


def _resolve(from_path: str, spec: str) -> str:
    parts = from_path.split("/")[:-1]
    for seg in spec.split("/"):
        if seg == "..":
            if parts:
                parts.pop()
        elif seg not in (".", ""):
            parts.append(seg)
    return "/".join(parts)


def _exports(src: str) -> tuple[set, bool]:
    names = set(EXPORT_NAMED_RE.findall(src))
    for group in EXPORT_LIST_RE.findall(src):
        for item in group.split(","):
            item = item.strip()
            if item:
                names.add(item.split(" as ")[-1].strip())
    return names, bool(re.search(r"^\s*export\s+default\b", src, re.M))


def _strip_templates(src: str) -> str:
    """Remove every template literal, including ones nested inside ${...}. A regex
    can't do this: html`a ${cond && html`b`} c` needs a scanner, not a pattern.
    Used so the JSX check doesn't see leftover markup from a conditional render."""
    out, i, n = [], 0, len(src)
    while i < n:
        if src[i] != "`":
            out.append(src[i]); i += 1; continue
        i += 1                                     # inside a template literal
        while i < n and src[i] != "`":
            if src[i] == "\\":
                i += 2
            elif src[i] == "$" and i + 1 < n and src[i + 1] == "{":
                i = _skip_expression(src, i + 2)    # may hold templates of its own
            else:
                i += 1
        i += 1                                     # past the closing backtick
        out.append(" ")
    return "".join(out)


def _skip_expression(src: str, i: int) -> int:
    """Index just past the } closing a ${ expression, templates inside included."""
    braces, n = 1, len(src)
    while i < n and braces:
        c = src[i]
        if c == "\\":
            i += 2; continue
        if c == "{":
            braces += 1
        elif c == "}":
            braces -= 1
            if not braces:
                return i + 1
        elif c == "`":
            i += 1
            while i < n and src[i] != "`":
                if src[i] == "\\":
                    i += 2
                elif src[i] == "$" and i + 1 < n and src[i + 1] == "{":
                    i = _skip_expression(src, i + 2)
                else:
                    i += 1
        i += 1
    return i


def _strip_comments(src: str) -> str:
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
    return re.sub(r"(?m)(^|[^:\\\\])//.*$", r"\1", src)


def review(app_files: dict[str, str]) -> dict:
    """What a careful reviewer would flag before calling the app done. Static checks only:
    they catch the mistakes that break a no-build ES-module app at load time."""
    problems, notes = [], []
    js = {p: c for p, c in app_files.items() if p.endswith(".js")}
    entry = js.get(ENTRY, "")
    if not entry:
        problems.append("app.js is missing.")
    elif entry == STARTER[ENTRY]:
        problems.append("app.js is still the starter screen.")
    elif "getElementById('root')" not in entry and 'getElementById("root")' not in entry:
        problems.append("app.js never renders into document.getElementById('root').")
    exports = {p: _exports(c) for p, c in js.items()}
    reachable, queue = set(), [ENTRY] if entry else []
    for path, src in js.items():
        code = _strip_comments(src)
        if code.count("`") % 2:
            problems.append(f"{path}: an odd number of backticks — a template literal is not closed.")
        imports = [(m.group("what") or "", m.group("spec")) for m in IMPORT_RE.finditer(code)]
        imports += [("", m.group("spec")) for m in DYNAMIC_RE.finditer(code)]
        imported = set()
        for what, spec in imports:
            if spec.startswith("."):
                target = _resolve(path, spec)
                if not target.endswith(".js"):
                    problems.append(f"{path}: import '{spec}' needs the .js extension.")
                    continue
                if target not in js:
                    problems.append(f"{path}: imports '{spec}', but {target} doesn't exist.")
                    continue
                names, has_default = exports[target]
                w = what.strip()
                default_part = w.split("{")[0].strip().rstrip(",").strip()
                if default_part and not default_part.startswith("*") and not has_default:
                    problems.append(f"{path}: default-imports {target}, which has no default export.")
                for inner in re.findall(r"\{([^}]*)\}", w):
                    for item in inner.split(","):
                        name = item.strip().split(" as ")[0].strip()
                        if name and name not in names:
                            problems.append(f"{path}: imports {{ {name} }} from {target}, which doesn't export it.")
            elif spec not in IMPORTS and not spec.startswith("three/addons/"):
                problems.append(f"{path}: '{spec}' isn't available. Use preact, preact/hooks, "
                                "htm/preact, lucide, motion, gsap, gsap/ScrollTrigger, "
                                "@floating-ui/dom, zod, chart.js/auto, d3, date-fns, marked, "
                                "fuse.js, sortablejs, embla-carousel, canvas-confetti, three, "
                                "phaser or creai/game.")
            for item in re.split(r"[,{}\s]+", what):
                item = item.strip()
                if item and item not in ("*", "as"):
                    imported.add(item)
        body = IMPORT_RE.sub("", code)
        if re.search(r"\bhtml`", body) and "html" not in imported and not re.search(r"\b(const|let|var|function)\s+html\b", body):
            problems.append(f"{path}: uses html`…` without importing html from 'htm/preact'.")
        for hook in HOOKS:
            if re.search(rf"\b{hook}\s*\(", body) and hook not in imported and not re.search(rf"\bfunction\s+{hook}\b", body):
                problems.append(f"{path}: calls {hook} without importing it from 'preact/hooks'.")
        jsx_body = _strip_templates(body)
        if re.search(r"(=>|return|\(|=)\s*<[A-Za-z][\w.]*[\s/>]", jsx_body):
            problems.append(f"{path}: looks like JSX. Use html`<${{Component}} />` templates instead.")
        if re.search(r"\bclassName=", body):
            notes.append(f"{path}: prefer class= over className= in htm templates.")
    while queue:
        p = queue.pop()
        if p in reachable or p not in js:
            continue
        reachable.add(p)
        for m in IMPORT_RE.finditer(_strip_comments(js[p])):
            if m.group("spec").startswith("."):
                queue.append(_resolve(p, m.group("spec")))
    for p in js:
        if p not in reachable and entry:
            notes.append(f"{p} isn't imported anywhere, so it never runs.")
    if "app.json" in app_files:
        try:
            json.loads(app_files["app.json"])
        except ValueError as exc:
            problems.append(f"app.json isn't valid JSON ({exc}).")
    code_all = "\n".join(js.values())
    used = sorted(set(re.findall(r"collection\(\s*['\"]([a-z][a-z0-9_]{0,40})['\"]", code_all)))
    declared = set(rules(app_files))
    missing = [c for c in used if c not in declared]
    if missing:
        notes.append("No visitor access rule yet for: " + ", ".join(missing)
                     + " (owner-only once published; add them to app.json if visitors should use them).")
    hit = EMOJI.search(code_all)
    if hit:
        problems.append(f"{hit.group(0)} is an emoji being used as an icon. They look different on "
                        "every device and can't take the brand colour. Import { createIcons, icons } "
                        "from 'lucide' and use an <i data-lucide=\"name\"></i>, or an inline SVG.")

    # Accounts: the two halves have to agree, or people meet a locked door.
    uses_auth = "creai.auth" in code_all
    wants_signin = signin_required(app_files)
    if wants_signin and not uses_auth:
        problems.append("app.json expects people to sign in, but nothing calls creai.auth — "
                        "add a sign-in screen, or change those rules to public.")
    if uses_auth and not wants_signin:
        notes.append("The app signs people in, but no collection uses \"user\" or \"own\", so "
                     "signing in changes nothing. Consider \"own\" for anything personal.")
    if uses_auth and not re.search(r"\bauth\.me\s*\(|\bme\s*\(\s*\)", code_all):
        notes.append("Call creai.auth.me() on load, so a returning person stays signed in.")
    if uses_auth and "signOut" not in code_all:
        notes.append("No way to sign out. Put it in the topbar.")
    for name, rule in rules(app_files).items():
        if rule.get("read") == "public" and rule.get("write") in ("user", "own"):
            notes.append(f"{name}: anyone can read it, but only signed-in people can add to it. "
                         "If those rows are personal, read should be \"own\".")
    if used and not re.search(r"catch\s*\(|\.catch\(", code_all):
        notes.append("Data calls have no error handling; show a friendly message when a call fails.")
    if used and not re.search(r"[Ll]oading", code_all):
        notes.append("No loading state found while data loads.")
    return {"ok": not problems, "problems": problems[:20], "notes": notes[:10]}


# ---------------------------------------------------------------- games

GAME_KIT = r"""
// creai/game — the plumbing every browser game needs, so the agent writes the game.
const listeners = new Set();
export const keys = Object.create(null);
const pressed = Object.create(null);
addEventListener('keydown', (e) => { keys[e.key] = true; pressed[e.key] = true; if (['ArrowUp','ArrowDown','ArrowLeft','ArrowRight',' '].includes(e.key)) e.preventDefault(); });
addEventListener('keyup', (e) => { keys[e.key] = false; });
export function tapped(key) { const was = pressed[key]; pressed[key] = false; return !!was; }

export function pointer(canvas) {
  const p = { x: 0, y: 0, down: false, tapped: false };
  const at = (e) => { const r = canvas.getBoundingClientRect(); const t = e.touches ? e.touches[0] : e;
    p.x = (t.clientX - r.left) * (canvas.width / r.width); p.y = (t.clientY - r.top) * (canvas.height / r.height); };
  canvas.addEventListener('pointerdown', (e) => { at(e); p.down = true; p.tapped = true; });
  canvas.addEventListener('pointermove', at);
  addEventListener('pointerup', () => { p.down = false; });
  canvas.addEventListener('touchstart', (e) => { e.preventDefault(); at(e); p.down = true; p.tapped = true; }, { passive: false });
  return p;
}

// A fixed-step loop: the same speed on every machine, paused when the tab is hidden.
export function loop({ update, draw, step = 1 / 60 }) {
  let last = performance.now(), acc = 0, raf = 0, running = true, fps = 60, frames = 0, fpsAt = last;
  function frame(now) {
    raf = requestAnimationFrame(frame);
    if (!running) { last = now; return; }
    acc += Math.min(0.25, (now - last) / 1000); last = now;
    while (acc >= step) { update(step); acc -= step; }
    draw(acc / step);
    frames++; if (now - fpsAt > 1000) { fps = frames * 1000 / (now - fpsAt); frames = 0; fpsAt = now; }
  }
  raf = requestAnimationFrame(frame);
  const onVis = () => { running = document.visibilityState === 'visible'; };
  document.addEventListener('visibilitychange', onVis);
  const api = { stop() { cancelAnimationFrame(raf); document.removeEventListener('visibilitychange', onVis); },
                pause() { running = false; }, resume() { running = true; }, get fps() { return fps; },
                get running() { return running; } };
  listeners.add(api);
  return api;
}

// A canvas that fills its box, stays sharp on phones, and reports its own size.
export function canvas(host, { width = 480, height = 800, fit = 'contain' } = {}) {
  const box = host || document.getElementById('root');
  // the game fills the screen, letterboxed, with nothing to scroll
  document.documentElement.style.height = '100%';
  document.body.style.cssText += ';margin:0;height:100%;overflow:hidden;background:var(--bg,#0b0d10)';
  box.style.cssText += ';display:grid;place-items:center;height:100dvh;width:100%';
  const c = document.createElement('canvas');
  c.width = width; c.height = height;
  c.style.cssText = `display:block;max-width:100%;max-height:100dvh;aspect-ratio:${width}/${height};` +
                    `object-fit:${fit};touch-action:none`;
  box.append(c);
  return c;
}

export function sprite(url) {
  const img = new Image(); img.crossOrigin = 'anonymous'; img.src = url;
  return new Promise((ok, no) => { img.onload = () => ok(img); img.onerror = () => no(new Error('could not load ' + url)); });
}

export async function loadAll(map) {
  const out = {};
  await Promise.all(Object.entries(map).map(async ([k, url]) => { out[k] = await sprite(url); }));
  return out;
}

// Sound without files: short tones for jumps, hits and points.
let audio;
export function beep({ freq = 440, ms = 90, type = 'square', gain = 0.05 } = {}) {
  try {
    audio = audio || new (window.AudioContext || window.webkitAudioContext)();
    if (audio.state === 'suspended') audio.resume();
    const o = audio.createOscillator(), g = audio.createGain();
    o.type = type; o.frequency.value = freq; g.gain.value = gain;
    o.connect(g); g.connect(audio.destination); o.start();
    g.gain.exponentialRampToValueAtTime(0.0001, audio.currentTime + ms / 1000);
    o.stop(audio.currentTime + ms / 1000);
  } catch (e) {}
}

// Saves and scores go through the app's own data, so the owner's rules apply.
const saves = window.creai.db.collection('saves');
const scores = window.creai.db.collection('scores');
export const save = {
  async load(slot = 'default') {
    const all = await saves.list();
    return (all.items || all || []).find((r) => r.slot === slot) || null;
  },
  async put(data, slot = 'default') {
    const found = await save.load(slot);
    return found ? saves.update(found.id, { ...data, slot }) : saves.add({ ...data, slot });
  },
};
export const leaderboard = {
  async top(n = 10) {
    const all = await scores.list();
    return (all.items || all || []).sort((a, b) => (b.score || 0) - (a.score || 0)).slice(0, n);
  },
  add: (name, score) => scores.add({ name: String(name).slice(0, 24), score: Math.round(score) }),
};

export const rand = (a, b) => a + Math.random() * (b - a);
export const clamp = (v, a, b) => Math.max(a, Math.min(b, v));
export const hits = (a, b) => a.x < b.x + b.w && a.x + a.w > b.x && a.y < b.y + b.h && a.y + a.h > b.y;
"""
