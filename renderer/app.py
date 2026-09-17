"""
CreAI renderer — the platform's eyes.

Two jobs, both in a throwaway browser page:

  /shot   render a site's HTML and return screenshots at phone and desktop widths,
          plus measurements a reviewer can't eyeball reliably (overflow, tiny text,
          weak contrast, images without alt text).
  /smoke  load an app the way the preview does, collect console errors, then click
          through its main controls and submit its first form to see it actually work.

The browser never sees CreAI's cookies: pages are loaded from the HTML we pass in,
scripts run in a page with no origin of ours, and every request to anywhere other
than the allowed asset hosts is blocked. One shared token guards the service.
"""

import asyncio
import base64
import os
import re
from contextlib import asynccontextmanager

from fastapi import FastAPI, Header, HTTPException
from playwright.async_api import async_playwright
from pydantic import BaseModel, Field

TOKEN = os.getenv("RENDER_TOKEN", "")
ALLOWED = tuple(h for h in os.getenv(
    "RENDER_ALLOWED_HOSTS",
    "fonts.googleapis.com,fonts.gstatic.com,esm.sh,v3.fal.media,fal.media,images.unsplash.com,app.creai.dev",
).split(",") if h)
SHOT_TIMEOUT = 25_000
VIEWPORTS = {"phone": (390, 844), "desktop": (1280, 900)}
MAX_HTML = 2_000_000

browser = None


@asynccontextmanager
async def lifespan(_: FastAPI):
    global browser
    pw = await async_playwright().start()
    browser = await pw.chromium.launch(args=["--disable-dev-shm-usage"])
    yield
    await browser.close()
    await pw.stop()


app = FastAPI(title="CreAI renderer", lifespan=lifespan)


def check(token: str | None):
    if not TOKEN or token != TOKEN:
        raise HTTPException(401, "bad token")


class ShotIn(BaseModel):
    html: str = Field(max_length=MAX_HTML)
    widths: list[str] = Field(default_factory=lambda: ["phone", "desktop"])
    full_page: bool = True


class SmokeIn(BaseModel):
    html: str = Field(max_length=MAX_HTML)
    clicks: int = Field(4, ge=0, le=8)
    fill_form: bool = True


REPORT_CATCH = """
window.__creaiErrors = [];
window.addEventListener('message', (e) => {
  if (e.data && e.data.type === 'creai-app-error') window.__creaiErrors.push(String(e.data.message).slice(0, 300));
});
window.addEventListener('error', (e) => window.__creaiErrors.push(String(e.message).slice(0, 300)));
"""


async def _page(width: str, sandboxed: bool):
    w, h = VIEWPORTS.get(width, VIEWPORTS["phone"])
    ctx = await browser.new_context(viewport={"width": w, "height": h},
                                    device_scale_factor=2 if width == "phone" else 1,
                                    is_mobile=width == "phone", has_touch=width == "phone",
                                    java_script_enabled=sandboxed, service_workers="block")

    async def guard(route):
        host = re.sub(r"^https?://([^/:]+).*$", r"\1", route.request.url)
        if route.request.url.startswith(("data:", "blob:")) or host.endswith(ALLOWED):
            return await route.continue_()
        await route.abort()

    await ctx.route("**/*", guard)
    reported: list[str] = []
    if sandboxed:
        await _stub_app_data(ctx, reported)   # registered after the guard, so it wins
    page = await ctx.new_page()
    return ctx, page, reported


async def _stub_app_data(target, reported: list):
    """Answer the app's own data calls from memory, so a smoke run exercises real
    flows (add, list, update) without touching anyone's data. The app's error
    reports are captured here rather than guessed at."""
    store: dict[str, list] = {}
    seq = {"n": 0}

    async def handle(route):
        import json as _json
        req = route.request
        path = re.sub(r"^https?://[^/]+/v1/appdata/?", "", req.url).split("?")[0]
        parts = [p for p in path.split("/") if p]
        name = parts[0] if parts else ""
        rows = store.setdefault(name, [])
        body = {}
        if req.method in ("POST", "PATCH"):
            try:
                body = _json.loads(req.post_data or "{}").get("data", {})
            except ValueError:
                body = {}
        if name == "_report":
            try:
                msg = _json.loads(req.post_data or "{}").get("message")
            except ValueError:
                msg = None
            if msg:
                reported.append(str(msg)[:300])
            return await route.fulfill(status=200, content_type="application/json", body='{"ok":true}')
        if req.method == "GET" and len(parts) == 1:
            out = {"items": list(reversed(rows))}
        elif req.method == "GET":
            out = next((r for r in rows if str(r["id"]) == parts[1]), None) or {}
        elif req.method == "POST":
            seq["n"] += 1
            out = {"id": seq["n"], **body, "created_at": "2026-01-01T00:00:00+00:00"}
            rows.append(out)
        elif req.method == "PATCH":
            out = next((r for r in rows if str(r["id"]) == parts[1]), {})
            out.update(body)
        else:
            rows[:] = [r for r in rows if str(r["id"]) != (parts[1] if len(parts) > 1 else "")]
            out = {"ok": True}
        await route.fulfill(status=200, content_type="application/json", body=_json.dumps(out))

    await target.route(re.compile(r"https?://[^/]+/v1/appdata.*"), handle)


MEASURE = """() => {
  const out = { overflow: [], tiny_text: [], low_contrast: [], images_without_alt: 0, empty_links: 0 };
  const lum = (c) => { const [r, g, b] = c.match(/\\d+(\\.\\d+)?/g).slice(0, 3).map(Number).map(v => {
    v /= 255; return v <= 0.03928 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4); });
    return 0.2126 * r + 0.7152 * g + 0.0722 * b; };
  const bgOf = (el) => { let n = el; while (n && n !== document.documentElement) {
    const c = getComputedStyle(n).backgroundColor;
    if (c && !/rgba?\\(0, 0, 0, 0\\)|transparent/.test(c)) return c; n = n.parentElement; }
    return 'rgb(255, 255, 255)'; };
  const docW = document.documentElement.clientWidth;
  for (const el of document.querySelectorAll('body *')) {
    const r = el.getBoundingClientRect();
    if (r.width && r.right > docW + 2 && getComputedStyle(el).position !== 'fixed')
      out.overflow.push({ tag: el.tagName.toLowerCase(), cls: el.className.toString().slice(0, 40), over: Math.round(r.right - docW) });
    const text = (el.textContent || '').trim();
    const direct = [...el.childNodes].some(n => n.nodeType === 3 && n.textContent.trim());
    if (direct && text) {
      const st = getComputedStyle(el);
      const size = parseFloat(st.fontSize);
      if (size && size < 12) out.tiny_text.push({ text: text.slice(0, 40), px: Math.round(size * 10) / 10 });
      try {
        const a = lum(st.color), b = lum(bgOf(el));
        const ratio = (Math.max(a, b) + 0.05) / (Math.min(a, b) + 0.05);
        const big = size >= 24 || (size >= 18.66 && parseInt(st.fontWeight) >= 700);
        if (ratio < (big ? 3 : 4.5)) out.low_contrast.push({ text: text.slice(0, 40), ratio: Math.round(ratio * 10) / 10, px: Math.round(size) });
      } catch (e) {}
    }
  }
  for (const im of document.images) if (!im.getAttribute('alt')) out.images_without_alt++;
  for (const a of document.querySelectorAll('a')) if (!(a.textContent || '').trim() && !a.querySelector('img,svg')) out.empty_links++;
  out.overflow = out.overflow.slice(0, 8); out.tiny_text = out.tiny_text.slice(0, 8); out.low_contrast = out.low_contrast.slice(0, 8);
  out.title = document.title || null;
  out.headings = [...document.querySelectorAll('h1,h2,h3')].slice(0, 12).map(h => h.tagName + ': ' + (h.textContent || '').trim().slice(0, 60));
  return out;
}"""


@app.get("/health")
async def health():
    return {"ok": True, "browser": bool(browser and browser.is_connected())}


@app.post("/shot")
async def shot(body: ShotIn, x_render_token: str | None = Header(None)):
    check(x_render_token)
    out = {"shots": {}, "measure": {}}
    for width in [w for w in body.widths if w in VIEWPORTS]:
        ctx, page, _ = await _page(width, sandboxed=False)
        try:
            await page.set_content(body.html, wait_until="load", timeout=SHOT_TIMEOUT)
            await page.wait_for_timeout(700)
            out["measure"][width] = await page.evaluate(MEASURE)
            png = await page.screenshot(full_page=body.full_page and width == "desktop", scale="css")
            out["shots"][width] = base64.b64encode(png).decode()
        except Exception as exc:
            out.setdefault("errors", []).append(f"{width}: {exc}")
        finally:
            await ctx.close()
    return out


SMOKE = """async (clicks) => {
  const seen = [];
  const vis = (el) => { const r = el.getBoundingClientRect(); return r.width > 4 && r.height > 4; };
  const targets = [...document.querySelectorAll('button, [role=button], nav a, .btn')].filter(vis).slice(0, clicks);
  for (const t of targets) {
    const label = (t.textContent || t.getAttribute('aria-label') || '').trim().slice(0, 40);
    try { t.click(); await new Promise(r => setTimeout(r, 450)); seen.push({ clicked: label, ok: true }); }
    catch (e) { seen.push({ clicked: label, ok: false, error: String(e).slice(0, 120) }); }
  }
  return seen;
}"""


CANVAS_CHECK = """async () => {
  const c = document.querySelector('canvas');
  if (!c) return null;
  const frames = await new Promise((ok) => { let n = 0; const t = performance.now();
    const step = () => { n++; performance.now() - t < 700 ? requestAnimationFrame(step) : ok(n); };
    requestAnimationFrame(step); });
  let drew = false;
  try {
    const ctx = c.getContext('2d');
    if (ctx) { const d = ctx.getImageData(0, 0, c.width, c.height).data;
      for (let i = 0; i < d.length; i += 4000) { if (d[i] !== d[0] || d[i+1] !== d[1] || d[i+2] !== d[2]) { drew = true; break; } } }
    else drew = true;                                   // webgl: assume drawn, the screenshot shows it
  } catch (e) { drew = true; }
  return { width: c.width, height: c.height, fps: Math.round(frames / 0.7), drew };
}"""


@app.post("/smoke")
async def smoke(body: SmokeIn, x_render_token: str | None = Header(None)):
    check(x_render_token)
    ctx, page, reported = await _page("phone", sandboxed=True)
    errors, requests = [], []
    page.on("console", lambda m: errors.append(m.text[:300]) if m.type == "error" else None)
    page.on("pageerror", lambda e: errors.append(str(e)[:300]))
    page.on("requestfailed", lambda r: requests.append(f"{r.method} {r.url[:120]} — {r.failure}"))
    out = {"errors": [], "blocked_requests": [], "interactions": [], "form": None}
    try:
        await page.set_content(body.html, wait_until="load", timeout=SHOT_TIMEOUT)
        await page.evaluate(REPORT_CATCH)                       # belt and braces alongside the report capture
        await page.wait_for_timeout(2500)                       # modules load and first render
        out["rendered"] = bool((await page.inner_html("#root")).strip()) if await page.query_selector("#root") else False
        out["text"] = (await page.inner_text("body"))[:1200]
        out["canvas"] = await page.evaluate(CANVAS_CHECK)
        out["interactions"] = await page.evaluate(SMOKE, body.clicks)
        if body.fill_form:
            form = await page.query_selector("form")
            if form:
                filled = 0
                for inp in (await form.query_selector_all("input, textarea"))[:5]:
                    kind = (await inp.get_attribute("type")) or "text"
                    if kind in ("checkbox", "radio", "file", "hidden", "submit"):
                        continue
                    await inp.fill("Test entry" if kind in ("text", "search", "") else
                                   "test@example.com" if kind == "email" else "5551234567" if kind == "tel" else "5")
                    filled += 1
                before = await page.inner_text("body")
                submit = await form.query_selector("button[type=submit], button:not([type])")
                if submit:
                    await submit.click()
                    await page.wait_for_timeout(1800)
                after = await page.inner_text("body")
                out["form"] = {"fields_filled": filled, "submitted": bool(submit), "page_changed": before != after}
        await page.wait_for_timeout(400)
        png = await page.screenshot(scale="css")
        out["shot"] = base64.b64encode(png).decode()
    except Exception as exc:
        out["errors"].append(f"load: {exc}")
    finally:
        try:
            out["_reported"] = await page.evaluate("() => window.__creaiErrors || []")
        except Exception:
            out["_reported"] = []
        await ctx.close()
    out["_reported"] = list(dict.fromkeys(reported + out.get("_reported", [])))
    out["errors"] = list(dict.fromkeys(out.pop("_reported", []) + errors + out["errors"]))[:8]
    out["blocked_requests"] = list(dict.fromkeys(requests))[:5]
    return out
