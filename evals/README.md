# Evals (Tier-B golden set)

The offline, corpus-driven evaluation harness. This is the **probabilistic LLM-behavior
tier** from [`docs/evaluation.md`](../docs/evaluation.md) Parts D–E, distinct from the
deterministic, CI-blocking unit tests under `tests/` (Part G). Its job is to run a fixed
corpus of utterances through the agent, score the actions and answers, and report the
value-dashboard scorecard so every later change is a *measured delta*.

Nothing here is destined for HA core. The thin, convention-pure pieces (the corpus format,
the scorer, trace enrichment) can graduate upstream later; the harness that drives them is a
project-local dev tool with no core-merge constraint. See "Reuse plan" below.

## Layout

```
evals/
  README.md                     this file
  corpus/
    wave0_golden_set.yaml        the seed cases + the fixture "home" they run against
```

The runner (pytest-driven, first step) lands next to this once the corpus stabilizes.

## What the corpus is

A `world` (a small fixture home: areas + exposed entities) plus a flat list of `cases`.
Each case is a single-turn `(utterance [, context] -> expected action(s) / answer)` per
`docs/evaluation.md` Part E. Multi-turn trajectories and a user-simulator come later.

### Case fields

| Field | Meaning |
|---|---|
| `id` | Stable kebab-case identifier. |
| `utterance` | Exactly what the user says. |
| `category` | Grouping for the scorecard (device-control, query, timer, media, list, small-talk, compound, implicit, knowledge). |
| `routing_truth` | Ground-truth label for the **local-vs-LLM split**: `local` = a built-in HASSIL template covers it (given the referenced entities are exposed); `llm` = no HASSIL template, only the model resolves it. This is the label; the runner measures where the utterance *actually* routes and the scorecard compares the two. |
| `resolves_at_wave0` | Whether Wave 0 (pass-through proxy, no capabilities yet) can produce the right outcome at all. `false` cases are VISION features not built yet; at baseline they land in the "don't understand" bucket on purpose, so the scorecard's starting distribution is honest. |
| `requires` | Fixture entities/features the case depends on (documents the context assumption; the runner must expose these). |
| `expected.tools` | Ordered list of `{name, args}` the correct outcome invokes. `args` are partial hints, not an exact-match contract. Omit for answer-only cases. |
| `expected.answer` | Optional predicate over the spoken response: `{contains: [...]}` or `{regex: ...}`. |
| `template` | For `local` cases, the HASSIL source template the utterance exercises (provenance). |
| `note` | Why the case earns its place. |

### `routing_truth` was grounded in the real dictionaries

The `local` cases were checked against the installed `home_assistant_intents` sentence
templates (`en.json`, the same data HASSIL matches on), not guessed. The `llm` cases are
utterances with **no** matching template that the model is *predicted* to resolve with tools
that exist today (compound commands, implicit intent, general knowledge). Per the plan, these
are deliberately hard to find, which is itself the point: it shows how little the LLM path is
expected to add *over* HASSIL at Wave 0, before any capability lands. "Predicted" is load-
bearing here, see the hypotheses note below.

## Scope notes worth remembering

- Slot-bearing `local` cases (turn off *the kitchen light*) only match when the named entity
  or area is exposed. That is why they carry `requires`; the runner builds the `world` first.
- `HassGetState` ("is the garage door open?") has a HASSIL template, so `routing_truth:
  local`, but §2.9 puts `GET_STATE` in the LLM-defer set. Baseline runs `prefer_local` OFF,
  so it routes to the LLM regardless; the split metric is where this shows up.
- Time/date/state answers depend on the fixture clock and states. Answer predicates stay
  loose (a shape, not a literal) so they survive a changing fixture.

## Reuse plan

Two runners, one corpus:

1. **Now — hand-rolled pytest runner.** Same shape as the Tier-A tests, reuses the streaming
   mock, produces the baseline immediately. First step.
2. **Later — a heavy, reused runner** (likely a fork of, or contribution to,
   [`ha-voiceagent-llm-benchmark`](https://github.com/Drizzt321/ha-voiceagent-llm-benchmark),
   or a DeepEval-shaped harness) for LLM-as-judge helpfulness, multi-turn user-simulation,
   and trace views. It lives **in this project** as a dev tool, so "reuse vs. build" is not a
   core-merge question.

The corpus format is the portable contract both consume. Keep it declarative (data, not
code) so either runner can read it. See [`docs/evaluation.md`](../docs/evaluation.md) Part H.

## Corpus labels are hypotheses, not measurements

Nothing here has been run against a live model. The `local` cases were checked against the
sentence *templates* in `home_assistant_intents/en.json` (a template exists), but not against
a live HASSIL match with the fixture entities exposed. Every `expected` action and every
`resolves_at_wave0: true` on an `llm` case is a **prediction** of model behavior, not an
observation. The runner's job is to test these labels; the gap between labelled truth and
measured behavior is itself a finding.

## Status

- Corpus: seeded (`corpus/wave0_golden_set.yaml`), labels unverified (see above).
- Scorer + scorecard: pending.
- Mock-driven runner (plumbing + scoring, keyless): pending.
- HASSIL-routing measurement (verifies the `local` labels, keyless): pending.

### Wave 0 exit gate (blocks Wave 1)

**Stand up a Claude API key + run the live baseline** — stock full-roster prompt,
`prefer_local` OFF. This is the one step that needs a key and cannot be faked: scoring a
mocked response would be scoring fabricated output. It is the *final* Wave 0 task, deferred
until the runner and scorer are built keyless. Wave 1 reports Δtokens / Δturns / Δhassil-rate
**against this baseline**, so no Wave 1 result exists until it runs. Do not carry it into
Wave 1.
