"""
Making fifty ads out of one idea — and learning which part did the work.

Producing variations is the easy half. The half people skip is attribution: if
fifty ads go out and three perform, the useful question is not "which three" but
"which choices". A short hook? A named trade? A cold open on the product? Without
that, the next fifty are another lottery.

So every variant carries the axis values that produced it, and results roll up by
axis rather than by ad. Ten ads sharing a hook style are ten samples of that
style, not ten separate anecdotes.

Two constraints hold the set honest.

Every mutation must still be a good ad. The conversion rules — product on screen
immediately, no brand name in the opening, one idea per scene — are not one of
the axes to vary. A variant that breaks them is not an experiment, it is a wasted
impression, and the person paid for it.

Different has to mean different. Two variants that differ only in a comma teach
nothing and split the sample. Anything that reads as a near-duplicate of one
already in the set is dropped rather than shipped to make up a number.
"""

import hashlib
import itertools
import logging
import random
import re

log = logging.getLogger("creai.admutate")

# The five angles a creative library needs. These are not variations of each other
# — they are different reasons to buy, and a set missing four of them is one idea
# tested five ways.
#
# Note what 'proof' requires. It is reported as the largest single lever available,
# and it is the one angle that cannot be written: it needs a customer who actually
# said something. Generating it would be fabricating a testimonial, so it is
# refused until real proof exists rather than quietly filled in.
ANGLES = {
    "demo":     "Show the value becoming obvious. Do not say it removes the stain; "
                "show the stain going. This is the strongest angle for a product "
                "nobody has seen before.",
    "problem":  "Sit in the thing they already resent, before offering anything. "
                "Name it more precisely than they would themselves.",
    "outcome":  "Open at the end: the evening back, the job entered, the invoice "
                "paid. Then show what produced it.",
    "nothing":  "Compare with carrying on as they are — not with a competitor. "
                "The real alternative is the spreadsheet and the memory.",
    "proof":    "A real customer, in their own words, about their own result. "
                "Requires a real quote from a real person; never written for them.",
}
NEEDS_REAL_PROOF = {"proof"}

# The axes worth varying, and why each one is here rather than being a style knob.
AXES = {
    "angle": tuple(ANGLES),
    # The first line decides whether the rest is watched at all.
    "hook": ("objection", "moment", "number", "confession", "question"),
    # Who it is for. The axis that most changes who responds, and the one that
    # differs per product: Creai serves trades, Arbi serves people selling and
    # people buying. Supplied per concept; this is only the fallback.
    "trade": ("plumber", "electrician", "bookkeeper", "baker", "landscaper", "dentist"),
    # What the first frame is. Cold product open is the current best guess, not a law.
    "open": ("product", "person", "result"),
    "length": (15, 20, 30),
    "style": ("demo", "ugc", "cinematic"),
    "cta": ("free", "try", "see"),
    "pace": ("quick", "measured"),
}

# How each hook actually opens, written out so a variant is a real alternative
# rather than a relabelled one.
HOOKS = {
    "objection": "Name the thing they already resent doing.",
    "moment": "Drop them into a specific time and place — a van at 6:47pm.",
    "number": "Open on a figure, stated flatly, with no setup.",
    "confession": "First person, slightly against interest: “I stopped doing X.”",
    "question": "Ask the thing they have thought and never said aloud.",
}

CTAS = {
    "free": "Free to start.",
    "try": "Try it on your own business.",
    "see": "See what it builds for you.",
}

# What each style is allowed to be, so 'ugc' does not quietly become a rendered
# person pretending to be real.
#
# The line that matters: a generated or stock human may APPEAR, but may never
# CLAIM. They can be in the van at dusk. They cannot tell you the product works.
# A face that does not exist making a testimonial is caught in the comments, and
# most stock licences forbid implying endorsement in any case.
STYLE_RULES = {
    "demo": "Real screen capture only. No people. The product is the whole argument.",
    "ugc": "A real person, filmed on a phone, talking to camera, then the real screen. "
           "A real person — not a generated or stock one. This variant is written "
           "now and filmed later by an actual human.",
    "cinematic": "A few seconds of establishing footage — a van at dusk, a workshop, "
                 "a counter at closing — then real product. Generated or stock "
                 "footage is fine here because nobody speaks and nothing is claimed. "
                 "The footage never shows the product, any text, or a face making a "
                 "recommendation.",
}

# Styles that cannot be finished without a person in front of a camera. Everything
# else renders tonight; these wait, with the script ready to hand over.
NEEDS_A_PERSON = {"ugc"}


class MutationError(ValueError):
    pass


def _key(v: dict) -> str:
    return "|".join(f"{k}={v[k]}" for k in sorted(v))


def _shape(line: str) -> str:
    """A rough fingerprint of a sentence, for spotting near-duplicates. Words that
    carry meaning, sorted — so two lines with the same content in a different
    order are correctly seen as the same line."""
    words = re.findall(r"[a-z]{4,}", (line or "").lower())
    return " ".join(sorted(set(words))[:8])


def available_angles(concept: dict) -> tuple[str, ...]:
    """Which angles can be made honestly for this concept right now.

    'proof' drops out unless a real customer quote is supplied. An invented
    testimonial is the one mistake that costs more than any variant can win, and
    the absence is worth seeing in the plan rather than papering over.
    """
    have = [a for a in ANGLES if a not in NEEDS_REAL_PROOF]
    if (concept.get("proof") or {}).get("quote") and (concept.get("proof") or {}).get("who"):
        have.append("proof")
    return tuple(have)


def plan(concept: dict, want: int = 12, seed: int | None = None) -> list[dict]:
    """A set of variants worth running.

    concept carries what does not change: the product, the thing being shown, the
    sentence being said. The axes carry what does.
    """
    if want < 1:
        raise MutationError("ask for at least one")
    base_trade = concept.get("trade") or "plumber"
    rng = random.Random(seed if seed is not None else 0)

    angles = available_angles(concept)
    # A concept may bring its own audiences. Arbi's are not plumbers.
    audiences = tuple(concept.get("audiences") or AXES["trade"])
    grid = [c for c in itertools.product(angles, AXES["hook"], AXES["open"], AXES["length"],
                                         AXES["style"], AXES["cta"], AXES["pace"])]
    rng.shuffle(grid)

    # Take the combination that is currently least represented, rather than the
    # next one off a shuffled pile. A random draw clustered seven of twelve on one
    # opening, which is a lottery ticket rather than an experiment: an axis value
    # seen twice cannot be compared with one seen seven times.
    counts: dict[tuple[str, str], int] = {}

    def imbalance(c) -> int:
        angle, hook, open_on, length, style, cta, pace = c
        # the angle is weighted: an unbalanced angle spread is a worse failure
        # than an unbalanced pace, because angles are what actually differ
        pairs = (("hook", hook), ("open", open_on), ("length", str(length)),
                 ("style", style), ("cta", cta), ("pace", pace))
        return counts.get(("angle", angle), 0) * 3 + sum(counts.get(p, 0) for p in pairs)

    out, seen = [], set()
    while len(out) < want and grid:
        grid.sort(key=imbalance)
        angle, hook, open_on, length, style, cta, pace = grid.pop(0)
        # A cold product open cannot also be a person-first opening. Contradictions
        # produce variants that were never really the thing they claim to be.
        if style == "demo" and open_on == "person":
            continue
        if style == "ugc" and open_on == "product":
            continue
        if length == 15 and pace == "measured":
            continue                            # fifteen seconds is not measured

        others = [t for t in audiences if t != base_trade] or list(audiences)
        trade = base_trade if len(out) % 2 == 0 else rng.choice(others)

        v = {"angle": angle, "hook": hook, "open": open_on, "length": length,
             "style": style, "cta": cta, "pace": pace, "trade": trade}
        k = _key(v)
        if k in seen:
            continue
        seen.add(k)
        for name, val in (("angle", angle), ("hook", hook), ("open", open_on),
                          ("length", str(length)), ("style", style), ("cta", cta),
                          ("pace", pace)):
            counts[(name, val)] = counts.get((name, val), 0) + 1
        needs = v["style"] in NEEDS_A_PERSON
        out.append({**v, "id": hashlib.sha256(k.encode()).hexdigest()[:10],
                    "brief": brief(concept, v),
                    "needs_a_person": needs,
                    "status": "to film" if needs else "ready to render"})

    if not out:
        raise MutationError("those settings leave nothing to make")
    return out


def to_film(variants: list[dict]) -> list[dict]:
    """The ones waiting on a real person, with their scripts ready to hand over."""
    return [v for v in variants if v.get("needs_a_person")]


# One filmed clip, many variants.
#
# A person films the lines once. Every other version — different trade, different
# hook, different length, another language — is the same footage redubbed, with
# the mouth re-synced to new audio. The endorsement stays genuine because a real
# person really said a version of it; only the wording moves.
#
# This is the only honest way to get UGC volume. Generating a presenter who does
# not exist produces a testimonial nobody gave, and that is the one failure that
# costs more trust than any variant can win.
REDUB_RATE = 0.07          # VEED Lipsync v2 on fal, per second of output


def shoot_list(variants: list[dict], concept: dict) -> dict:
    """What to ask a person to say, once, so everything else can be redubbed.

    Returns the lines to film and what the redubs would cost, so the decision to
    book somebody is made against a number rather than a feeling.
    """
    filming = to_film(variants)
    if not filming:
        return {"lines": [], "variants": 0, "redub_seconds": 0, "redub_cost": 0.0}

    # Film the longest version of each distinct opening. A shorter cut is a
    # trim of a longer one; the reverse needs another shoot.
    by_hook: dict[str, dict] = {}
    for v in filming:
        cur = by_hook.get(v["hook"])
        if cur is None or v["length"] > cur["length"]:
            by_hook[v["hook"]] = v

    lines = [{
        "hook": h,
        "direction": HOOKS[h],
        "seconds": v["length"],
        "say": f"An opening line for a {v['trade']} in the style: {HOOKS[h]} "
               f"Then: “{concept.get('said', '')}”",
        "covers": sorted(x["id"] for x in filming if x["hook"] == h),
    } for h, v in sorted(by_hook.items())]

    seconds = sum(v["length"] for v in filming)
    return {
        "lines": lines,
        "shoot_minutes": max(10, len(lines) * 4),
        "variants": len(filming),
        "redub_seconds": seconds,
        "redub_cost": round(seconds * REDUB_RATE, 2),
        "note": "Film these once. Every variant above is the same footage with the "
                "mouth re-synced to new audio, so the person really did say it.",
    }


def brief(concept: dict, v: dict) -> str:
    """What the agent is told to write for this one variant."""
    shown = concept.get("shows") or "the product doing the thing"
    said = concept.get("said") or ""
    return "\n".join([
        f"Write a {v['length']}-second vertical ad for a {v['trade']}.",
        f"Angle: {ANGLES[v['angle']]}",
        f"Style: {STYLE_RULES[v['style']]}",
        f"Open on: {v['open']}. {HOOKS[v['hook']]}",
        f"Pace: {'short lines, cut often' if v['pace'] == 'quick' else 'fewer lines, let them land'}.",
        f"What it shows: {shown}",
        (f"The spoken line, unchanged: “{said}”" if said else ""),
        f"End on: {CTAS[v['cta']]}",
        "",
        "Rules that are not up for variation:",
        "- The product is on screen within the first two seconds.",
        "- The brand name does not appear in the opening line.",
        "- One idea per scene, written to work with the sound off.",
        "- Nothing claimed that the product does not do.",
        "- The first two seconds must move. A still frame with text on it does not",
        "  stop a thumb: something enters, changes or is said by a person.",
        "- The last third states what to do and why now.",
    ]).strip()


def drop_near_duplicates(variants: list[dict]) -> list[dict]:
    """Remove variants whose opening line says the same thing as one already kept.
    Two ads that differ by a comma split the sample and teach nothing."""
    kept, shapes = [], set()
    for v in variants:
        s = _shape(v.get("opening_line") or "")
        if s and s in shapes:
            log.info("dropped near-duplicate: %s", v.get("id"))
            continue
        if s:
            shapes.add(s)
        kept.append(v)
    return kept


def learn(results: list[dict]) -> dict:
    """Roll results up by axis value rather than by ad.

    results: [{id, hook, open, length, style, cta, pace, trade, plays, actions}]
    Ten ads sharing a hook are ten samples of that hook.
    """
    out: dict[str, dict] = {}
    for axis in AXES:
        buckets: dict[str, dict] = {}
        for r in results:
            val = r.get(axis)
            if val is None:
                continue
            b = buckets.setdefault(str(val), {"ads": 0, "plays": 0, "actions": 0})
            b["ads"] += 1
            b["plays"] += int(r.get("plays") or 0)
            b["actions"] += int(r.get("actions") or 0)
        for b in buckets.values():
            b["rate"] = round(b["actions"] / b["plays"], 4) if b["plays"] else None
        # Ordered by rate, but a value with almost no plays is not a finding; the
        # caller can see the sample size and decide.
        out[axis] = dict(sorted(buckets.items(),
                                key=lambda kv: (kv[1]["rate"] or 0), reverse=True))
    return out


def enough(results: list[dict], axis: str, min_plays: int = 1000) -> bool:
    """Whether an axis has been watched enough to believe. Called before anybody
    declares a winner off forty impressions."""
    per = {}
    for r in results:
        per[str(r.get(axis))] = per.get(str(r.get(axis)), 0) + int(r.get("plays") or 0)
    return bool(per) and min(per.values()) >= min_plays
