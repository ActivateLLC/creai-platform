# The game division

A strategy and an agent design, built from what the research says actually
breaks games — not what we assumed did.

Written 2026-09-19. Read `docs/agents.md` first for how specialists are shaped.

---

## What the data says

The numbers, from 2026 industry analyses of the indie market:

- **70% of indie games fail.** Not from quality — from never being found.
- **17,889 games shipped on Steam in 2025.** Roughly half earned fewer than ten
  reviews. Median lifetime revenue: **$570.** "Under 50 reviews, you don't
  exist" is the working rule.
- **Discoverability is the single biggest pain point** in every survey, every
  year, by a wide margin. Ahead of engine, ahead of art, ahead of money.
- **Marketing is a different skill from making**, and most developers don't
  have it. "Build it and they will come" is described, verbatim, as "a death
  sentence."
- **What works:** five-to-fifteen-second vertical clips of one satisfying
  mechanic. YouTube Shorts for algorithmic stability. Discord as the retention
  layer. Micro-influencers at 1–10K viewers. Wishlists cultivated *before*
  launch, not after. The developer visibly present in the community.
- **The "AI slop" stigma is real.** Steam requires AI disclosure. Games that
  look generated get review-bombed; games with a visible human behind them
  get the benefit of the doubt.
- **Burnout and isolation** are named as the second-order killer: one person
  doing programming, art, design, sound and marketing alone.

And our own data, from one customer:

> "It didn't work. The graphics are horrible. It looked nothing like it was
> supposed to." — asked for realistic characters, got dots.

---

## The insight

Every competitor makes games. **Nobody makes games that find their players.**

Rosebud, Bezi, the Unity and Unreal AI tools — all of them stop at the build.
The developer is handed a game and left alone with the part that kills 70% of
them. Creai already has the other half: an ad pipeline that writes, directs and
cuts video from a brand; a post queue for nine networks; a hypothesis engine;
a critic; an approval queue. None of it is pointed at games.

So the division's promise is not "make a game." It is:

> **A game that finds its players.**

Built to be good, packaged for the stores, and launched with the campaign that
every indie developer knows they need and can't do alone — the mechanic clips,
the Shorts, the wishlist page, the store listing, the community — all waiting
in a queue for their yes.

That is the moat. Fidelity is table stakes; Unity closes that gap. Discovery is
where nobody else is.

---

## What "surpass competitors" actually requires

Three things, in order of how hard they are to copy:

1. **The game is genuinely good and genuinely what was asked for.** Unity for
   fidelity; a vision loop so the agent *sees* what it built; an honest ceiling
   so nobody gets dots when they asked for people. Competitors can match this.

2. **It ships to the stores under the customer's name**, review-ready. Icons at
   every size, screenshots per device, listing copy, privacy labels, age
   rating, and a pre-check against Apple's 4.2 (minimum functionality) and 4.3
   (spam / template) rejections. Competitors mostly don't do this.

3. **It launches with a campaign.** Mechanic clips cut by the ad pipeline, a
   Shorts cadence, a wishlist and pre-launch page, a Discord, a "build in
   public" feed with the developer visibly in it. **No competitor does this.**
   It's the whole reason 70% fail, and we already own the machinery.

---

## The agents

Same shape as everywhere else: one orchestrator holding the loop, specialists
called once each. Provider chosen per job by the evidence in `docs/agents.md`.

```
                    ┌────────────────────────────────────┐
                    │  GAME ORCHESTRATOR · claude-fable-5-1 │
                    │  writes C# + scenes, builds, fixes    │
                    └───────────────┬────────────────────┘
        ┌──────────┬───────────┬────┴─────┬───────────┬──────────┬─────────┐
        ▼          ▼           ▼          ▼           ▼          ▼         ▼
    concept    design      art        playtester   store      launch    community
    fable      director    director   (vision)     readiness  (growth)  (support)
               fable       gemini     gpt-6-astra  fable      existing  fable
                                                              pipeline
```

### 1. Concept · `gameconcept.py` · Fable

**Job:** before a line of code, the game's hypothesis — the same discipline the
ad pipeline already has. Who plays it, the one mechanic, the hook in one
sentence, the thirty-second experience, **and why this and not the 17,889
others.** Returns a confidence level.

**Refuses:** a clone. If the concept is "Flappy Bird but blue," it says so and
proposes the twist that makes it a reason to play.

**Why Fable:** judgement over speed. Written once.

### 2. Design director · already in `GAME_EXTRA` + `genres.py` · Fable

**Job:** the loop, the numbers, feel before features, the difficulty curve,
the genre rules. Already built tonight. Moves from a brief into a callable
specialist so the orchestrator gets a *design document* it builds against,
not just guidance it may or may not follow.

### 3. Art director · `gameart.py` · Gemini

**Job:** the visual bible for one game, single-shot. Palette (three colours,
one accent), style, the light direction every sprite shares, the construction
rules, the reference feel. For Unity: material and lighting direction. Returns
a document the orchestrator builds to, so every asset in one game looks like
it belongs to that game.

**Why Gemini:** the research is consistent — "a better eye for modern visual
trends," "visually polished output fast." This is the one job it's best at.
It never touches a file; the orchestrator does.

### 4. Playtester · `playtest.py` · gpt-6-astra (vision)

**Job:** the missing piece, and the one that would have caught the dots.
Runs the built game, captures frames at the start screen, ten seconds in, on
a death, on the game-over — and answers three questions with the *original
request* in hand:

- Does this look like what was asked for? (dots ≠ realistic characters)
- Does it work? (input answered, no stuck states, no broken text)
- Is the thirty-second experience the one the concept promised?

Returns `ship` / `fix` with specifics. **Fails open** like the critic.

**Why vision:** "a blind agent fails constantly at 3D work." Every source
says the differentiator isn't tool count, it's whether the agent can see what
it did. This is `look` for games, which sites have had all along and games
never did.

### 5. Store readiness · `storeready.py` · Fable

**Job:** everything a submission needs that nothing here produces yet, plus a
review pre-check.

- Icons at every required size, from the game's own art
- Screenshots per device class, captured from the running build
- Listing copy: title, subtitle, description, keywords — in the customer's
  voice, from the concept
- Privacy labels, age rating, content descriptors
- **The 4.2 / 4.3 check:** is there enough here to be an app, and does it
  look like anything else we've shipped? A game that fails this isn't
  submitted; it's sent back with the reason.

Submitted under **the customer's** developer account, via their App Store
Connect / Play Console API keys — the same principle as domains in their name
and payments into their Stripe. Creai never publishes under its own.

### 6. Launch · the existing ad pipeline, pointed at games

**Job:** the campaign. This is mostly wiring, because the machinery exists:

- `hypothesis` → what the game argues
- `producer` → 5–15s **mechanic clips** — one satisfying moment each, the
  format the research says works. Not trailers. Clips.
- `cutter` → cut unattended, captions figure-first, sign-off with the store link
- `admutate` → variants across hooks and lengths, rolled up by axis
- `draft_posts` → the Shorts cadence, queued for approval
- a wishlist / pre-launch page, built by the site agent from the concept

Plus two things that don't exist and should: a **Discord scaffold** (server,
channels, the first pinned post) and a **"build in public" feed** — the log
lines the agent already narrates ("added a second enemy type", "tightened the
jump") turned into short posts with a frame attached, so the developer is
visibly present without writing anything. That directly answers the AI-slop
stigma: a human, in public, making a thing over time.

### 7. Community · `gamesupport.py` · Fable

**Job:** after launch. Drafts replies to store reviews (never sent without a
yes), turns crash reports and one-star patterns into a fix list for the
orchestrator, writes patch notes from the diff. Closes the loop between
players and the next build.

---

## What each agent must never do

- **Concept** never approves a clone by silence.
- **Art director** never writes a file.
- **Playtester** never blocks a build by being unavailable.
- **Store readiness** never submits under Creai's account, and never submits
  something the 4.3 check flagged.
- **Launch** never posts, publishes or spends without a tap.
- **Community** never sends a reply the person hasn't seen.

---

## Sequence

Not all at once. In the order that de-risks the most:

1. **Playtester** — build it now, on the current engine. It catches the exact
   failure a customer reported, and it's engine-independent: it works on
   Godot today and Unity tomorrow. *(Built tonight; see `playtest.py`.)*
2. **Unity build pipeline** — the fidelity fix. Linux headless for Android and
   WebGL; a hosted Mac for iOS.
3. **Concept + art director** — the two specialists that make each game a
   game rather than a template. Both are prompt work on existing shapes.
4. **Store readiness** — the 4.2/4.3 check first, the asset generation second.
5. **Launch wiring** — point the ad pipeline at games. Mechanic clips first.
6. **Community** — after there's a launched game to support.

---

## What would make this fail

Honestly named, so they can be watched for:

- **The AI-slop stigma.** If games look generated, they get review-bombed
  regardless of quality. The build-in-public feed and the customer's own
  voice in the listing are the defence. A game with no human visibly behind
  it is a game the market has decided to distrust.
- **Apple 4.3.** A platform that ships many games invites the "spam" rejection.
  Every game must be distinct in concept, art and name. The concept agent's
  clone refusal and the store check are the guard, and they have to hold.
- **Cost per game.** Unity builds, a Mac host, vision calls on every playtest,
  ad renders, store assets. Each game costs real money to make and launch.
  Price it as a product line, not a feature of the free tier.
- **Promising what the engine can't do.** The dots happened because nobody
  said "I can't make that here." The ceiling statement in `GAME_EXTRA` is the
  fix; it has to survive every rewrite of that brief.
