# Making an advertisement

Everything below is wired. Where something is still done by hand it says so.

## The chain

```
brief ─→ admutate ─→ producer ─→ shots ─→ cutter ─→ critic ─→ approval queue
         variants     the sheet   pictures  the film  a look    nothing posts
                                                                without a yes
```

Each stage is a module under `app/services/` and knows nothing about the others
beyond the shape it is handed.

---

## 1. Variants — `admutate.py`

One concept becomes a balanced set. Axes: **angle** (demo, problem, outcome,
comparison-with-nothing, proof), hook, opening, length, style, CTA, pace, trade.

Balanced deliberately: a random draw put seven of twelve variants on one opening,
which is a lottery rather than an experiment. Every axis value now appears within
one or two of every other, so a result means something.

- `proof` is refused unless a real customer quote is supplied. It is the largest
  single lever and the one angle that cannot be written.
- `ugc` variants are separated as **to film** — a real person, later. `shoot_list()`
  collapses them to the fewest takes and prices the redubs at $0.07/sec.
- `learn()` rolls results up **by axis**, not by ad. Ten ads sharing a hook are ten
  samples of that hook. `enough()` refuses to call a winner on forty impressions.

## 2. The sheet — `producer.py`

`gpt-6-astra` writes the script **and directs it**. Per scene: line, who says it,
what is shown, **angle, move, lens, light, tone, sound, music, caption**, seconds,
and one line on why that angle.

The angle grammar, which carries the story:

| | |
|---|---|
| `high` | camera above — the subject shrinks. Where the problem lives. |
| `eye` | level — neutral, the register of real footage. |
| `low` | camera below — the subject gains authority. The payoff. |
| `over` | over a shoulder — the product becomes *theirs*, not something shown to them. |
| `macro` | very close — where the thing actually happens. |

**High on the problem, low on the payoff, across the same person.** That is the move.

`check()` refuses: one angle throughout, opening low, ending high, no over/macro
shot, more than one tilt, a locked-off opening, one tone across every scene, an
effect on nearly every beat, a bed that lifts twice or runs under the last line,
captions on most scenes.

## 3. Pictures — `shots.py`

`make_still()` generates, **judges**, and **sharpens** in one call.

- **Judge before animating.** Motion costs roughly twenty times a still, and the
  fault is always visible in the frame. Rejects hands, faces, garbled text,
  merged objects. Retries with the judge's own suggested addition — regenerating
  the identical prompt mostly reproduces the identical fault.
- **Sharpen before animating, never after.** A soft frame makes soft video, and
  scaling that up enlarges the blur. `faithful` (ESRGAN) keeps a face the same
  person between shots; `detailed` (Clarity) adds detail and can drift.
- **`put_screen_on()`** places a real screen recording onto a surface in a scene
  with a perspective transform. No video model renders an interface legibly —
  generated phones show a smudge. The real screen goes on afterwards.
- **`shoot()`** is the whole sequence in one call: generate, judge, sharpen,
  animate. It exists in one place because the order is easy to get wrong —
  judging after animating wastes the expensive step, sharpening after animating
  enlarges the blur rather than removing it. The camera move goes at the **end**
  of the motion prompt; embedded mid-sentence the model reinterprets the subject
  instead of the camera.
- **`end_card()`** — the mark, the address, the reason. Uses `assets/brand/mark.png`,
  the transparent one. `logo.png` carries a dark plate that shows as a box on any
  background but its own.

## 4. The film — `cutter.py`

A sheet becomes an mp4, unattended. Assets are optional: **a scene with no
footage is shot rather than refused**, so a production sheet on its own is enough
to make a film. Supplying footage stays the better path where it exists — real
product capture beats anything generated, and costs nothing.

- Footage is checked **before any voice is bought** — a missing asset used to cost
  a full set of paid calls to discover something knowable up front.
- Scene length follows the line, never the reverse. Squeezing speech into a slot
  is why AI ads sound rushed at the end of every sentence.
- Captions measure the footage brightness and switch to dark ink on pale screens.
  White on cream is invisible and nothing reports it.
- Audio is concatenated as WAV and encoded once. Stream-copying AAC fragments
  produces a file that plays as noise with no error anywhere.
- The bed ducks under the voice by sidechain, and fades out over the last four
  seconds so the ask lands dry.
- **Every cut ends with the sign-off.** Left to the sheet, it gets forgotten.

## 5. The read — `voicedirect.py`

Every beat gets its own direction, generated separately: exhausted → sharp →
curious → quick → relieved → certain. One instruction across the whole ad is a
voice setting, not a performance, and a model never hears the contrast between
lines, so the range has to be written in.

- Silence is **cut in**, not requested. A model asked to pause mid-line reads
  straight through, so short opening sentences are split and real silence is
  inserted.
- Narrator and customer are **different voices** — the voice cuts where the camera
  cuts. `ash` is retired by name.
- The customer's own line is directed *not to perform*: somebody talking into
  their phone does not emphasise words.
- Brand names are spelled for the ear — `Creai` → `Kree-aye`, `Arbi` → `AR-bee` —
  in the text the model reads, never in what the viewer sees.

## 6. Captions — `captions.py`

No container, Inter Display, two to five words, one word lifted.

**The caption is not the transcript.** A voice says "eighteen four"; the screen
says **$18,400**. Written out it reads as a subtitle of somebody talking; set as a
figure it reads as the thing itself.

## 7. Sound — `sound.py`

ACE-Step for beds (Apache-2.0), Stable Audio for effects, both hosted on fal.

**The licence is checked before anything is paid for.** MusicGen is CC-BY-NC and
is refused by name for advertising, with the alternative offered. Finding out
afterwards means the money is gone and the cut is built around the track.

## 8. The critic — `critic.py`

Pulls frames weighted toward the opening and reviews what is actually on screen —
the plan can be right and the render wrong. Checks the first frame moves, captions
are legible, nothing looks dated, the camera says something, no unsupported claim
is on screen, and the end card can be read.

**Fails open.** A render that could not be reviewed still ships.

---

## What decides whether it earns anything

- **Hook rate** = 3-second views ÷ impressions. 25% baseline, 30% good, 35% scalable.
- **Hold rate** = 15-second views ÷ 3-second views. 40–50% average, 60%+ strong.
  Two ads with the same hook rate can differ several times over in return because
  one holds to the CTA and the other empties at second six.
- Published benchmarks disagree badly; treat any single number as directional.

Rules that follow, and are enforced rather than suggested: product on screen
inside two seconds, the opening must move, no brand name in the opening line,
one idea per scene, written for sound off, an offer in the last third, and
nothing claimed that the product does not do.

---

## Still by hand

- **Choosing the four corners** for `put_screen_on()`. Needs a person's eye, or a
  detector.
- **Filming UGC.** A real person, once. `shoot_list()` says what to ask for.
- **Lip-sync redubs.** VEED at $0.07/sec on fal — funded, not yet wired.

## Costs, roughly

| | |
|---|---|
| still (flux/dev) | ~$0.01 |
| upscale | ~$0.01 |
| 5s clip (Wan 2.2) | ~$0.25 |
| 5s clip (Veo 3.1 Lite) | ~$0.25 |
| voice, per line | fractions of a cent |
| bed (ACE-Step, 30s) | ~$0.01 |
| lip-sync redub | $0.07/sec |

A 25-second ad with two generated clips and a bed comes in around **$0.60**.
