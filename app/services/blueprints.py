"""
Blueprints — the handful of things nearly every business asks for.

A CRM and a content store are not new systems. They are collections with access
rules and a few good screens, on primitives that already exist here. What they
need is not code so much as judgement: which fields are required, what happens
when somebody is in a hurry, and who the thing is actually for.

The judgement in these two comes from why the real ones fail.

A CRM fails on entry, not on features. Somewhere between half and two thirds of
implementations fail, and poor adoption beats every technical cause. A third of
salespeople lose over an hour a day to typing things in; three quarters say less
than half their data is right. The fix is not more fields — it is fewer, and a
way to add something while driving. So: one required field, voice in, and the
screen tells you who to chase rather than asking you to maintain it.

A content store fails when it becomes a second tool. The whole point here is that
there is no editor to learn, so this covers only what changes often — a price, a
post, an opening time — and leaves design in the conversation where it belongs.
"""

import logging

log = logging.getLogger("creai.blueprints")


# ---------------------------------------------------------------- CRM

CRM_COLLECTIONS = {
    # 'own' so a salesperson sees their own pipeline and nobody else's, enforced
    # in SQL rather than by the screen remembering to filter.
    "leads": {"read": "own", "write": "own", "manage": "owner"},
    "notes": {"read": "own", "write": "own", "manage": "owner"},
}

# One required field. Every additional required field is a reason not to bother,
# and an unentered deal is worth less than a half-entered one.
CRM_FIELDS = [
    {"name": "who", "label": "Who", "required": True,
     "hint": "A name is enough. Everything else can wait."},
    {"name": "stage", "label": "Stage", "required": False,
     "options": ["New", "Quoted", "Won", "Lost"], "default": "New"},
    {"name": "value", "label": "Worth", "required": False, "kind": "money"},
    {"name": "next", "label": "Next thing to do", "required": False},
    {"name": "when", "label": "By when", "required": False, "kind": "date"},
]

CRM_BRIEF = """
Build a CRM for this business, shaped to how they actually work.

What matters, in order of how badly it fails when ignored:

- ONE required field: who it is. Anything else is optional. Most CRMs die because
  entering a lead costs more than it returns, so make entry nearly free.
- Voice first. A prominent "say it" control that records a sentence and fills the
  form from it: "just quoted Reyes Roofing eighteen four for the duplex, chase
  Thursday" should produce a lead with a name, a value, a stage and a follow-up.
  Somebody in a van will never type that, and that person is the customer.
- The screen tells them what to do. Open with who needs chasing today — quotes
  going cold, follow-ups due — not with a table to maintain. A CRM that asks to
  be fed gets abandoned inside a quarter.
- Built for the person selling, not the person watching. No required activity
  logging, no fields that exist only so a manager can audit. If the business has
  several people, each sees their own leads.
- Their words, not CRM words. A plumber has jobs and quotes, not opportunities
  and pipeline velocity. Use the language they used when describing the business.

Collections: leads and notes, both 'own', so each person's pipeline is theirs.
""".strip()


# ---------------------------------------------------------------- content

CMS_COLLECTIONS = {
    # Public to read because the site shows it; only the owner may change it.
    "content": {"read": "public", "write": "owner", "manage": "owner"},
}

CMS_BRIEF = """
Build a small content store for the parts of this business that change often.

What this is for: prices, menu items, opening hours, posts, listings,
availability — the things a business updates weekly and should never need a
conversation to change.

What this is NOT: a website editor. Layout, colours, wording of the page itself
all stay in the conversation. Building a second editor here would hand back the
exact problem Creai exists to remove.

So:
- Only the things they said change often. If they did not mention it changing,
  leave it out.
- Each entry has a draft and a published state, and nothing on the live site
  changes until they publish it.
- Publishing is one tap from a phone. Most of these edits happen standing in a
  shop, not at a desk.
- Show them the live version beside the draft, so they can see what changes.
- Read is public because the site renders it; only the owner may write.
""".strip()


BLUEPRINTS = {
    "crm": {
        "name": "CRM",
        "what": "Track leads, quotes and who to chase — with voice entry so it "
                "actually gets used.",
        "collections": CRM_COLLECTIONS,
        "fields": CRM_FIELDS,
        "brief": CRM_BRIEF,
        "why": "Most CRMs fail on adoption, not features: entering a lead costs "
               "more than it returns. This one takes a spoken sentence.",
    },
    "content": {
        "name": "Content",
        "what": "Change prices, posts and hours yourself, without a conversation.",
        "collections": CMS_COLLECTIONS,
        "brief": CMS_BRIEF,
        "why": "The recurring things should be editable in seconds. The design "
               "stays in the conversation, where there is no editor to learn.",
    },
}


def get(name: str) -> dict | None:
    return BLUEPRINTS.get((name or "").lower().strip())


def listing() -> list[dict]:
    return [{"id": k, "name": v["name"], "what": v["what"], "why": v["why"]}
            for k, v in BLUEPRINTS.items()]


def brief_for(name: str, business: str, trade: str = "") -> str:
    """What the agent is told when someone asks for one of these."""
    bp = get(name)
    if not bp:
        raise KeyError(name)
    who = f"{business}" + (f", a {trade}" if trade else "")
    return (f"{bp['brief']}\n\nThis is for {who}. Use their words for everything "
            "a customer would recognise, and keep the screens to what they asked for.")
