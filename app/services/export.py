"""
Taking your work with you.

The loudest fear people bring to an AI builder is that the thing they built is
not really theirs — that the code can't be exported, and that if the company
goes away, so does the site. It is a reasonable fear and reviewers of other
builders say it plainly.

So this exists, and it is honest about what it hands over. A site exports as a
page you can put on any host. An app exports as its real source, together with a
README that says exactly which parts still depend on Creai's API and what it
would take to replace them. A half-truth here would be worse than nothing: the
point is that nobody is locked in, not that everything works everywhere.
"""

import io
import json
import zipfile
from datetime import datetime, timezone

from ..core.db import conn
from . import appfs, godot
from . import site as site_spec

SITE_NOTE = """# {name}

Exported from Creai on {when}.

## What is here

- `index.html` — your published page, standing on its own. Open it, or put it on
  any web host. It needs nothing else to run.
- `site.json` — the description your page was built from: headline, sections,
  colours, typeface. Keep it if you ever want to come back.

## What to know

The page loads its typefaces from Google Fonts and its pictures from the web
addresses in the HTML. Both keep working anywhere. If you would rather hold the
pictures yourself, download them and point the `src` attributes at your copies.

There is no tracking, no analytics and no scripts other than a small animation
helper Creai wrote, which is inline at the bottom of the page.

Your domain is registered in your name. Moving it needs nothing from us.
"""

APP_NOTE = """# {name}

Exported from Creai on {when}.

## What is here

- `files/` — the real source of your app, exactly as it runs. Plain JavaScript
  modules, no build step.
- `app.json` — who may read and write each kind of data.

## What runs anywhere, and what does not

The screens, the layout and the logic are yours and need nothing from Creai.

These call Creai's API, and would need replacing to run elsewhere:

- `creai.db` — where your records are kept
- `creai.auth` — the accounts people sign in with
- `creai.files` — files people upload
- `creai.pay` — taking payments

They are ordinary HTTPS calls, so any small backend can answer them. Your data
itself is not in this file: ask us to export it and we will send it as JSON.

We would rather tell you this plainly than let you discover it later.
"""

GAME_NOTE = """# {name}

Exported from Creai on {when}.

## What is here

The Godot project, as source. Open the folder in Godot 4.5 or later and it runs.
Export it yourself to the web, to a desktop, or anywhere else Godot goes.

Nothing in it depends on Creai.
"""


def _stamp() -> str:
    return datetime.now(timezone.utc).strftime("%-d %B %Y")


async def bundle(project_id: int, org_id: int) -> tuple[str, bytes]:
    """(filename, zip bytes) for a project, whatever kind it is."""
    async with conn() as c:
        p = await c.fetchrow(
            "SELECT id, name, path, answers FROM projects WHERE id=$1 AND org_id=$2",
            project_id, org_id)
    if not p:
        raise LookupError("no such project")

    name = p["name"] or "project"
    safe = "".join(ch if ch.isalnum() or ch in "-_ " else "" for ch in name).strip() or "project"
    buf = io.BytesIO()

    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        if p["path"] == "game":
            files = await godot.files(project_id, org_id)
            for path, content in sorted(files.items()):
                z.writestr(f"files/{path}", content)
            z.writestr("README.md", GAME_NOTE.format(name=name, when=_stamp()))
        elif p["path"] == "app":
            files = await appfs.files(project_id, org_id)
            for path, content in sorted(files.items()):
                z.writestr(f"files/{path}", content)
            z.writestr("README.md", APP_NOTE.format(name=name, when=_stamp()))
        else:
            spec = (p["answers"] or {}).get("site") or {}
            z.writestr("index.html", site_spec.render(spec))
            z.writestr("site.json", json.dumps(spec, indent=2, sort_keys=True))
            z.writestr("README.md", SITE_NOTE.format(name=name, when=_stamp()))

    return f"{safe}.zip", buf.getvalue()


async def records(project_id: int, org_id: int) -> dict:
    """Everything an app has stored, as plain JSON. Their data, on request."""
    async with conn() as c:
        rows = await c.fetch(
            """SELECT collection, data, created_at FROM app_records
               WHERE project_id=$1 AND org_id=$2 ORDER BY collection, id""",
            project_id, org_id)
        users = await c.fetch(
            """SELECT email, name, created_at FROM app_users
               WHERE project_id=$1 AND org_id=$2 ORDER BY id""", project_id, org_id)
    out: dict = {"collections": {}, "users": []}
    for r in rows:
        out["collections"].setdefault(r["collection"], []).append(
            {**(r["data"] or {}), "created_at": r["created_at"].isoformat()})
    # Never a password hash, not even to the owner: they cannot need it, and a
    # copy in a download is a copy that can leak.
    out["users"] = [{"email": u["email"], "name": u["name"],
                     "created_at": u["created_at"].isoformat()} for u in users]
    return out
