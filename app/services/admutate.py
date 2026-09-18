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

# The axes worth varying, and why each one is here rather than being a style knob.
AXES = {
    # The first line decides whether the rest is watched at all.
    "hook": ("objection", "moment", "number", "confession", "question"),
    # Same product, different person — the one that most changes who responds.
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


def plan(concept: dict, want: int = 12, seed: int | None = None) -> list[dict]:
    """A set of variants worth running.

    concept carries what does not change: the product, the thing being shown, the
    sentence being said. The axes carry what does.
    """
    if want < 1:
        raise MutationError("ask for at least one")
    base_trade = concept.get("trade") or "plumber"
    rng = random.Random(seed if seed is not None else 0)

    grid = [c for c in itertools.product(AXES["hook"], AXES["open"], AXES["length"],
                                         AXES["style"], AXES["cta"], AXES["pace"])]
    rng.shuffle(grid)

    # Take the combination that is currently least represented, rather than the
    # next one off a shuffled pile. A random draw clustered seven of twelve on one
    # opening, which is a lottery ticket rather than an experiment: an axis value
    # seen twice cannot be compared with one seen seven times.
    counts: dict[tuple[str, str], int] = {}

    def imbalance(c) -> int:
        hook, open_on, length, style, cta, pace = c
        pairs = (("hook", hook), ("open", open_on), ("length", str(length)),
                 ("style", style), ("cta", cta), ("pace", pace))
        return sum(counts.get(p, 0) for p in pairs)

    out, seen = [], set()
    while len(out) < want and grid:
        grid.sort(key=imbalance)
        hook, open_on, length, style, cta, pace = grid.pop(0)
        # A cold product open cannot also be a person-first opening. Contradictions
        # produce variants that were never really the thing they claim to be.
        if style == "demo" and open_on == "person":
            continue
        if style == "ugc" and open_on == "product":
            continue
        if length == 15 and pace == "measured":
            continue                            # fifteen seconds is not measured

        trade = base_trade if len(out) % 2 == 0 else rng.choice(
            [t for t in AXES["trade"] if t != base_trade])

        v = {"hook": hook, "open": open_on, "length": length, "style": style,
             "cta": cta, "pace": pace, "trade": trade}
        k = _key(v)
        if k in seen:
            continue
        seen.add(k)
        for name, val in (("hook", hook), ("open", open_on), ("length", str(length)),
                          ("style", style), ("cta", cta), ("pace", pace)):
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


def brief(concept: dict, v: dict) -> str:
    """What the agent is told to write for this one variant."""
    shown = concept.get("shows") or "the product doing the thing"
    said = concept.get("said") or ""
    return "\n".join([
        f"Write a {v['length']}-second vertical ad for a {v['trade']}.",
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
