"""
A sentence, spoken in a van, becoming a record.

This is the part that makes a voice-first CRM true rather than aspirational.
Transcription gives words; this gives fields. "Just quoted Reyes Roofing eighteen
four for the duplex, chase Thursday" has to become a name, a value, a stage and a
date, or the person is still typing and nothing has been solved.

Three rules it keeps.

Never invent. A field that was not said stays empty. A CRM that guesses a value
is worse than one that leaves it blank, because a wrong number in a pipeline
total is a decision made on a lie — and three quarters of people already say
their CRM data is not accurate.

Numbers as people say them. "Eighteen four" is 18,400 to a contractor and 184 to
a parser that has never met one. "A grand", "twelve fifty", "two and a half k" —
all normal speech, all currently unparseable by a form.

Hand back what it heard. The record is shown filled in before it is saved, so a
mistake costs a tap rather than a wrong number sitting in a forecast.
"""

import json
import logging
import re

import httpx

from ..core.config import settings

log = logging.getLogger("creai.voicelead")

MODEL = "gpt-4o-mini"
API = "https://api.openai.com/v1/chat/completions"

STAGES = ("New", "Quoted", "Won", "Lost")

SYSTEM = """You turn one spoken sentence from a tradesperson into a lead record.

Return ONLY a JSON object, no prose, with these keys:
  who    - the customer or company name, as said. Required; if truly absent, "".
  stage  - one of New, Quoted, Won, Lost. "quoted" -> Quoted, "got the job"/"they
           said yes" -> Won, "went with someone else" -> Lost. Otherwise New.
  value  - the money, in whole units, as a number. null if no amount was said.
  next   - the next action, in their words, short. null if none was said.
  when   - the day it is due, as spoken ("Thursday", "next week"). null if absent.
  heard  - the sentence as you understood it.

Rules:
- Never invent a field. If it was not said, it is null or "".
- Trade speech for money: "eighteen four" = 18400, "twelve fifty" = 1250,
  "two and a half k" = 2500, "a grand" = 1000, "eighteen hundred" = 1800.
  Prefer the reading a contractor would mean, not the literal digits.
- Keep their words in `next`. Do not translate "chase" into "follow up".
"""


class LeadError(RuntimeError):
    """Something the person should be told plainly."""


# Spoken money, for the cases worth catching without asking a model twice.
_WORDS = {"a": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
          "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12,
          "thirteen": 13, "fourteen": 14, "fifteen": 15, "sixteen": 16,
          "seventeen": 17, "eighteen": 18, "nineteen": 19, "twenty": 20}
# "twelve fifty" is 1,250 to a contractor. The second word is a round tens value,
# not a digit, and leaving it out was the difference between 1,250 and nothing.
_TENS = {"ten": 10, "twenty": 20, "thirty": 30, "forty": 40, "fifty": 50,
         "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90}


def money_from_speech(text: str) -> int | None:
    """"eighteen four" -> 18400. The reading a contractor means, not the digits.

    Kept as code rather than left entirely to the model because it is the single
    most consequential field and the failure is silent: nobody notices a lead
    worth 184 until the forecast is wrong.
    """
    t = (text or "").lower().replace(",", "")
    m = re.search(r"\b(\d+(?:\.\d+)?)\s*(k|grand)\b", t)
    if m:
        return int(float(m.group(1)) * 1000)
    m = re.search(r"\b(a|one|two|three|four|five|six|seven|eight|nine|ten)\s+(?:and\s+a\s+half\s+)?"
                  r"(k|grand)\b", t)
    if m:
        base = _WORDS.get(m.group(1), 1) * 1000
        return base + (500 if "and a half" in m.group(0) else 0)
    teens = "|".join(k for k, v in _WORDS.items() if v >= 11)
    # "eighteen four" -> 18,400 : a teens word then a single digit
    m = re.search(rf"\b({teens})\s+(one|two|three|four|five|six|seven|eight|nine)\b", t)
    if m:
        return _WORDS[m.group(1)] * 1000 + _WORDS[m.group(2)] * 100
    # "nine six" -> 9,600 : the same shorthand with a single digit in front. Only
    # after an article or "the", so "four five jobs" is not read as money.
    m = re.search(r"\b(?:the|a)\s+(one|two|three|four|five|six|seven|eight|nine)\s+"
                  r"(one|two|three|four|five|six|seven|eight|nine)\b", t)
    if m:
        return _WORDS[m.group(1)] * 1000 + _WORDS[m.group(2)] * 100
    # "twelve fifty" -> 1,250 : a teens word then a round tens
    m = re.search(rf"\b({teens})\s+({'|'.join(_TENS)})\b", t)
    if m:
        return _WORDS[m.group(1)] * 100 + _TENS[m.group(2)]
    # "eighteen hundred" and "three hundred", said either way
    m = re.search(rf"\b(\d{{1,3}}|{teens}|{'|'.join(_TENS)}|one|two|three|four|five|six|seven|"
                  r"eight|nine)\s+hundred\b", t)
    if m:
        w = m.group(1)
        n = int(w) if w.isdigit() else (_WORDS.get(w) or _TENS.get(w) or 0)
        return n * 100
    m = re.search(r"[£$€]\s?(\d[\d.]*)", t)
    if m:
        return int(float(m.group(1)))
    return None


def _tidy(raw: dict, said: str) -> dict:
    """Trust nothing the model returned without checking it against the sentence."""
    out = {
        "who": (raw.get("who") or "").strip()[:120],
        "stage": raw.get("stage") if raw.get("stage") in STAGES else "New",
        "value": None,
        "next": (raw.get("next") or None),
        "when": (raw.get("when") or None),
        "heard": said,
    }
    value = raw.get("value")
    if isinstance(value, (int, float)) and value > 0:
        out["value"] = int(value)
    # If no amount appears in the words at all, drop whatever the model produced:
    # a value nobody said is the field most likely to be believed and most costly
    # to get wrong.
    spoken = money_from_speech(said)
    if spoken is None and not re.search(r"\d", said):
        out["value"] = None
    elif spoken is not None and (out["value"] is None or
                                 abs(out["value"] - spoken) > max(spoken * 0.5, 100)):
        out["value"] = spoken
    if out["next"]:
        out["next"] = str(out["next"]).strip()[:160]
    if out["when"]:
        out["when"] = str(out["when"]).strip()[:60]
    return out


async def lead_from_speech(said: str) -> dict:
    """One spoken sentence to a lead, ready to be shown before it is saved."""
    said = (said or "").strip()
    if not said:
        raise LeadError("nothing was said")
    if not settings.openai_key:
        raise LeadError("voice isn't configured")

    async with httpx.AsyncClient(timeout=40) as x:
        r = await x.post(API,
                         headers={"Authorization": f"Bearer {settings.openai_key}",
                                  "Content-Type": "application/json"},
                         json={"model": MODEL, "temperature": 0,
                               "response_format": {"type": "json_object"},
                               "messages": [{"role": "system", "content": SYSTEM},
                                            {"role": "user", "content": said}]})
    if r.status_code >= 400:
        log.error("lead parse failed: %s %s", r.status_code, r.text[:200])
        raise LeadError("that couldn't be read back")
    try:
        raw = json.loads(r.json()["choices"][0]["message"]["content"])
    except (KeyError, ValueError, IndexError) as exc:
        raise LeadError("that couldn't be read back") from exc

    lead = _tidy(raw, said)
    if not lead["who"]:
        raise LeadError("I didn't catch who that was for — say the name and try again")
    return lead
