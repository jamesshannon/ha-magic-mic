# Speaker Identification (Voice-ID)

> Feature doc. Identifying *who is speaking* so the assistant can scope per-user
> data (memory, calendar, reminders). The model is the easy part; core adoption
> is hard for reasons that are mostly architectural and security-related.
> Feeds the `resolve_user()` seam in [`../PRODUCT_PLAN.md`](../PRODUCT_PLAN.md)
> §5.1; deferred to Phase 4.

---

## TL;DR

- **The model is easy and community-proven.** Lightweight open-source speaker
  embeddings (resemblyzer, ECAPA-TDNN, pyannote, wespeaker) run fine on HA
  hardware. The community has already shipped integrations that do this.
- **Core has none of it.** No speaker-ID in the pipeline (fixed 4-stage enum:
  `WAKE_WORD/STT/INTENT/TTS`), and `ConversationInput` carries no speaker field.
- **There has been no formal attempt to add it to core** — no PR, no
  architecture-repo ADR. Only an *unanswered* org discussion + community PoCs.
- **Auth vs. personalization is a spectrum, not a binary.** Voice-ID is a
  *medium-assurance* signal: fine for reading a user's own soft data (with
  consent + confidence threshold + fallback), never sufficient for
  high-consequence actions or HA permission escalation.
- **"Confidence" is a tuned cosine/PLDA score, not a model probability** — see
  [Confidence scores](#confidence-scores-how-they-actually-work).
- **Adaptive enrollment is a valid idea with a sharp catch** — see
  [Adaptive enrollment](#adaptive-enrollment-youll-have-this-idea-again). Tracking
  drift (esp. aging) and template *poisoning* are the **same mechanism**; prefer
  supervised re-anchor. (Written down because it's an easy idea to re-invent.)

---

## State in core (verified)

- No `speaker-id` / `voiceprint` / `enrollment` references anywhere in
  `assist_pipeline`, `conversation`, or `assist_satellite`.
- Pipeline stages are a fixed enum — `PipelineStage.{WAKE_WORD,STT,INTENT,TTS}`
  (`assist_pipeline/pipeline.py:480`). No slot for an identification stage.
- `ConversationInput` exposes `context.user_id` (the pipeline *owner*, not the
  speaker), `device_id`, `satellite_id` — **no speaker identity**
  (`conversation/models.py:22`).

## State outside core

- **No core PR** for "speaker recognition" / "voice identification".
- **Nothing in the `home-assistant/architecture` repo** (the only "speaker" issue
  there, #364, is multi-room *audio* speaker groups — unrelated).
- The only venue is org **Discussion #527** ("Voice / Speaker Recognition") — and
  it is **unanswered by maintainers**. Participants are community members sharing
  PoCs (EuleMitKeule's resemblyzer integration; mitrokun's experimental Wyoming
  intermediate server; a VoiceBM project).
- So the status is one step *earlier* than "proposed and rejected": nobody has
  driven it into the formal process, and there is no core-team champion.

---

## Why it hasn't been tackled in core (ordered by load-bearing-ness)

1. **The payoff needs a per-user personalization layer that doesn't exist —
   chicken-and-egg.** SID yields a `user_id`, but core has nothing that consumes
   it (no per-user exposed entities / memory / calendar scoping). Core SID would
   identify a speaker and then do nothing. The valuable half (personalization) is
   the bigger lift and isn't built, so the identification half has no reason to
   ship alone.
2. **It's a security landmine, and HA is security-first.**
   - Voice is **spoofable** (play a recording). If SID maps to an HA *user*, it
     becomes an **auth bypass** — a guest/recording inherits permissions.
   - **Misattribution is a privacy leak** ("what's on *my* calendar" answered for
     the wrong person). For a privacy-focused project, an occasionally-leaky
     identity feature is *worse* than none.
3. **Real-world accuracy ≪ benchmarks.** Models score well on clean ~5 s audio;
   far-field, noisy, 1–2 s commands ("turn off the lights") with similar-sounding
   family members push error rates up. Core's bar is turnkey robustness for
   non-technical users; a flaky *identity* feature doesn't clear it.
4. **Enrollment is the real product, and it's fiddly.** The embedding match is
   trivial; the enrollment/onboarding/management UX (record samples per person,
   store profiles, map to Persons, handle drift + re-enrollment) is a whole
   surface that must be turnkey in core.
5. **Architecturally it wants a new pluggable provider + pipeline stage.** Like
   `stt`/`tts`/`wake_word`, SID should be its own provider domain — which is why
   community requests frame it as a **Wyoming-protocol extension** (needs
   Nabu Casa / Mike Hansen buy-in). A new stage + entity platform + wire protocol
   + where-it-runs (satellite vs server) is non-trivial and unslotted.
6. **Priorities / small team.** The voice team has been focused on the base
   pipeline (speech-to-phrase, streaming TTS, Voice PE hardware, LLM
   integration). SID is a personalization nicety below "make the core pipeline
   excellent"; the community fills the gap.

### "But HA has multi-user administration"

True, but it's the *wrong kind* of multi-user — and that's part of the problem.
HA's user model is an **authentication** construct (logins, tokens, admin flags,
entity/dashboard access). Voice attribution is a **personalization** signal that
must be kept *away* from that auth construct (reason 2). Person entities are
about presence/location, not voice. So the gap isn't "HA doesn't know about
users" — it's that (a) the voice path can't attribute an utterance to anyone, and
(b) the user model HA *does* have is auth-shaped, exactly the shape you don't want
SID wired into. Mapping SID → *Persons for personalization* is fine; mapping SID →
*users for permissions* is the trap.

---

## Auth vs. personalization: a graduated-assurance model

The clean "personalization, never auth" dichotomy is **wrong** — Google Voice
Match gates personal-data *reads* (calendar, reminders, messages) on voice
recognition, which is an access boundary for that data. The accurate model is a
spectrum where required assurance scales with the **consequence of being wrong**:

| Tier | Examples | Voice-ID sufficient? | Mitigations |
|---|---|---|---|
| 1. Preferences | playlist, address-by-name | Yes | low stakes |
| 2. Personal-data **reads** | calendar, reminders, messages | **Yes — the boundary** | enrollment **consent** + **confidence threshold** + **graceful fallback** ("I don't recognize your voice" → generic/decline); error is *reversible* |
| 3. High-consequence actions | purchases, unlock, payments | **No** | step-up: voice PIN / app confirm / blocked |

Voice-ID is a **medium-assurance** signal. Even Google's Tier-2 is opt-in and has
documented failure modes (leaking to guests) — accepted risk with mitigations,
not solved. Shipping Tier-2 turnkey at HA's privacy bar is exactly the hard part.

### Our invariant (refined)

> Resolved `user_id` from voice-ID scopes our **soft per-user data** — memory,
> calendar/reminder *reads*, personalization — gated by a **confidence threshold**
> with an **unknown-speaker fallback** to the household/default user, and only
> after explicit **enrollment consent**. It **never** grants HA permissions,
> **never** actuates devices by virtue of identity, and **never** gates
> high-consequence/irreversible actions — those require a separate step-up.

**HA-specific bright line:** voice-ID must never touch HA's permission /
entity-control system. HA already has a separate, speaker-independent gate for
"what can voice do at all" — the **exposed-entities** config. Hard device control
is bounded by exposure regardless of who's speaking; voice-ID only ever decides
*whose soft data* to surface. That confines voice-ID to Tiers 1–2 by construction
and keeps Tier-3 safe.

This **exposure-is-the-hard-bound** invariant is the *same* one [`security.md`](security.md)
leans on against prompt injection: no input — a spoofed voice *or* an injected instruction
from web/calendar content — can exceed the exposed-tool envelope. Identity and content are
two attack surfaces onto one bound.

---

## How it fits our design

- **It's an *input* to `resolve_user()`** (§5.1), not a new subsystem. The
  community-proven pattern is exactly this: wrap the STT/conversation entity,
  compute a speaker embedding, match against enrolled profiles, and enrich the
  `user_id` in the conversation context. That is our `resolve_user()` seam with a
  higher-priority branch.
- **Resolution order becomes:** (1) confident voice-ID match → that user; (2)
  `context.user_id` if it maps to a real Person; (3) configured device→owner; (4)
  `"default"` household user. Low-confidence voice-ID falls through to (2)–(4)
  rather than guessing (same ambiguity-guard discipline as `find_entities`).
- **Why it's more tractable for us than for core:** we're *building* the
  personalization layer it feeds (escapes reason 1), and we can guarantee
  personalization-not-auth by keeping `user_id` a scoping key for our per-user
  data that never touches HA permissions (sidesteps reason 2 by construction).
- **What we'd still own:** an enrollment UX, and honest low-confidence handling
  (fallback to household/default, never guess).

Deferred to **Phase 4**. The `resolve_user()` seam and user-keyed store land in
Phase 0 so voice-ID drops in later with no data migration.

---

## Confidence scores: how they actually work

Local speaker models (resemblyzer, ECAPA-TDNN, x-vectors, SpeechBrain, wespeaker,
pyannote) **do not output "speaker = Alice, confidence 0.87."** They output a
fixed-length **embedding vector** (~192–256 dims). Everything else you build:

1. **Model → embedding** for the utterance.
2. **You → similarity**: compare against each enrolled speaker's profile (a
   centroid/set of their enrollment embeddings) via **cosine similarity** (~0–1),
   or PLDA log-likelihood in x-vector systems.
3. **You → decision**: `argmax` = probable ID; that speaker's score = "confidence."

**The score is uncalibrated — not a probability.** Two consequences:

- **The cutoff must be tuned per deployment** (mic, room, enrollment quality) to
  an equal-error-rate point. Too high → false rejects (real user → fallback); too
  low → false accepts (wrong person → privacy leak, the dangerous direction).
- **Use a two-part gate**, not one number:
  - *Absolute threshold* — "is this anyone we know?" (open-set / unknown-speaker).
  - *Top-1 vs top-2 margin* — "is it unambiguously this person vs a similar-voiced
    other?" (closed-set ambiguity). Same discipline as hassil's `MIN_DIFF_SCORE`
    and our `find_entities` ambiguity guard.
  - **Match only if `best ≥ threshold` AND `(best − second) ≥ margin`; else →
    unknown → household/default user. Never guess.**

Calibration options if you want the number to *mean* something: **PLDA** (more
calibrated than raw cosine); **Platt/logistic score calibration** on a dev set.
**Do not use softmax as the unknown-speaker gate** — it's closed-set and sums to
1, so a stranger still gets high "probability" for the nearest enrolled person.

Reliability degrades exactly where HA voice lives — short (1–2 s), far-field,
noisy utterances + thin enrollment produce noisier, less separable scores. So the
margin guard and unknown-fallback aren't niceties; they're the safety mechanism.

---

## Adaptive enrollment (you'll have this idea again — read this first)

> Written deliberately as a "you will re-invent this" note. The idea is **valid
> and studied**, but it has one catch that reframes the whole thing.

**The idea (as you'll re-derive it):** use high-confidence matches to add new
embeddings to a speaker's profile, expanding coverage of conditions the 2–3
enrollment samples missed; expire outliers over time. A high score (>0.9)
*correlates* with "drifted a bit along some axis (distance / noise / age) but not
a lot," so those samples are the safe ones to chain on. The killer case is
**gradual, directional drift — especially aging**: age-20 → age-50 is a single
0.7 match (fails), but 20→25→30→…→50 in ~0.95 hops **tracks a moving target you
could never bridge in one jump**.

**It's real and named:** *adaptive / incremental enrollment*; in biometrics,
*template update / self-update*. The aging problem is *template aging*, and
incremental update is its recognized mitigation. So the instinct is correct —
this is the canonical motivation, and aging is its cleanest case.

**The catch that reframes everything — template poisoning:**
- False accepts at a high threshold are rare but **permanent and compounding**.
  The impostors that clear 0.9 are, by selection, the ones most acoustically
  similar to you — the most damaging ones. Each admitted embedding pulls the
  profile toward the confusable region, making the next false accept likelier.
  A positive-feedback loop; adversarially, a **poisoning attack** (worse on a
  spoofable modality).
- **Tracking power ≡ poisoning vulnerability — the same property.** The mechanism
  that follows your voice to age 50 is identical to the one that lets an adversary
  walk the profile toward themselves in 0.95 hops. The score is **direction-blind**
  — it can't tell "legit trajectory toward future-you" from "adversarial walk,"
  because it doesn't know future-you.

**The tension aging exposes (kills the naive safeguard):** "keep immutable
original anchors" (the obvious anti-poisoning move) *directly conflicts* with
drift-tracking — pin the age-20 anchor and it rejects the legitimately-aged
age-50 you. You cannot both pin to the original *and* track a target that walks
far from it.

**What actually rescues the legit case — frequency asymmetry:** the real user is
**high-frequency** (many sessions); an attacker is **low-frequency**. Legit drift
is supported by a dense stream that continuously re-centers the profile; a
poisoner must move the centroid against that stream. So **conservative,
rate-limited adaptation** lets volume win — and this is *especially* favorable for
aging, which is slow enough that a very low adaptation rate suffices.

**The clean resolution (and why vendors chose it):** a periodic **supervised
re-anchor** ("please redo Voice Match") jumps the profile to the *confirmed*
current you — tracking arbitrary long-horizon drift **without** opening the
feedback loop. The real design axis is **supervised vs. unsupervised**, and
Google/Alexa chose supervised. Aging is the best explanation for *why supervised
re-enrollment exists* — it's not a failure to track aging, it's tracking it via
the one mechanism that stays safe.

**Safe recipe if we do the unsupervised version:**
1. Separate, stricter adaptation threshold (e.g. 0.9 vs 0.8 access) — necessary,
   not sufficient.
2. Quality-gate candidates (high SNR, sufficient duration).
3. Keep a **bounded, aging template *set*** — not a single running average
   (averaging destroys the multi-condition benefit you're chasing).
4. **Rate-limit** adaptation (leans on frequency asymmetry).
5. Occasional **supervised re-anchor** for long-horizon drift.

**Why it's more defensible for us than for Google:** aging is the ideal case
(low-stakes Tier-2 data, legit-user-dominant, slow → low adaptation rate), and by
our invariant voice-ID **never touches Tier-3 / HA permissions** — so a poisoned
profile can, worst case, surface the wrong person's reminders, never actuate
anything. That bounded blast radius is what makes cautious unsupervised adaptation
legitimate for us where it isn't for a system gating purchases. Potential
differentiator: "adapts to your actual acoustic environment," precisely because
we've fenced off the dangerous tier.

---

## Sources

- [Voice / Speaker Recognition — HA Discussion #527](https://github.com/orgs/home-assistant/discussions/527) (unanswered by maintainers)
- [`EuleMitKeule/speaker-recognition` (GitHub)](https://github.com/EuleMitKeule/speaker-recognition)
- [Speaker recognition in Voice Assistant — HA Community feature request](https://community.home-assistant.io/t/speaker-recognition-in-voice-assistant/654276)
