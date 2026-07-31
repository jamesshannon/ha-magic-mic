# Deployed-Use Telemetry

> Product-outcome measurement for the proving ground: do the VISION moments occur, complete,
> recover, and become useful in real homes? This is distinct from deterministic tests,
> corpus-based LLM evaluation, and per-run debugging traces.

---

## TL;DR

The flagship scenario metrics are **deployed-user telemetry**, not corpus measurements.
A corpus can prove that a scripted reminder, learning, memory, music, or explainability
interaction succeeds under known conditions. It cannot tell us:

- whether people actually attempt it;
- whether it works across the diversity of real homes;
- whether they accept, ignore, correct, cancel, or reuse it;
- whether a “magical” behavior saves friction over days and weeks;
- whether our population assumptions about rooms, sessions, and installed tools are true.

Collect those answers locally first. Any fleet upload is **off by default, explicitly
opt-in, disclosed, content-free, locally aggregated, bounded-cardinality, and erasable**.
Never upload utterances, transcripts, audio, entity names/IDs, reminder or memory content,
tool arguments, calendar data, user IDs, or aliases.

Telemetry validates and prioritizes after deployment. It does not replace correctness tests
or become an excuse to ship unsafe/incomplete behavior.

---

## Four measurement layers

| Layer | Question | Example | Where it belongs |
|---|---|---|---|
| **Deterministic test** | Does exact machinery obey its contract? | One scheduled occurrence survives restart and does not silently disappear | pytest / CI |
| **Corpus evaluation** | Given a controlled utterance and home, does the model produce an acceptable action/answer? | “Remind me in an hour if…” compiles the expected trigger/condition/body | [`evaluation.md`](evaluation.md) |
| **Trace** | What happened on this one real interaction? | Tool retrieval, model generations, execution, delivery result, latency | local conversation/pipeline trace |
| **Product telemetry** | Across real use, is the interaction attempted, completed, recovered, and reused? | Accepted learned aliases reduce later repair turns | this document |

The same instrumentation may feed more than one layer, but the aggregation and claim differ.
Do not call a production frequency or retention metric a corpus acceptance criterion.

### What corpus evaluation still contributes

Each VISION moment needs scripted positive, negative, ambiguity, and failure cases. Those
cases establish functional correctness before deployment. Field telemetry answers the
separate ecological-validity question: whether the scenario and its assumptions survive
uncontrolled devices, integrations, acoustics, languages, habits, and time.

---

## Privacy and governance invariants

### Two tiers

1. **Local product counters** live on the HA instance. They can be shown in diagnostics,
   exported by the user, and used during development without leaving the home.
2. **Fleet telemetry** is an optional upload of coarse local aggregates. It belongs only in
   the proving-ground component and/or a cloud service such as Nabu Casa—not inside reusable
   capability logic or an assumed core feature.

### Consent

- Default fleet upload: **off**.
- Separate, plain-language opt-in; never bundled with enabling memory, web access, or the
  conversation agent.
- Show exactly which fields are sent and the most recent payload.
- Support immediate disable, local reset, server-side deletion where an install identifier
  exists, and schema/version disclosure.
- A configuration change must not retroactively upload previously collected raw events.

### Forbidden data

Never collect or upload:

- audio, wake-word buffers, or STT transcripts;
- user utterances or assistant response text;
- tool arguments or raw tool results;
- entity/device/area names or IDs;
- calendar, todo, reminder, memory, media-title, web, or notification content;
- aliases or their source/target phrases;
- HA user IDs, speaker embeddings, identity labels, or voice confidence samples;
- exact location, IP-derived location, or exact event timestamps;
- arbitrary exception strings or integration payloads.

### Allowed shapes

Prefer:

- versioned enum event names;
- coarse capability ID from a fixed allowlist;
- outcome/reason enums;
- booleans;
- bounded integer counts;
- coarse duration/token/tool-count/home-size buckets;
- locally computed ratios and histograms;
- coarse model/provider families only when necessary to interpret quality.

No free-form telemetry strings. Enforce a cardinality budget in schema tests.

### Local aggregation before upload

Raw lifecycle events may be useful locally for debugging, but fleet payloads should contain
daily/weekly aggregates, not event streams. Local aggregation can compute relationships such
as “an accepted learning fix was reused successfully within 30 days” without uploading the
phrase, entity, user, or exact times.

Prefer no stable cross-upload identifier. If longitudinal install cohorts prove essential,
use a separately disclosed, rotating pseudonymous install ID with the shortest useful
rotation/retention window. Do not introduce it merely because retention dashboards are
convenient.

### Operational controls

- schema version on every aggregate;
- sampling and upload rate limits;
- local and server retention limits;
- kill switch for a bad schema;
- minimum cohort/count thresholds before reporting rare combinations;
- automated redaction/allowlist tests;
- telemetry code must fail closed and never affect assistant behavior.

---

## Common event model

Feature code emits a small local semantic event after a meaningful state transition:

```text
ProductEvent {
  schema_version
  capability: fixed enum
  interaction_stage: fixed enum
  outcome: fixed enum
  reason: optional fixed enum
  origin: wake_word | continuation | text | deferred
  scope_class: household | personal | unrestricted   # never user identity
  latency_bucket
  generation_count_bucket
  tool_count_bucket
  recovery_used: boolean
}
```

Not every event needs every field. Exact timestamps, record IDs, principal IDs, and
capability payloads remain local and are excluded from fleet aggregates.

Events should arise from deterministic state transitions where possible—not from another
model guessing whether the user was satisfied.

### Outcome vocabulary

Use shared enums where the semantics match:

- `attempted`
- `completed`
- `declined`
- `clarified`
- `corrected`
- `cancelled`
- `expired`
- `unavailable`
- `failed`
- `queued`
- `recovered`

Capability-specific reason enums may refine these, but remain fixed and low-cardinality.

---

## VISION-moment telemetry

These are product signals to examine after each feature has passed its deterministic and
corpus gates.

### 1. Spoken conditional automation

Measure:

- authoring attempts;
- compile success, clarification, decline, and validation failure rates;
- trigger type and bounded body class (enum only);
- created → fired / expired / cancelled distribution;
- condition true versus false at evaluation;
- fire-time completion, partial failure, and unavailable-target outcomes;
- time from creation to cancellation/correction bucket;
- repeated authoring after a successful first use.

Interpretation:

- High compile success alone does not prove value; most items expiring/cancelling without a
  useful evaluation may mean the interaction is confusing or over-created.
- A condition evaluating false is not failure—it may mean the rule correctly prevented an
  interruption.

Never upload trigger entities, condition values, action arguments, or spoken rule text.

### 2. Reminder reaches the user

Measure:

- reminder/alarm creation completion and cancellation;
- due occurrences;
- first-target delivery success, busy/defer, escalation, and queue rates;
- pull-to-read acknowledgement, dismiss, snooze, expiry, and still-unacknowledged outcomes;
- delivery-to-ack latency buckets;
- reminders surfaced after downtime;
- correlation failures when “read it” finds zero or multiple eligible items;
- repeated reminder use after the first delivered item.

Interpretation:

- “Announcement service call succeeded” is not the flagship success. The stronger outcome
  is delivered → acknowledged/read, with escalation and queue behavior visible.
- An ignored reminder is not necessarily a model failure, but persistent high ignore or
  queue rates should drive delivery/UX investigation.

Never upload reminder content, target area/device, scheduled timestamp, or user identity.

### 3. Learns how the household talks

Measure:

- eligible friction signals;
- offers shown, accepted, declined, suppressed, and expired;
- accepted fix type (entity alias / command alias / other enum);
- reuse of an accepted fix;
- later resolution without clarification;
- later repair/misroute after applying the fix;
- manual edit/delete of a learned fix;
- offer frequency per interaction bucket.

Primary outcome:

> Among accepted fixes that are reused, does later friction decrease without increasing
> unrelated misroutes?

Offer acceptance alone is not success. It may measure politeness or curiosity.

Never upload alias phrases, expansions, entity references, or repair utterances. Link reuse
locally and upload only aggregated counts.

### 4. Household/personal memory

Measure:

- remember/recall/forget/overwrite attempts and outcomes;
- slot match, ambiguous match, and no-match rates;
- recall after prior write;
- correction/overwrite shortly after recall;
- expiry cleanup and explicit deletion;
- denied scope/policy outcomes by coarse scope class;
- repeat use across locally computed time buckets.

Interpretation:

- Raw recall volume does not establish correctness. A recall followed quickly by correction,
  overwrite, or forget is a stronger negative proxy.
- No-match is often the honest result and should not be optimized away into hallucination.

Never upload memory content, slot keys, TTL timestamps, or person identifiers.

### 5. Continued conversation

Measure:

- responses that invite continuation;
- actual wake-word-free follow-up arrival;
- follow-up classified intentional/spurious/uncertain;
- continuation actions staged for confirmation;
- correction/cancel/rephrase immediately after a continuation action;
- optional no-tools classifier use and added latency;
- session turn-count and inter-turn-gap buckets;
- explicit stop and timeout endings.

Interpretation:

- Follow-up rate and session-length distribution validate whether keeping the mic open is
  useful.
- A correction soon after an action is only a **proxy** for a false accept; do not label it
  ground truth without explicit user feedback.

Never upload transcript/audio, speaker signal, or the action arguments.

### 6. “What's playing?” follow-up

Measure:

- what's-playing attempts with zero, one, or multiple plausible active players;
- metadata complete/partial/absent;
- follow-up knowledge questions after a successful identification;
- model-knowledge versus web-grounded answer path;
- correction/re-query after the answer;
- backend capability family and unavailable-operation outcomes;
- playback correction success for “other version” / “next.”

Never upload title, artist, album, station, player/entity, or search query.

### 7. “Why did that happen?”

Measure:

- explain-state attempts;
- `attributed | partially_attributed | unattributable`;
- cause class (`assistant | automation | script | user | device/external | unknown`);
- recorder unavailable/excluded/purged outcomes;
- entity/time ambiguity and clarification;
- repeated/rephrased why-question after an answer;
- narration/evaluation disagreement found by sampled opt-in testing, if ever performed.

Interpretation:

- A high unattributable rate may reveal missing HA context propagation, not poor narration.
- Never optimize attribution rate by allowing the model to guess.

Never upload entity, automation/script, actor, state value, service data, or narrated answer.

### 8. Capability selection

Measure:

- catalog size, filtered count, selected count, and prompt-token buckets;
- discovery fallback and effect-free expanded-retry rate;
- selected tool absent/miss signals;
- unavailable-hint outcomes;
- dependency-expansion failures;
- task correction after a selection miss;
- distribution relative to the 128-tool ceiling.

Corpus shadow mode remains the pre-enforcement gate
([`capability-selection.md`](capability-selection.md)). Field telemetry detects long-tail
homes, providers, languages, and paraphrases the corpus missed.

---

## Denominators and interpretation

Every rate must name its denominator:

- offer acceptance / offers shown, not all turns;
- reminder acknowledgement / due deliverable occurrences, not created reminders;
- discovery fallback / LLM turns with selectable capabilities;
- learned-fix improvement / accepted fixes that were later reused;
- explainability attribution / valid explain-state requests with recorder available.

Report counts alongside rates and suppress small cohorts. Avoid composite “magic scores”:
they conceal whether a change improved adoption, completion, reliability, or merely shifted
the denominator.

Telemetry generally provides **correlation and product signals**, not causal proof.
Feature flags or staged rollouts can support comparisons, but a real experiment needs a
predeclared hypothesis, guardrails, and sufficient sample size. Do not casually A/B test
safety, privacy, or delivery guarantees.

---

## Rollout

### Phase 1 — local only

- Implement semantic counters/histograms behind a versioned allowlist.
- Expose them in diagnostics and test with synthetic events.
- Verify forbidden data cannot enter the payload.
- Use the project's own installs for design feedback.

### Phase 2 — opt-in proving-ground aggregate

- Add explicit consent and payload preview.
- Upload coarse periodic aggregates with no raw event stream.
- Publish schema and retention.
- Start with reliability/cost metrics; add longitudinal product metrics only when the local
  aggregation and privacy need are clear.

### Phase 3 — decision use

- Tie each dashboard to a documented design assumption or product question.
- Define review dates and thresholds before collecting indefinitely.
- Remove metrics that no longer inform a decision.
- Never make fleet availability a runtime dependency.

---

## Build-time checklist

- [ ] Define the fixed event/reason enum schema and cardinality limits.
- [ ] Map each emitted event to a deterministic state transition.
- [ ] Implement local aggregation and diagnostics before network upload.
- [ ] Add automated forbidden-field/free-form-string checks.
- [ ] Add explicit opt-in, payload preview, reset, retention, and deletion behavior.
- [ ] Document denominators and the decision each metric informs.
- [ ] Test offline behavior: telemetry failure cannot affect the assistant.
- [ ] Review schema changes as privacy/security changes.

This checklist is not a pre-Wave-1 capability blocker. Add local instrumentation alongside
the feature state machines; fleet upload is a separate, later product decision.

---

## Related docs

- [`evaluation.md`](evaluation.md) — corpus evaluation, deterministic tests, and per-run
  traces.
- [`prompt-context.md`](prompt-context.md) — prompt/cache assumptions whose population
  distributions motivated the first fleet metrics.
- [`capability-selection.md`](capability-selection.md) — shadow evaluation before selection
  enforcement and long-tail field signals afterward.
- [`security.md`](security.md) — privacy, identity, provenance, and opt-in configuration.
- [`conversation-loop.md`](conversation-loop.md) — continuation outcomes and false-accept
  proxies.
- [`scheduling-model.md`](scheduling-model.md) — durable delivery lifecycle that emits
  reminder outcome events.
- [`learning.md`](learning.md) — accepted-fix reuse and friction-reduction outcome.
