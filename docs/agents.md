# The agents, and what each one is told

An inventory of every model-backed role in the platform: what it does, which
model runs it, why that model, and the shape of its instructions. Written so
that adding a new agent — or reassigning one to a different provider — starts
from what exists rather than from memory.

Last confirmed against the code: 2026-09-19.

---

## The shape of the system

There is **one orchestrator** and **six single-shot specialists**. The
distinction matters more than the count:

- The **orchestrator** runs a tool loop. It reads files, writes files, checks
  its work, looks at the result, builds, reads the build log, fixes, and
  decides when to stop. It holds state across many steps and every step
  depends on the last.
- A **specialist** is called once, with one input, returning one output.
  No tools, no loop, no filesystem. The orchestrator decides what to do with
  what comes back.

This is the line every provider decision follows. The research that justified
adding Gemini was explicit: Gemini is strong at fast, single-shot, visually-led
generation; Claude leads by its widest margin on multi-step, stateful,
tool-calling work — SWE-bench Pro being the single largest divide reported.
So orchestration stays on Claude, and specialists go to whichever model is
best at that one job.

```
                   ┌──────────────────────────────────┐
                   │   ORCHESTRATOR  ·  claude-fable-5-1  │
                   │   agent.py · SYSTEM + one *_EXTRA    │
                   │   holds the tool loop, decides       │
                   └────────────┬─────────────────────┘
                                │ calls, once each, when it needs them
        ┌──────────┬────────────┼────────────┬────────────┬───────────┐
        ▼          ▼            ▼            ▼            ▼           ▼
   hypothesis   producer     critic     shots.judge   voicelead    polish
   gpt-6-astra  gpt-6-astra  gpt-6-astra gpt-6-astra  gpt-4o-mini  gemini-2.5-flash
```

---

## The orchestrator — `agent.py`

**Model:** `claude-fable-5-1` ("best", the default). `claude-sonnet-5` for
chat-only turns and when the person picks "fast". Both Anthropic; both hold a
tool loop reliably.

**Instructions:** one base `SYSTEM` (46 lines) plus exactly one `*_EXTRA`
chosen by project kind. The base sets the working method and the quality bar;
the extra is the specialist knowledge for that kind of thing.

### `SYSTEM` — the base, always present

Five steps it follows every turn: **Understand → Decide → Build → Verify →
Report.** The decisions worth knowing:

- Build on the first message rather than asking questions. A turn that only
  plans is a turn the person paid for and got nothing to look at.
- A "Sign in" button on a site must reach a real app — build the app first,
  never a page advertising a portal that doesn't exist.
- Draw icons itself as SVG path data on a 24-grid. Never an emoji, anywhere.
- Never a placeholder a visitor would read. Never an invented price, review,
  licence or address.
- Look at the result with the `look` tool before calling anything finished —
  spacing, hierarchy and whether the first screen earns the scroll cannot be
  judged from a spec.
- Never spends money, publishes or connects accounts. Those are the person's
  taps; it offers them with `suggest_action`.

### The extras, by project kind

| Extra | Lines | What it adds |
|---|---|---|
| `APP_EXTRA` | 149 | The engineer's brief. Libraries and structure, the four access levels, registers of design, images, and — added tonight — what the **first screen must carry**: the number that changes a decision, state not rows, "what now", shape over time, plausible data. *"A screen that is a heading, a list and an add button is a database with a coat on."* |
| `GAME_EXTRA` | 120 | Godot correctness (tabs, scene headers, export flags, touch) **plus** what makes a game good: feel before features, the first ten seconds teach without telling, difficulty rises one variable at a time, failure fair and fast. Then how to look expensive with no art files — shaders, glow, eased motion, type that varies — and that **SVG is text, so real artwork can be written**. States plainly there is no game server, so networked PvP cannot be built; offers local multiplayer instead. Tells it to call `game_rules` before writing. |
| `VIDEO_EXTRA` | 53 | The director's brief. The three-second gate, holding beats hooking, the first two seconds must move, no brand name in the opening line, the five angles, scenes take their length from the line, captions burned in, generated people may appear but never claim, direct the read beat by beat. |
| `MARKET_EXTRA` | 8 | This project is about marketing an existing business: brand kit, posts, the approval queue. |
| `MARKET_ALSO` | 5 | Marketing is switched on but the site is still being built — do both. |
| `EDIT_EXTRA` | 3 | This is the person's existing site on Webflow; edits are staged, publishing waits. |
| `IMPROVE_EXTRA` | 5 | After a change, may offer up to three honest improvements. |
| `PROJECT_EXTRA` | 2 | Signed in, in their own workspace. |
| `PLAN_EXTRA` / `CHAT_EXTRA` | 2 each | Mode switches: plan without changing anything; talk without building. |

### The tools it can call

Grouped by what they do, since that's what decides which project kinds get them:

**Make and inspect (apps, games)** — `list_files`, `read_file`, `write_files`,
`delete_file`, `check_app`, `check_game`, `build_game`, `look`

**Judgement it borrows** — `game_rules` (genre knowledge before writing),
`use_blueprint` (CRM and content store, built on why the real ones fail),
`plan_video` (scenes schema with conversion rules built in)

**The site** — `update_site`, `generate_image`, `start_app` (offers the
button that turns a site into an app project)

**Marketing** — `read_website`, `save_brand_kit`, `draft_posts`, `revise_post`

**The conversation** — `save_answer`, `suggest_action`, `suggest_improvements`

---

## The specialists

Each is one file, one `SYSTEM`, one model. None has tools. None touches the
filesystem. The orchestrator (or a service acting for it) calls them and
decides what to do with the answer.

### `hypothesis.py` — the creative brain · gpt-6-astra

**Job:** before any ad is written, work out what it should *argue*. Audience,
pain, the insight (which must be arguable — "they want to save time" is a
category, not an insight), emotional arc, promise, proof, objection, CTA,
and a confidence level.

**Why this model:** judgement over speed. Written once, watched thousands of
times; four seconds of thinking is free.

**The discipline in the prompt:** proof is what exists, not what would help.
Given no customer quote and no numbers, it must say what proof is missing
rather than invent it. Marks itself `low` confidence when guessing — a stated
guess is useful, a guess dressed as a finding is not. `check()` rejects
marketing language (*solution, streamline, empower, seamless*) and an
emotional arc that doesn't move.

### `producer.py` — script and shot list · gpt-6-astra

**Job:** write the ad **and direct it**. Per scene: line, who says it, what's
shown, angle, camera move, lens, light, tone, sound, music, caption, seconds,
and one line on why that angle.

**Why this model:** same reason — direction is judgement.

**The rules that are enforced, not suggested** (`check()`): the camera can't
say nothing (one angle throughout), can't open low or end high, product shots
are over-shoulder or macro, the opening must move, one tilt maximum, one tone
across every scene is a voice setting not a read, sound marks a beat rather
than decorating one, the bed lifts once and drops for the last line, captions
on at most half the scenes.

**The angle grammar** it is required to use: `high` shrinks the subject
(the problem), `low` restores them (the payoff), `over` makes the product
theirs, `macro` is where the thing happens, `eye` is neutral. **High on the
problem, low on the payoff, across the same person.**

### `critic.py` — watches the finished file · gpt-6-astra

**Job:** pull six frames, weighted toward the opening, and say what's wrong
with what's actually on screen. The plan can be right and the render wrong.

**Checks, in the order they cost money:** does the first frame move; is every
caption legible; anything dated (a box behind captions, a yellow word, emoji);
does the camera say something; any figure or claim the product can't support;
can the end card be read.

**Fails open.** A render that couldn't be reviewed still ships — it's a second
opinion, not a gate.

### `shots.judge` — one still, before it's animated · gpt-6-astra

**Job:** look at a generated frame before spending on motion (about twenty
times the cost). Rejects wrong hands, distorted faces, garbled text, merged
objects, a subject facing the wrong way, lighting that contradicts the ask.
Does **not** reject on style or composition it merely disagrees with. Returns
a short prompt addition that would avoid the fault, so the retry isn't the
identical request.

**Fails open** the same way.

### `voicelead.py` — a spoken sentence becomes a lead · gpt-4o-mini

**Job:** *"Quoted Reyes Roofing eighteen four for the duplex, chase Thursday"*
→ who, stage, value, next, when.

**Why this model:** latency is the feature. Somebody taps this in a van;
one second versus four is "instant" versus "did it work?" Tested against
`gpt-6-astra`: Astra parsed one edge case better, at 3–5× the latency. Speed
won for this path.

**The discipline:** never invents. No amount said → value stays empty, even
when the model offers one. Didn't catch a name → refuses. Money is parsed the
way contractors speak (*"eighteen four"* = 18,400) in code, not left to the
model, because that failure is silent.

### `polish.py` — a bounded UI/visual pass · gemini-2.5-flash

**Job:** given a description of a screen and optionally its current markup,
suggest concrete visual improvements — layout, spacing, hierarchy, colour,
type, and for games, SVG art direction. Returns discrete suggestions the
orchestrator applies or rejects **individually**.

**Why this model:** the research is consistent that Gemini has "a better eye
for modern UI trends" and produces "visually polished output fast." This is
exactly that job, and nothing else.

**The boundary, enforced by tests:** `tools=False` in the registry, no
fallback chain into an orchestration model, the provider function never
builds a function-calling schema, `polish.py` has no AST-level reference to
`write_files`/`check_app`/`check_game`/`build_game`/`godot`/`appfs`, and
`suggest()` never raises — a polish pass failing is never worth surfacing.

---

## Where Gemini fits, and where it does not

Gemini is a **specialist**, not an orchestrator. The data supports it for
single-shot visual work and does not support it for holding a tool loop. So:

**Already assigned:** `polish` — the UI/visual pass.

**Good candidates to assign next**, all single-shot and visually led:

- **Theme and palette proposal** for a new site or game — "three colours and
  one accent for a noir bakery" — a bounded output the orchestrator then uses.
- **SVG art direction** for game sprites — silhouette first, twenty to sixty
  path commands, consistent light direction. Gemini proposes, the orchestrator
  writes the file.
- **Caption and copy variants** for the mutation engine — high volume, no
  tool loop, cost matters at scale.
- **Still-image prompt drafting** for `shots.make_still` — the prompt is text
  in, text out.

**Not candidates:**

- Anything in `agent.py`'s tool loop. Games, apps, sites — the build/check/
  fix cycle stays on Claude.
- `hypothesis`, `producer`, `critic`, `shots.judge` — these are judgement
  calls, and the research puts Claude and Astra ahead there. Cost isn't the
  constraint on a script written once.
- `voicelead` — latency is the constraint, and Gemini Flash is competitive on
  speed but the parse is already tuned and tested on 4o-mini.

---

## How to add a specialist

Every one in this file follows the same shape. A new one should too:

1. **One file, one `SYSTEM`, one `MODEL`.** The prompt states the job, the
   output format, and the discipline — what it must never invent, when it
   must say it doesn't know.
2. **No tools. No filesystem.** If it needs to change something, it returns a
   suggestion and the orchestrator changes it.
3. **A `check()` for its own output** where rules can be stated. The producer
   and hypothesis both have one; it catches the model's mistakes before
   anything is built on them.
4. **Fails open or fails plainly.** A cosmetic pass returns empty; a required
   step raises a named error the person can act on. Never a generic message
   that sends them to fix the wrong thing.
5. **Tests that enforce the boundary**, not just the happy path. What it
   can't call, what it can't return, what it does when the provider is down.

The provider is the last decision, not the first. Decide the job, decide the
shape, then pick whichever model the evidence says is best at that one thing.
