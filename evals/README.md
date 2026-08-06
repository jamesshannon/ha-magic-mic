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
    wave1_fuzzy_fallback.yaml    spoken-name device cases for the match-layer fuzzy fallback
    resolution/seed.yaml         the resolver micro-benchmark (model-free, see below)
  harness/                       corpus loader, scorer, routing measurement, runner
    baseline.py                  the live-baseline entry point (needs a key)
    local_first.py               the faithful prefer-local routing driver (key for fallback)
    fuzzy_fallback.py            the find_entities Consumer 1 driver (summary on, names off)
    console.py                   interactive CLI to hand-drive turns (needs a key)
    backing.py                   real executable entities for the fixture home
    resolution.py                the resolver micro-benchmark loader + runner + scorecard
  results/
    wave0_baseline.json          the recorded live baseline Wave 1 measures against
    wave1_fuzzy_fallback.json    the recorded fuzzy-fallback (Consumer 1) run
```

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
| `resolves_at_wave0` | Whether Wave 0 (pass-through proxy, no capabilities yet) can produce a judgeable successful outcome. A `false` case is a VISION feature not built yet and cannot declare a success predicate. An error lands in `unresolved`; any successful-looking response lands in `unjudged`, never in a success bucket. |
| `requires` | Fixture entities/features the case depends on (documents the context assumption; the runner must expose these). |
| `satellite_area` | Optional world area key (e.g. `den`) placing the requesting satellite for this case. The room is context: it decides where a bare name resolves and whether an echoed area scopes away the target, so one corpus can pair a same-room and a different-room variant. Read by `harness/fuzzy_fallback.py`; omitted cases use the run's default placement. |
| `provider_options` | Optional provider setup for this case. `web_search` and `web_fetch` are independent booleans and both default off when omitted. The live runner applies changes through the Magic Mic config entry before the turn and records the effective values in the artifact. |
| `expected.tools` | Ordered list of required `{name, args}` calls. Named argument values match exactly after Unicode, case, and whitespace normalization; unspecified observed arguments are allowed. Every undeclared extra call fails. |
| `expected.supporting_tools` | Optional call patterns that may appear around the required calls without being required themselves. Use for a deliberate read-before-answer step such as `GetLiveContext`, not as a broad tool allowlist. |
| `expected.effects` | Ordered durable or external effects that must be observed in addition to the tool call, written as `{kind, data}`. Named data values use the same exact normalized matching as tool arguments; extra effects fail. |
| `expected.answer` | Optional predicate over the spoken response: `{contains: [...]}` or `{regex: ...}`. |
| `expect_changes` | `entity_id -> {state, attributes}` the turn must leave the world in. When present, the case is **state-scored**: correctness is the resulting state, not the tool called (`harness/statediff.py`), so it needs no `expected_llm` or `any_of`. Declared entities are checked on state plus each named attribute; other exposed entities are checked on state plus a small domain-specific set of reproducible action attributes. Undeclared new entities and removed exposed entities fail. Requires the executable world (`backing.py`). |
| `permitted_tools` | Required for a state-scored case. Lists every call pattern allowed during the turn, including alternate mutation tools and deliberate supporting reads. A correct final state still fails if any observed call falls outside this roster. |
| `setup` | `entity_id -> {state, attributes}` to stage **before** the turn, so a change is real (turn on a light that starts on, open a garage that starts open). Same shape as `expect_changes`. |
| `ignore_changes` | `entity_id -> [attribute, ...]` checks to suppress; the literal `state` drops that entity's state check, for a genuinely non-deterministic outcome. |
| `any_of` | Either `expected` or `expected_llm` may be `{any_of: [<outcome>, ...]}` instead of a single `{tools, answer}` block. The case is correct when the turn matches **any** listed outcome. This is for genuine ties the model picks between run to run (close a cover by `HassTurnOff` or by `HassSetPosition: 0`), so non-determinism does not flip-flop pass/fail. Only for equally-valid outcomes, never accepted failures. **For device-control ties, prefer `expect_changes`**: judging the state absorbs the tie without enumerating tools. |
| `expected_llm` | Optional per-scope override of `expected` for the **LLM path**, used where core's Assist API drops the intent (`GetCurrentTime`, `GetState`, `GetWeather`, …) in favor of a general tool (`GetDateTime`, `GetLiveContext`), or where the LLM grounds a target by name where the local template uses an area slot. Same shape as `expected` (single outcome or `any_of`). When absent, both scopes score against `expected`. `expected_for(llm=...)` picks the right one. An empty `tools: []` is a deliberate "no tool, unjudgeable" (e.g. nevermind), distinct from omitting the field. |
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

## Two planned scopes, one corpus

The corpus defines expectations for two scopes (`evaluation.md`'s Scope knob): the **local**
path (core's HASSIL agent) and the **LLM** path (the agent under test). The current live
baseline and variant artifacts run only the LLM path with `prefer_local` off. The keyless
routing measurement probes HASSIL separately; it does not execute the combined user path or
prove fallback behavior. `harness/local_first.py` runs the actual HASSIL→LLM path faithfully
(strict recognize + HA's CONTROL fallback filter, then the model only on a miss or deferred
intent), which is the driver `prefer_local_intents` needs before it becomes an acceptance
gate. It reports the off-cloud rate as the real routed-locally count and judges correctness
only where the local path allows it (world diff, spoken answer, or a no-arg intent name),
marking the arg-bearing remainder UNJUDGED rather than guessing.

The `local` cases also run through the LLM, not as redundant coverage but to measure where
the LLM path costs more than local. Concretely: core's Assist API drops six of the intents
these cases use (`GetCurrentTime`,
`GetCurrentDate`, `GetState`, `GetWeather`, `GetTemperature`, `Nevermind`) in favor of the
general `GetDateTime` / `GetLiveContext` tools. So the model reaches the same answer, but
often through an extra tool round-trip that HASSIL answers in zero, which is exactly the
generations/tokens/latency delta the scorecard tracks and the case *for* `prefer_local` ON.
`expected_llm` encodes those diverging expectations; everything else scores against
`expected` in both scopes.

**State-scored cases sidestep the scope split.** A case with `expect_changes` is judged by
the world it leaves, which is identical whether HASSIL or the LLM acted and whichever
equally-valid tool got there. So it carries no `expected_llm` and no `any_of`; the nine
device-control cases migrated to `expect_changes` dropped exactly that machinery (see
[`docs/evaluation.md`](../docs/evaluation.md) Part H). `set-bedroom-brightness` joined them on
2026-08-05: the percentage is exactly what a state check reads, since 30% is brightness 76 of
255 in the attributes, and the pair of tool expectations it carried disagreed on the grounding
slot (`area` locally, `name` for the model). A case with a genuinely loose end state
(`implicit-cold` raising a setpoint by an unspecified amount) stays tool-scored on purpose.

## Scope notes worth remembering

- Slot-bearing `local` cases (turn off *the kitchen light*) only match when the named entity
  or area is exposed. That is why they carry `requires`; the runner builds the `world` first.
- `HassGetState` ("is the garage door open?") has a HASSIL template, so `routing_truth:
  local`, but §2.9 puts `GET_STATE` in the LLM-defer set. Baseline runs `prefer_local` OFF,
  so it routes to the LLM regardless; the split metric is where this shows up.
- Time/date/state answers depend on the fixture clock and states. Answer predicates stay
  loose (a shape, not a literal) so they survive a changing fixture.

## Resolver micro-benchmark (model-free)

`corpus/resolution/seed.yaml` + `harness/resolution.py` are a separate, deterministic tier
from everything above: they measure the fuzzy entity **scorer** in isolation
([`docs/find-entities.md`](../docs/find-entities.md), [`docs/evaluation.md`](../docs/evaluation.md)
Part G), with no LLM and no Home Assistant world. The scorer sees only each candidate's
descriptive phrases (names, aliases, area, floor), so a case is plain data: a `query` against a
named `home`, plus the expected guard outcome (`resolves_to` one entity / `ambiguous` shortlist
/ `none`). Structured filtering, state, and exposure are `async_match_targets`' job and are not
scored here; the tool-level integration that exercises them lives in
`tests/.../test_find_entities.py`.

That purity is the point: cases run in microseconds, so the set can scale to thousands and A/B
two scorers cheaply. A `home` *is* the candidate set the scorer is handed (what
`async_match_targets` already returned), so it stands in for a regime: a whole small house, or
the set left after an area filter (the corpus an IDF-weighted scorer learns term weights from).
A richer scorer drops in behind the `Resolver` seam without touching the corpus.

```
.venv/bin/python -m evals.harness.resolution     # print the scorecard
```

The scorecard reports **false-resolve rate** (decisively acting on the wrong entity, the one
unsafe failure), **decisive accuracy**, **resolve recall** (target returned at all, even if
only shortlisted), ambiguous / none handling, and a **per-regime breakdown** keyed on each
case's `tags`. `harness/test_resolution.py` is the CI-blocking gate: false-resolve rate stays
at zero, decisive accuracy holds at or above its recorded floor, and the guardrail regimes
(`decisive`, `small-home`, `none`, `ambiguous`) stay perfect. The per-regime split is how a
scorer change is judged safe: `shared-word` and `area-filtered` cases (a common token like
"light" dilutes the discriminator) are the tuning target for IDF weighting and are allowed to
be imperfect; `small-home` cases are the counterweight (too few entities to estimate term
rarity, so IDF must not turn cautious there). None of the misses is ever a false resolve.

## Interactive console (manual tracing)

`harness/console.py` is the live-tracing sibling of the offline corpus runner: the terminal
equivalent of Home Assistant's "chat with the assistant" box plus its debug-trace tool, for
hand-testing during a wave. It stands up the same headless HA and fixture world the corpus
uses, then lets you type utterances and inspect the whole turn. It needs a live key (read
from the environment or a project-root `.env`, same as `baseline.py`).

```
.venv/bin/python -m evals.harness.console                       # interactive REPL
.venv/bin/python -m evals.harness.console --skip-hassil         # start LLM-only
.venv/bin/python -m evals.harness.console --web-search -u "what happened today?"
.venv/bin/python -m evals.harness.console -u "turn on the kitchen light" -u "now off it"
```

Each turn shows, per model round, the **durable** system prompt (the `cache_control`-marked
block, printed once since it is cached and unchanged across rounds) and the **volatile**
message list as it grows, plus the tools sent, the tool calls made (with full inputs and
results), durable fixture effects, the token/cache cost, and the spoken answer. Every round
is timed (including the HASSIL probe), so you can see where a turn spends its latency. The
composed prompt is not on the conversation trace, so the console captures it harness-side by
wrapping the provider client's `messages.create` for the turn; nothing in
`custom_components/` changes.

The turn is issued from a voice **satellite** placed in a room (default the living room), so a
bare "turn on the lights" resolves to that room the way a real satellite would: HASSIL injects
the room as `preferred_area_id`, and the LLM's `IntentTool` fills the same slot from the
device. `:here <room>` moves the satellite between the fixture's rooms (`:here nowhere` clears
it, `--here` sets the start), and `:world` prints the fixture as a table ordered by area with
the satellite in its room.

Two knobs, both live-toggleable, are the Scope and agent selectors from
[`docs/evaluation.md`](../docs/evaluation.md) Part E:

- **Scope** (`:hassil on|off`, or start with `--skip-hassil`): the full hassil→LLM path
  probes the local HASSIL agent first and, if it resolves, stops there (the `prefer_local`
  win, no LLM call); LLM-only skips straight to the model.
- **Agent** (`:agent baseline|testbed`, or `--agent`): the stock provider agent vs the Magic
  Mic proxy.
- **Provider web tools** (`--web-search`, `--web-fetch`): enable Claude's native tools for
  the console session. Both are off when omitted, matching a corpus case with no
  `provider_options`.

Unlike the corpus runner, the fixture world is **not** reset between turns: state
accumulates, so "turn it on" then "now turn it off" behave, and a follow-up ("no, I meant the
bedroom") works because the conversation carries a stable id across turns. `:reset` restores
the world; `:new` starts a fresh conversation; `:here` moves the satellite; `:world` prints
the room-ordered table; `:req` dumps the last turn's full requests; `:help` lists every
command.

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

**Prior art we are borrowing from.**
[home-assistant-datasets](https://github.com/allenporter/home-assistant-datasets) is the
closest existing HA eval framework. Two adoptions are recorded in
[`docs/evaluation.md`](../docs/evaluation.md) Part H: **state-diff scoring** (adopt now, a
complementary correctness signal for device-control cases that removes the tool-name
brittleness `any_of` currently patches) and **`synthetic_home`** as the fixture format
(deferred, a dependency-bearing swap for `backing.py`). It scores the LLM agent only, so it
has no local-vs-LLM routing split; that scorecard is the part unique to this harness.

## What is measured vs. still predicted

The **routing labels are now measured**, keyless. `test_routing.py` runs every utterance
through core's local (HASSIL) agent with the fixture home exposed and confirms: all 17
`local` cases are recognized (a sentence template matches), and none of the 8 `llm` cases
resolve locally. So `routing_truth` is no longer a template-existence guess; it is a live
result.

The **`expected` actions are now measured for the LLM scope.** The live baseline
(`harness/baseline.py`) ran the corpus through the real model, and the run falsified four
predicted `expected.tools`: the model correctly set brightness, cover position, and volume
and added a list item, but through `HassLightSet`, `HassSetPosition`, `HassSetVolume`, and
`HassListAddItem`, not the tools the corpus guessed. Those four score as `wrong action`
against the current corpus until the expectations are reconciled; the model's spoken
confirmations show the action itself was right.

Findings the keyless routing measurement surfaced:

- The routing harness runs a minimal core (no per-domain platforms), so most device intents
  *recognize* but cannot *execute*; recognition, not execution, is what the `local` check
  asserts (see `routing.py`). The live baseline does not share this limitation: it backs the
  fixture with real platforms (see `backing.py`).
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
- Executable fixture: done (`harness/backing.py`). The live baseline registers each corpus
  entity on its real domain platform (`light`, `switch`, `fan`, `cover`, `climate`,
  `media_player`, `todo`), so the domain's services and Assist intents both load and the full
  tool roster is exposed and executes. State-only fallback domains such as `weather` still
  receive registry identity, corpus attributes, area and device-class metadata, Assist
  exposure, and complete reset behavior. Without it, tool calls hit missing services, the model
  retried, and per-turn generation counts were inflated. Timers are device-scoped, not
  entity-scoped: `register_satellite` stands up the voice satellite (a registry-backed device
  the turn carries) with a no-op timer handler so `HassStartTimer` is exposed and runs. Being
  registry-backed, the satellite can hold a room, so a bare "turn on the lights" resolves to
  its area; the baseline leaves it area-less (scores unchanged), while the console places it.
- Live baseline: done (`harness/baseline.py`, run recorded in `results/wave0_baseline.json`).
  Runs the full corpus by default; `--case ID`, `--category CAT`, and `--routing local|llm`
  select a subset for a cheap re-run after a targeted fix. A subset never overwrites the
  locked baseline (it prints, or writes to `--out`); `--list` shows the selection without a
  key.
- Interactive console: done (`harness/console.py`). Hand-drive utterances against the fixture
  world and inspect the turn (durable/volatile prompt split, tools, tool calls with full
  results, durable effects, per-round timing, cost, answer), with live HASSIL/agent toggles,
  a room-placed satellite (`:here`) for area-context testing, and multi-turn conversation
  continuity. See "Interactive console" above.

### Wave 0 exit gate (blocks Wave 1): done

Ran the live baseline keyed from a project-root `.env`: stock full-roster prompt,
`prefer_local` OFF, model `claude-haiku-4-5`, 25 cases. Rescored result through R23: 15
resolved-by-LLM-correct, 4 wrong-action, 6 unjudged, 0 unresolved; routing agreement 8/25
(every case routes to the LLM with
`prefer_local` OFF, so only the 8 `llm`-labelled cases agree); 44 generations. Wave 1 reports
Δtokens / Δturns / Δhassil-rate against this artifact.

> **Refreshed under state scoring (keyed re-run, `claude-haiku-4-5`).** After nine
> device-control cases moved to `expect_changes`, the keyed baseline was re-run so its
> scoring basis matches. R20 through R23 later rescored the stored observations as 15
> LLM-correct / 4 wrong-action / 6 unjudged / 0 unresolved, still 44 generations. State
> scoring agrees with
> the reconciled tool expectations on this corpus while being robust to tool variance (the
> device cases pass whichever equally-valid tool the model picks). The three wrong cases are
> model behavior, not harness faults: `turn-off-all-lights` and `implicit-too-dark` ask which
> entity instead of acting (the world does not change, so state scoring marks them wrong for
> the right reason); `implicit-cold` reads the thermostat then asks how warm. The fourth
> stored wrong result, `weather`, came from the R22 fixture defect and needs a keyed rerun.
> The three unbuilt VISION cases have no success predicate and cannot raise task success.
> `nevermind` is also unjudged on the LLM path because a plain acknowledgement has no
> deterministic success predicate. `start-timer` and `add-shopping-item` are unjudged only
> because this historical artifact predates durable-effect telemetry; a keyed rerun can
> prove their effects.

This is the **pre-magic roster**. Cases without `provider_options` run with `web_search` and
`web_fetch` off. A search-specific case can enable either provider tool without changing the
rest of the corpus, and the artifact records the effective options per case. The runner also
pins unspecified provider defaults off, preserving this reference if shipped defaults ever
change.

### Local-first routing (2026-08-04, `claude-haiku-4-5`, living_room satellite)

First live run of `harness/local_first.py` (the faithful prefer-local path: strict recognize
plus HA's CONTROL fallback filter, model only on a miss or deferred intent). Routing:
**14/25 off-cloud** (local wins, no model call), 1 deferred by CONTROL (`is-garage-open`,
`HassGetState`), 9 local miss to the LLM; routing agreement 22/25. Scorecard: 10 resolved
locally and correct, 6 LLM correct, 3 wrong-action, 6 unjudged. Latency over the 11 model
turns: TTFT p50 634ms / p95 2673ms, round duration p50 3.06s / p95 5.88s.

One local win diverged, which is the finding the gate exists to surface. `turn off the
lights` from a living_room satellite resolves locally to that room (`HassTurnOff`,
area-preferred), leaving `light.kitchen` on, so the world diff marks it wrong against the
whole-home expectation (both kitchen and living-room lights off). **Decision (2026-08-04):
room-scoped is the intended semantics for a bare `turn off the lights` from a room-bound
satellite; HASSIL is right and the corpus case encodes the wrong (whole-home) expectation.**
The corpus case `turn-off-all-lights` needs a room-scoped rewrite (deferred: its expectation
is entangled with the area-less baseline, which has no room to scope to, so the two drivers
may need different expectations). The distinct `turn off *all* the lights` phrasing is
genuinely ambiguous and is TBD later. The other two wrong-action cases (`implicit-cold`,
`implicit-too-dark`) are LLM-routed model behavior, not routing. The three arg-bearing local wins
(`set-bedroom-brightness`, `start-timer`, `add-shopping-item`) are UNJUDGED: they executed
locally but the local path exposes no arg schema to verify against. **Superseded (2026-08-05),
and this artifact predates it:** the driver now judges an arg-bearing local win from a
declared durable effect, which `start-timer` (`timer.started`, seconds) and
`add-shopping-item` (`todo.item_created`, summary) both produce on the local path, and
`set-bedroom-brightness` moved to state scoring. All three resolve on the next run, leaving no
UNJUDGED local win in the corpus. `weather` routed to the
LLM only because the fixture has no weather integration (recognized `HassGetWeather`, no
handler); a real home with the integration resolves it locally, so the true off-cloud rate
is a lower bound here.

**Verdict (2026-08-05): recommend `prefer_local_intents` on** (PRODUCT_PLAN §2.9, README
install step 6). None of the three routing disagreements is HASSIL taking a turn it should
not have, so this run found no instance of the false-positive pre-emption that was the one
argument against. Cross-reading the same case ids against `wave0_baseline.json` (which records
`prefer_local: false`) shows the 14 off-cloud turns also resolve on the model path: 11 correct,
1 wrong in both arms (`turn-off-all-lights`), 3 unjudged. That cross-read is manual; do it at
each wave close.

### Fuzzy fallback / find_entities Consumer 1 (2026-08-04, `claude-haiku-4-5`, hallway satellite)

Live run of `harness/fuzzy_fallback.py` over `corpus/wave1_fuzzy_fallback.yaml`: device names
deliberately more formal than the spoken phrasing ("Corner Floor Lamp" vs "the floor lamp"),
driven with the entity summary on and name injection off so the model never sees the exact
roster and must resolve a spoken name. Each case sets the room the request comes from
(`satellite_area`), so the corpus pairs same-room and different-room variants and puts two
"reading light"s in different rooms. **7/7 correct, zero wrong-target, zero echoed rooms.**
"turn on the floor lamp" resolved the den lamp from a device-less hallway and from the den;
"turn on the reading light" resolved the living-room one from the living room (the room
tiebreak) and asked from the hallway (no room to break the tie); "the reading light in the
bedroom" honored the spoken room; "under cabinet lights" went through `find_entities`; "close
the office shade" resolved by area + device-class slots. Latency over the 7 model turns: TTFT
p50 1.28s / p95 2.61s, round duration p50 7.03s / p95 7.68s
(`results/wave1_fuzzy_fallback.json`).

An earlier pass exposed a real bug, now fixed. With the entity summary on, the model tended to
echo its satellite's room onto a name-bearing call ("the user is in the living room, I'll add
the area"), and the fallback originally honored that as a hard filter, so a cross-room named
device came back not-found. That is wrong: HA core matches a name house-wide and treats the
room only as a soft `preferred_area_id` (verified against the installed core: a unique name
resolves from another room; only a genuinely spoken area scopes). The fix has two halves: the
fallback matches house-wide and uses the requesting room (from `device_id`) only to break a
genuine ambiguity, honoring a spoken area as a hard scope; and the system prompt tells the
model to pass an area only when the user names a location. The run now flags any turn that
echoes its own room, so a regression in either half shows up as an echo count and, on a
cross-room case, a wrong-target failure. This pass echoed on none.

Run it (every case reaches the model; there is no local shortcut):

```
.venv/bin/python -m evals.harness.fuzzy_fallback                       # full corpus, writes the artifact
.venv/bin/python -m evals.harness.fuzzy_fallback --case ambiguous-lamp # one case
.venv/bin/python -m evals.harness.fuzzy_fallback --list                # show the selection, no key
```

Config knobs like the web tools, `prefer_local`, and `web_search` `user_location` (off by
default, privacy-first) are eval **axes**, not just shipped defaults to mirror: measure each
one against the baseline with a labelled on/off comparison, using cases that actually
exercise it. Location is not tested at all yet, because no case's correct answer depends on
it (the corpus is location-invariant). It earns a location-on variant only once the corpus
has location-sensitive cases ("what's open near me"), where the point is to measure whether
attaching location improves the answer enough to justify the privacy cost, a decision the
eval informs precisely because it is more than a mirror of defaults.

Three wrong-action cases are model behavior worth recording:
`turn-off-all-lights` and `implicit-too-dark` ask which entity instead of acting;
`implicit-cold` reads the thermostat then asks how warm. The fourth stored wrong result,
`weather`, is the historical R22 fixture failure and needs a keyed rerun.
`conditional-reminder`,
`remember-fact`, and `undo-last` are unbuilt VISION features
(`resolves_at_wave0: false`), so their stored responses are unjudged. The earlier run's
`nevermind` acknowledgement is the fourth unjudged response. The earlier run's other five
wrong-action cases were the timer gap (now backed) and the four reconciled predictions, all
correct here.

Token counts depend on prompt-cache behavior, which varies run to run: this run read
214,656 cached tokens against 20,708 uncached input, where an earlier uncached run showed
190,845 input tokens for the same corpus. Wave 1 should compare `generations` and
`output_tokens` directly and treat input-token deltas as cache-regime-dependent.

Run the full baseline, or a subset:

```
.venv/bin/python -m evals.harness.baseline                     # full corpus, writes the artifact
.venv/bin/python -m evals.harness.baseline --case start-timer  # one case, prints, artifact untouched
.venv/bin/python -m evals.harness.baseline --routing llm       # just the llm-labelled cases
.venv/bin/python -m evals.harness.baseline --list              # show the selection, no key
```

### Model non-determinism

The baseline runs the agent as shipped: extended thinking on (`thinking_effort: low`),
temperature at the API default. Lowering temperature to cut run-to-run variance is not
available here, because Anthropic requires `temperature = 1` whenever thinking is enabled,
and disabling thinking would measure a mode production does not use. So variance is handled
in the corpus instead: `any_of` records the equally-valid resolutions a case flips between,
so a genuine tie does not swing the scorecard. Where a case still flaps for a reason `any_of`
does not cover, that is signal the model is borderline, not a bug to paper over; this tier is
probabilistic and non-gating (`docs/evaluation.md`), so some jitter in the deltas is expected.

The variant runner keeps the usual comparison to one paired pass. It runs each case's arms
back to back, alternates `off→on` and `on→off` by case, and writes every pair's order,
outcomes, and resource deltas to the artifact. Use three trials only for the cases whose
small delta would affect a decision:

```
.venv/bin/python -m evals.harness.variant --case implicit-cold
.venv/bin/python -m evals.harness.variant --case implicit-cold --trials 3
.venv/bin/python -m evals.harness.variant --case implicit-cold --case conditional-reminder --trials 3
```

Routine changes that do not touch model behavior use the deterministic test suite. During
behavioral development, use one paired pass over the affected and adjacent cases. Run the
full corpus once after broad prompt, model, provider-option, tool-roster, routing, or scoring
changes, and when refreshing the locked artifact for a wave or release milestone. Run three
full-corpus trials only for a broad go/no-go decision that a targeted set cannot represent.
A small efficiency claim needs the same direction across the targeted trials with no task
success loss; three observations are not a confidence interval. Any safety failure is
investigated on first occurrence rather than averaged across trials.
