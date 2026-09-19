"""
Whether a game is ready for a store, before anyone tries.

Two Apple guidelines decide whether a platform that ships many games survives
review. 4.2, minimum functionality: is there enough here to be an app at all?
4.3, spam: does it look like a copy of something else — including something
else we shipped? A platform generating games invites both, and a rejection
comes back weeks later with no appeal worth having.

So this checks before submission, deterministically where it can, and refuses
to submit what it flags. The check is the product's defence against becoming
the thing Apple built the rule for.

The ownership rule is fixed and not configurable: submissions go under the
customer's developer account, with their keys. Domains are registered in
their name; payments go into their Stripe; apps ship under their account.
Creai never publishes a customer's game as its own.
"""

import logging
import re

log = logging.getLogger("creai.storeready")

# Apple's own required icon sizes for iOS, and Google's for Play. Missing one
# is an automatic rejection, and it is the most common cause of a first-time
# submission failing for a reason that has nothing to do with the game.
IOS_ICON_SIZES = (20, 29, 40, 58, 60, 76, 80, 87, 120, 152, 167, 180, 1024)
PLAY_ICON_SIZE = 512
PLAY_FEATURE_GRAPHIC = (1024, 500)

# Screenshot device classes Apple requires at least one of.
IOS_SCREENSHOT_CLASSES = ("6.7-inch", "6.5-inch", "5.5-inch")

# Words in a listing that read as generated, and that reviewers and players
# have both learned to distrust.
HOLLOW = ("addictive", "immersive", "next-level", "the ultimate", "revolutionary",
          "endless fun", "for all ages", "you won't believe")


def minimum_functionality(game_files: dict[str, str], concept: dict | None = None) -> list[str]:
    """Apple 4.2. Is there enough here to be an app?

    Judged from the source, which is what we have. Thin games get rejected
    with the line 'your app provides a minimal user experience', and there is
    no arguing with it afterwards.
    """
    problems = []
    src = "\n".join(v for k, v in game_files.items() if k.endswith((".gd", ".cs", ".tscn")))
    scripts = [k for k in game_files if k.endswith((".gd", ".cs"))]
    if len(scripts) < 2:
        problems.append("one script file; a store game needs more than a single screen of logic")
    if not re.search(r"score|points|progress|level|wave|rack", src, re.I):
        problems.append("no score, level or progress — nothing for a player to come back for")
    if not re.search(r"pause|game.?over|restart|play.?again|retry", src, re.I):
        problems.append("no pause, game-over or restart — the states a store reviewer taps first")
    if not re.search(r"save|load|user://|PlayerPrefs|persist", src, re.I):
        problems.append("nothing persists between sessions; a best score at least")
    if concept and not (concept.get("twist") or "").strip():
        problems.append("the concept has no twist, so there is no reason to prefer this to the "
                        "one it resembles")
    return problems


def _shape(concept: dict) -> str:
    """A rough fingerprint of a concept, for spotting near-copies of our own."""
    words = re.findall(r"[a-z]{4,}", " ".join(
        str(concept.get(k) or "") for k in ("mechanic", "resembles", "twist")).lower())
    return " ".join(sorted(set(words))[:10])


def spam_check(concept: dict, others: list[dict]) -> list[str]:
    """Apple 4.3. Does this look like a copy — including of something we shipped?

    `others` is every concept we have already submitted. A platform that ships
    ten games with the same mechanic and a different colour is exactly what the
    guideline names, and the first rejection tends to take the rest with it.
    """
    problems = []
    if concept.get("is_clone") and not (concept.get("twist") or "").strip():
        problems.append("a clone with no twist; Apple 4.3 names this exactly")
    mine = _shape(concept)
    for other in others:
        if other is concept:
            continue
        if mine and _shape(other) == mine:
            problems.append(f"indistinguishable in mechanic and twist from "
                            f"'{other.get('title', 'another game we shipped')}'")
            break
    title = (concept.get("title") or "").lower()
    for other in others:
        if title and title == (other.get("title") or "").lower():
            problems.append(f"the title '{concept.get('title')}' is already ours")
            break
    return problems


def listing_check(listing: dict) -> list[str]:
    """The store listing itself: complete, sized, and not obviously generated."""
    problems = []
    title = listing.get("title") or ""
    if not title:
        problems.append("no title")
    elif len(title) > 30:
        problems.append(f"title is {len(title)} characters; the App Store allows 30")
    sub = listing.get("subtitle") or ""
    if len(sub) > 30:
        problems.append(f"subtitle is {len(sub)} characters; the App Store allows 30")
    desc = listing.get("description") or ""
    if len(desc) < 80:
        problems.append("description is too short to say what the game is")
    if len(desc) > 4000:
        problems.append("description exceeds the 4,000-character limit")
    low = (desc + " " + sub).lower()
    for w in HOLLOW:
        if w in low:
            problems.append(f"'{w}' reads as generated copy; say what the game actually is")
            break
    if not listing.get("privacy_policy_url"):
        problems.append("no privacy policy URL; required for every submission")
    if not listing.get("age_rating"):
        problems.append("no age rating chosen")
    icons = set(listing.get("icon_sizes") or [])
    missing = [s for s in IOS_ICON_SIZES if s not in icons]
    if missing:
        problems.append(f"missing iOS icon sizes: {missing[:5]}{'…' if len(missing) > 5 else ''}")
    shots = listing.get("screenshots") or {}
    if not any(shots.get(c) for c in IOS_SCREENSHOT_CLASSES):
        problems.append("no screenshots for any required iPhone size")
    return problems


def ownership_check(account: dict) -> list[str]:
    """Submissions go under the customer's account. Not negotiable."""
    problems = []
    if not account.get("apple_issuer_id") or not account.get("apple_key_id"):
        problems.append("no App Store Connect API key from the customer's own developer account")
    if not account.get("play_service_account"):
        problems.append("no Play Console service account from the customer's own account")
    if account.get("owner") == "creai":
        problems.append("the account is Creai's; a customer's game ships under the customer's "
                        "account, never ours")
    return problems


def ready(game_files: dict, concept: dict, listing: dict, account: dict,
          shipped: list[dict] | None = None) -> dict:
    """Everything, in the order it would fail review."""
    blocks = {
        "functionality": minimum_functionality(game_files, concept),
        "spam": spam_check(concept, shipped or []),
        "listing": listing_check(listing),
        "ownership": ownership_check(account),
    }
    total = sum(len(v) for v in blocks.values())
    return {"ready": total == 0, "problems": blocks, "count": total,
            "note": ("" if total == 0 else
                     "Not submitted. Fix these first; a store rejection comes back in weeks "
                     "and a spam rejection can take every game we have shipped with it.")}
