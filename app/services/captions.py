"""
Captions, premium mode.

The look this replaces — white text in a black rounded box, one yellow word,
thick stroke — reads as 2021 creator content. It still works for raw creator
footage; it undercuts anything trying to look like software worth paying for.

What replaces it:

No container. A soft shadow carries contrast where the footage is busy, and
nothing else. A box is a crutch for text that has not been placed carefully.

Two to five words at a time, arriving in rhythm. A subtitle block sitting still
for six seconds is a transcript. Phrases that land as they are said are part of
the edit.

One word carries the weight. The time, the figure, the day. It comes up larger
and in the accent, and everything around it stays quiet — so the eye is told what
matters rather than left to find it.

Placement respects the platform. TikTok and Reels put their own interface over
the bottom fifth and the right edge; captions sit above that, not under it.
"""

import logging
import re
from dataclasses import dataclass

log = logging.getLogger("creai.captions")

# Inter Display: tight, neutral, built for large sizes. The look is the typeface
# plus the restraint, not an effect.
FONT = "/home/claude/fonts/inter/extras/ttf/InterDisplay-Bold.ttf"
FONT_HEAVY = "/home/claude/fonts/inter/extras/ttf/InterDisplay-Black.ttf"

INK = "#F7F5F2"
INK_DARK = "#14181C"          # on light footage, white text simply disappears
ACCENT = "#E8894A"
ACCENT_DARK = "#B5541F"       # the same accent, legible on a pale background

# What the platforms cover. Captions sit above the bottom band and clear of the
# right-hand column of buttons.
SAFE_BOTTOM = 0.22          # share of height reserved at the bottom
SAFE_SIDES = 0.10

MAX_WORDS = 5


@dataclass
class Phrase:
    words: str
    start: float
    end: float
    emphasis: str = ""       # the one word that carries the line


# Things worth emphasising: a time, an amount, a weekday, a plain number.
_STRONG = re.compile(
    r"^(?:\$[\d,]+|\d{1,2}:\d{2}|\d[\d,.]*|"
    r"monday|tuesday|wednesday|thursday|friday|saturday|sunday)$", re.I)


# The caption is not the transcript. A voice says "six forty-seven"; the screen
# should say 6:47. A voice says "eighteen four"; the screen says $18,400. Written
# out, those read as a subtitle of somebody talking — set as figures, they read as
# the thing itself, and the number is what the viewer remembers.
AS_WRITTEN = {
    "six forty-seven": "6:47",
    "eighteen four": "$18,400",
    "nine six": "$9,600",
    "creai dot dev": "creai.dev",
    "kree-aye dot dev": "creai.dev",
}


def as_written(text: str) -> str:
    """The line as it should be SEEN, which is rarely how it is said."""
    out = text or ""
    for spoken, shown in AS_WRITTEN.items():
        out = re.sub(re.escape(spoken), shown, out, flags=re.I)
    return out


def pick_emphasis(words: str) -> str:
    """The single word that should carry the phrase, or nothing.

    One word only. Emphasising three is emphasising none, and a line where
    everything shouts reads as the old style with a new typeface.
    """
    for w in words.split():
        bare = w.strip(".,!?").lower()
        if _STRONG.match(bare):
            return w.strip(".,!?")
    return ""


def phrases(text: str, start: float, end: float, words: list[dict] | None = None) -> list[Phrase]:
    """A line broken into phrases that arrive in rhythm.

    Real word timings are used when they exist, so a phrase lands as it is said.
    Without them the line is divided evenly, which is a guess and looks like one
    on close inspection — but never wrong enough to notice at speed.
    """
    text = as_written(text)
    chunks, current = [], []
    for w in (text or "").split():
        current.append(w)
        if len(current) >= MAX_WORDS or w.endswith((".", "!", "?", ",")):
            chunks.append(" ".join(current)); current = []
    if current:
        chunks.append(" ".join(current))
    if not chunks:
        return []

    out = []
    if words:
        # walk the transcript, giving each chunk the span of its own words
        idx = 0
        for c in chunks:
            n = len(c.split())
            span = words[idx:idx + n]
            idx += n
            if not span:
                break
            out.append(Phrase(c, float(span[0]["start"]),
                              float(span[-1].get("end", span[-1]["start"] + 0.35)),
                              pick_emphasis(c)))
    else:
        step = (end - start) / len(chunks)
        for i, c in enumerate(chunks):
            out.append(Phrase(c, start + i * step, start + (i + 1) * step, pick_emphasis(c)))
    return out


def _esc(t: str) -> str:
    return (t.replace("\\", "").replace(":", "\\:").replace("'", "\u2019")
             .replace("%", "\\%").replace("—", "-"))


def draw(phrase: Phrase, w: int, h: int, *, size: float = 0.072,
         light: bool = False) -> str:
    """One drawtext filter for a phrase: no box, a soft shadow, one word lifted.

    The emphasised word is drawn as its own layer at a larger size and in the
    accent, with the rest of the phrase drawn around it. Two layers rather than
    one because ffmpeg has no rich text — but the result is what matters, and it
    is the difference between typography and a subtitle.
    """
    fs = int(w * size)
    y = int(h * (1 - SAFE_BOTTOM) - fs * 1.4)
    # White on cream is invisible. The app screens are pale, the footage is dark,
    # and a caption that assumes one of them disappears on the other.
    ink = INK_DARK if light else INK
    accent = ACCENT_DARK if light else ACCENT
    shadow = (":shadowcolor=white@0.45:shadowx=0:shadowy=2" if light
              else ":shadowcolor=black@0.55:shadowx=0:shadowy=3")
    window = f":enable='between(t,{phrase.start:.2f},{phrase.end:.2f})'"

    if not phrase.emphasis:
        return (f"drawtext=fontfile={FONT}:text='{_esc(phrase.words)}':fontcolor={ink}"
                f":fontsize={fs}:x=(w-text_w)/2:y={y}{shadow}{window}")

    # the phrase with its strong word removed, then the strong word beneath it
    rest = phrase.words.replace(phrase.emphasis, "").replace("  ", " ").strip(" .,")
    big = int(fs * 1.5)
    layers = [
        f"drawtext=fontfile={FONT_HEAVY}:text='{_esc(phrase.emphasis)}':fontcolor={accent}"
        f":fontsize={big}:x=(w-text_w)/2:y={y - int(big * 0.55)}{shadow}{window}"
    ]
    if rest:
        layers.append(
            f"drawtext=fontfile={FONT}:text='{_esc(rest)}':fontcolor={ink}"
            f":fontsize={fs}:x=(w-text_w)/2:y={y + int(fs * 0.7)}{shadow}{window}")
    return ",".join(layers)


def filters(text: str, start: float, end: float, w: int, h: int,
            words: list[dict] | None = None, light: bool = False) -> str:
    """Every caption layer for one line, ready to append to a filter chain.

    `light` for pale footage — the app screens. Getting this wrong does not
    produce an error, it produces a caption nobody can read.
    """
    parts = [draw(p, w, h, light=light) for p in phrases(text, start, end, words)]
    return ("," + ",".join(parts)) if parts else ""
