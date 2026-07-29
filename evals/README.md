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
| `expected_llm` | Optional per-scope override of `expected` for the **LLM path**, used where core's Assist API drops the intent (`GetCurrentTime`, `GetState`, `GetWeather`, …) in favor of a general tool (`GetDateTime`, `GetLiveContext`). Same shape as `expected`. When absent, both scopes score against `expected`. `expected_for(llm=...)` picks the right one. An empty `tools: []` is a deliberate "no tool, unjudgeable" (e.g. nevermind), distinct from omitting the field. |
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

## Two scopes, one corpus

Every case runs at two scopes (`evaluation.md`'s Scope knob): the **local** path (core's
HASSIL agent) and the **LLM** path (the agent under test). The `local` cases run through the
LLM too, not as redundant coverage but to measure where the LLM path costs more than local.
Concretely: core's Assist API drops six of the intents these cases use (`GetCurrentTime`,
`GetCurrentDate`, `GetState`, `GetWeather`, `GetTemperature`, `Nevermind`) in favor of the
general `GetDateTime` / `GetLiveContext` tools. So the model reaches the same answer, but
often through an extra tool round-trip that HASSIL answers in zero, which is exactly the
generations/tokens/latency delta the scorecard tracks and the case *for* `prefer_local` ON.
`expected_llm` encodes those diverging expectations; everything else scores against
`expected` in both scopes.

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

## What is measured vs. still predicted

The **routing labels are now measured**, keyless. `test_routing.py` runs every utterance
through core's local (HASSIL) agent with the fixture home exposed and confirms: all 17
`local` cases are recognized (a sentence template matches), and none of the 8 `llm` cases
resolve locally. So `routing_truth` is no longer a template-existence guess; it is a live
result.

The **`expected` actions are still predictions.** Nothing has run against a live model, so
every `expected.tools` / `expected.answer` and every `resolves_at_wave0: true` on an `llm`
case remains a hypothesis until the live baseline (below) runs.

Findings the measurement surfaced:

- The harness runs a minimal core (no per-domain platforms), so most device intents
  *recognize* but cannot *execute*; recognition, not execution, is what the `local` check
  asserts (see `routing.py`).
- Two `llm` cases (`compound-two-devices`, `knowledge-capital-france`) do not cleanly
  no-match: HASSIL *false-matches* a catch-all template and then fails with no valid target.
  They still do not resolve locally, so the `llm` label holds, but for a subtler reason than
  "no template."

## Status

- Corpus: seeded (`corpus/wave0_golden_set.yaml`).
- Scorer + scorecard: done, deterministic (`harness/scoring.py`).
- HASSIL-routing measurement: done, keyless, 25 cases (`harness/routing.py`, `world.py`).
- Instrumentation: done. `MagicMicChatLog` records a `GenerationRecord` per model round
  (tokens, cache read/creation), populated by the provider and read from the conversation
  trace. Serves the runner and live/prod debug tracing alike
  (`custom_components/magic_mic/chat_log.py`).
- Runner: done (`harness/runner.py`). `observe_turn` drives an agent and reduces the turn to
  an `ObservedTurn` from the trace (tool calls + generations) and the result (speech,
  resolution); `run_case` scores it against the scope's expectation. Verified against the
  mocked stream for text answers, tool calls, generation counting, and wrong-action buckets
  (`tests/components/magic_mic/test_runner.py`).

Everything keyless is in place. The remaining step is the live baseline, which needs a key.

### Wave 0 exit gate (blocks Wave 1)

**Stand up a Claude API key + run the live baseline** — stock full-roster prompt,
`prefer_local` OFF. This is the one step that needs a key and cannot be faked: scoring a
mocked response would be scoring fabricated output. It is the *final* Wave 0 task, deferred
until the runner and scorer are built keyless. Wave 1 reports Δtokens / Δturns / Δhassil-rate
**against this baseline**, so no Wave 1 result exists until it runs. Do not carry it into
Wave 1.
