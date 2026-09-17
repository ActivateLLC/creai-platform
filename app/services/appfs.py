"""
App projects: working web apps the agent writes as code.

Apps are small ES-module projects (Preact + htm, no build step) stored as files in
the database. The preview runs them inside an iframe sandboxed WITHOUT
allow-same-origin, so app code gets an opaque origin: it cannot read CreAI's
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
IMPORTS = {
    "preact": f"{ESM}/preact@{PREACT}",
    "preact/hooks": f"{ESM}/preact@{PREACT}/hooks",
    "htm/preact": f"{ESM}/htm@{HTM}/preact?external=preact",
}

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
        <p class="muted">CreAI will build the screens, forms and data here.</p>
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


def rules(app_files: dict[str, str]) -> dict:
    """Access rules from app.json. Anything not declared is owner-only when published."""
    try:
        raw = json.loads(app_files.get("app.json") or "{}")
    except ValueError:
        return {}
    out = {}
    for name, r in (raw.get("collections") or {}).items():
        if isinstance(r, dict) and re.match(r"^[a-z][a-z0-9_]{0,40}$", str(name)):
            out[name] = {k: ("public" if r.get(k) == "public" else "owner") for k in ("read", "write", "manage")}
    return out


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
--surface:#FFFFFF;--canvas:#F6F5F1;--text:#161616;--muted:#5A5A57;--line:#1414141F}}
*{{box-sizing:border-box}}html,body{{margin:0;height:100%}}
body{{font:16px/1.55 var(--body);color:var(--text);background:var(--canvas)}}
h1,h2,h3{{font-family:var(--display);font-weight:{t['dw']};letter-spacing:{t['track']};line-height:1.1;margin:0 0 .5em}}
h1{{font-size:clamp(28px,4vw,44px)}}h2{{font-size:26px}}h3{{font-size:19px;font-family:var(--body);font-weight:600;letter-spacing:0}}
.shell{{min-height:100%;display:flex;flex-direction:column}}
.topbar{{display:flex;align-items:center;justify-content:space-between;gap:12px;padding:14px 20px;
background:var(--bg);color:var(--ink)}}
.topbar nav{{display:flex;gap:6px;flex-wrap:wrap}}
.topbar nav button,.tab{{background:transparent;color:inherit;border:1px solid transparent;padding:7px 12px;
border-radius:999px;font:inherit;cursor:pointer}}
.topbar nav button[aria-current=page],.tab[aria-selected=true]{{border-color:currentColor}}
.page{{padding:28px 20px;max-width:1080px;width:100%;margin:0 auto}}
.muted{{color:var(--muted)}}
.btn{{display:inline-flex;align-items:center;gap:8px;background:var(--accent);color:var(--on);border:0;
padding:11px 18px;border-radius:calc(var(--r) + 4px);font:600 15px var(--body);cursor:pointer;
transition:transform .2s cubic-bezier(.2,.7,.2,1),filter .2s}}
.btn:hover{{transform:translateY(-1px);filter:brightness(1.05)}}.btn:active{{transform:none}}
.btn.ghost{{background:transparent;color:var(--text);border:1px solid var(--line)}}
.btn.danger{{background:#B42318;color:#fff}}.btn[disabled]{{opacity:.5;cursor:not-allowed}}
.card{{background:var(--surface);border:1px solid var(--line);border-radius:var(--r);padding:20px;
animation:in .35s cubic-bezier(.2,.7,.2,1) both}}
.grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(240px,1fr));gap:14px}}
.stack{{display:flex;flex-direction:column;gap:12px}}.row{{display:flex;gap:10px;align-items:center;flex-wrap:wrap}}
label{{display:flex;flex-direction:column;gap:6px;font-size:14px;font-weight:500}}
input,select,textarea{{font:inherit;padding:10px 12px;border:1px solid var(--line);border-radius:var(--r);
background:#fff;color:var(--text)}}
input:focus,select:focus,textarea:focus,.btn:focus-visible{{outline:2px solid var(--accent);outline-offset:1px}}
table{{width:100%;border-collapse:collapse;background:var(--surface);border-radius:var(--r);overflow:hidden}}
th,td{{text-align:left;padding:10px 12px;border-bottom:1px solid var(--line)}}th{{font-size:13px;color:var(--muted);font-weight:600}}
.badge{{display:inline-block;padding:2px 9px;border-radius:999px;font-size:12px;font-weight:600;
background:color-mix(in srgb,var(--accent) 18%,transparent)}}
.stat{{font:{t['dw']} 34px/1 var(--display)}}
.empty{{text-align:center;padding:48px 20px;color:var(--muted);border:1px dashed var(--line);border-radius:var(--r)}}
.toast{{position:fixed;bottom:18px;left:50%;transform:translateX(-50%);background:var(--text);color:#fff;
padding:10px 16px;border-radius:999px;animation:in .3s both}}
@keyframes in{{from{{opacity:0;transform:translateY(8px)}}to{{opacity:1;transform:none}}}}
@media (prefers-reduced-motion:reduce){{*{{animation:none!important;transition:none!important}}}}
"""


SDK = """
const API = __API__, TOKEN = __TOKEN__;
async function call(method, path, body) {
  const r = await fetch(API + path, { method, headers: { 'Content-Type': 'application/json',
    'X-App-Token': TOKEN }, body: body ? JSON.stringify(body) : undefined });
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.detail || ('request failed ' + r.status));
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
window.creai = { db: { collection }, report };
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
             .replace("__TOKEN__", json.dumps(app_token))
    csp = ("default-src 'none'; script-src 'unsafe-inline' blob: https://esm.sh; "
           "style-src 'unsafe-inline' https://fonts.googleapis.com; font-src https://fonts.gstatic.com; "
           f"img-src data: blob: https:; media-src blob: https:; connect-src {api_base.rstrip('/')} https://esm.sh; "
           "form-action 'none'; base-uri 'none'")
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="{csp}">
<title>{site_spec.html.escape(s['business'] or 'App')}</title>
{site_spec._fonts(t)}
<style>{kit_css(s)}</style>
<script type="importmap">{json.dumps({"imports": IMPORTS})}</script>
</head><body><div id="root"></div>
<script id="files" type="application/json">{blob}</script>
<script>{sdk}</script>
<script type="module">{RUNNER}</script>
</body></html>"""


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
            elif spec not in IMPORTS:
                problems.append(f"{path}: '{spec}' isn't available. Use only preact, preact/hooks and htm/preact.")
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
        jsx_body = re.sub(r"html`(?:[^`\\]|\\.)*`", "", body, flags=re.S)
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
    if used and not re.search(r"catch\s*\(|\.catch\(", code_all):
        notes.append("Data calls have no error handling; show a friendly message when a call fails.")
    if used and not re.search(r"[Ll]oading", code_all):
        notes.append("No loading state found while data loads.")
    return {"ok": not problems, "problems": problems[:20], "notes": notes[:10]}
