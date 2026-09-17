# CreAI renderer

The platform's eyes. Two endpoints, both guarded by `X-Render-Token`:

- `POST /shot` — HTML in, screenshots at phone and desktop widths, plus measurements
  (overflow, text under 12px, contrast below WCAG AA, images without alt text, headings).
- `POST /smoke` — an app preview in, loaded the way the real preview loads it: console and
  reported errors, clicks through the main controls, fills and submits the first form,
  and a screenshot. App data calls are answered from memory, so flows run without
  touching real data.

Requests to anything other than the allowed asset hosts are blocked, and the browser
never holds CreAI credentials.

Env: `RENDER_TOKEN` (required), `RENDER_ALLOWED_HOSTS` (optional, comma separated).
