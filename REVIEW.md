# Code Review Ledger

This is a temporary working document for the review that began after the pre-Wave-1
foundation work. Durable decisions belong in `PRODUCT_PLAN.md` and the design documents.
Delete this file after its findings have been fixed, accepted, or moved into those documents.

## Review method

The review follows request lifecycles and architectural contracts rather than file order.
For each seam, compare the plan, design document, docstring, producers, consumers, Home
Assistant core behavior, and tests. Record a finding only when it has a concrete failure mode
or leaves a claimed boundary unenforced.

Severity means:

- **Foundational:** fix before more behavior depends on the seam.
- **High:** a concrete security, privacy, or request-failure path.
- **Medium:** a correctness or contract defect that can be bounded while the POC proceeds.
- **Low:** tooling, maintainability, or defensive-hardening work.

## Pass 1: runtime spine and foundation contracts

Status: complete. Reviewed the request path from the conversation entity through identity,
ChatLog/session state, prompt construction, tool exposure and execution, pending operations,
effect capture, and undo replay. Compared the provider fork with the checked-out HA Anthropic
integration and ran the repository tests with coverage.

### R1. Web retrieval is enabled by default and sits outside the policy boundary

**Severity:** Foundational security and product-contract violation. Blocks Wave 1 until the
default is corrected and the boundary is described accurately.

**Resolved:** Claude's native `web_search` and `web_fetch` now default off and are exposed
as independent provider options in Magic Mic's POC configuration UI. Enabling either option
directly controls whether the Claude adapter includes that server-side tool. No second
Magic Mic policy gate applies to a provider capability the user enabled. HA-executed tools
remain governed by the documented tool policy.

At review time, `internal/claude/const.py` set both `CONF_WEB_SEARCH` and `CONF_WEB_FETCH` to
`True`.
`ClaudeBaseLLMEntity._get_model_args()` merges those defaults into both the baseline and
testbed agents, then sends Anthropic's server-side tools directly in the provider request.
Those tools do not appear in `chat_log.llm_api.tools`, so `TestbedAPI` cannot filter, classify,
confirm, trace, or journal them.

This conflicts with the source-of-truth product plan and `security.md`, which say external
retrieval ships off as an explicit egress and untrusted-content opt-in. `web-search.md` says
the opposite in several places and is internally inconsistent with its own security
cross-reference. The checked-out HA Anthropic integration defaults both tools off; Magic Mic
changed them to on.

The correction is to keep provider-native configuration in the provider adapter and expose
it through Testbed only because Testbed owns the POC UI. Documentation must distinguish
these provider-executed capabilities from HA tools intercepted by `TestbedAPI`.

### R2. Concurrent turns in one conversation corrupt turn attribution

**Severity:** Foundational contract defect.

**Resolved:** `ToolPolicyContext` now requires the exact request-local `TurnMetadata`.
Exposure traces, execution traces, and delayed effect recording use that reference. The
session sidecar retains metadata by turn ID and no longer exposes a replaceable current-turn
pointer. A regression test pauses turn A's executor, starts turn B, then verifies A's delayed
undo effect remains attributed only to A.

`MagicMicSessionState` stores one mutable `turn_metadata` pointer for the whole conversation.
Every turn replaces it in `async_begin_turn()`. `TestbedAPI._record_effect()` later reads that
shared pointer rather than retaining the `TurnMetadata` created for its request.

HA does not serialize callers that reuse a conversation ID. If turn B starts while a tool
from turn A is awaiting I/O, B replaces `session_state.turn_metadata`. When A completes, its
undo entry receives B's `turn_id`, and its effect is appended to B's metadata. Exposure and
execution traces happen before the delegated await today, but they use the same indirect
lookup and would become vulnerable if recording moved later. The existing effect corruption
already damages auditability and can give future provenance logic the wrong request facts.

Keep conversation-lifetime state in the sidecar, but make current-turn metadata request-local.
At minimum, pass the exact `TurnMetadata` into `ToolPolicyContext` and have all recording use
that object. If metadata needs later lookup, key active/completed turns by turn ID rather than
using one replaceable slot. Add an overlapping-turn test that pauses A's executor, begins B,
then completes A.

### R3. A tool name absent from the advertised inner API bypasses the execution gate

**Severity:** High security-boundary defect.

`TestbedAPI.async_call_tool()` delegates directly to `inner.async_call_tool()` when
`_find_inner_tool()` cannot find the requested name. That path performs no policy resolution,
exposure check, invocation classification, confirmation, trace, or effect capture. HA's base
`APIInstance` would reject the unknown name, but a custom `APIInstance` may implement dynamic
dispatch and accept it.

Preserving a custom inner executor does not require accepting calls outside its declared tool
list. Reject an absent name with a localizable error at the proxy boundary. Add a test using a
custom inner executor that would accept an undeclared name and prove that it is never called.

### R4. Pending-operation conflicts can abort a conversation

**Severity:** High once any confirmation-sensitive policy is enabled; fix before the first
such production tool.

The policy wrapper stages a confirmation with the default `StageConflictPolicy.REJECT`.
`PendingOperationAlreadyStaged` is a plain `Exception`, while HA's ChatLog converts only
`HomeAssistantError` and voluptuous validation failures into tool results. A second
confirmation-sensitive call, including two calls emitted in one model generation, therefore
escapes the tool loop and can fail the entire conversation. With concurrent tool execution,
which operation wins is also an incidental scheduling outcome.

Define deterministic multiple-call behavior before wiring approval. A reasonable v1 rule is
one pending operation selected by model tool-call order, with later confirmation-sensitive
calls returning a localizable conflict result and performing no work. The normal replacement
utterance from the requirements (for example, "No. Turn off the kitchen lights") also needs
an explicit reject-or-supersede transition before it can stage the replacement.

### R5. `UserKeyedStore` does not enforce the identity/scope contract it claims

**Severity:** Foundational data-boundary defect. Fix before the first stored capability.

The store accepts an arbitrary string key for both reads and writes. A capability can read or
replace another user's bucket without presenting a `ResolvedPrincipal` or `DataScope`.
Callers are expected to invoke `ResolvedPrincipal.storage_key()` correctly, but the stated
purpose of the foundation seam is to make incorrect scoping difficult from the first
capability commit.

The store also returns its live internal dictionary and retains the caller's dictionary in
`async_set()`. Either side can mutate data without a corresponding save, producing memory and
disk state that disagree.

Change the public API to accept the resolved principal and requested scope, deriving the key
inside the store. Return and retain owned copies, or replace this generic dictionary wrapper
with capability-specific operations that own mutation and persistence.

### R6. HA prompt rendering can use authorization identity instead of resolved identity

**Severity:** Medium latent identity defect. Fix before configurable prompts or personal
prompt context.

The testbed correctly resolves an unknown voice request to the unidentified principal, but it
passes the original `LLMContext` into `ChatLog.async_provide_llm_data()`. Core independently
derives the prompt-template `user_name` from `llm_context.context.user_id`. For voice, that ID
can be the pipeline owner rather than the speaker.

The current fixed default prompt does not reference `user_name`, so this does not presently
change output. A future configurable prompt can silently address an unknown speaker as the
pipeline owner and encourage the model to infer the wrong personal context. Prompt
personalization needs an explicit resolved-user input while the original HA `Context` remains
unchanged for authorization.

### R7. The immutable JSON boundary relies on annotations instead of validation

**Severity:** Medium contract-hardening defect.

`freeze_json_mapping()` recursively freezes mappings and lists but returns every other leaf
unchanged. Python annotations do not prevent a capability from supplying a mutable custom
object, set, bytes value, non-string mapping key, or non-finite float. Such a value can make a
pending or inverse descriptor mutable, unserializable, or impossible to replay consistently.

Validate the JSON value domain while freezing and reject unsupported keys and leaves. Cover
nested invalid values and aliasing in tests. Pending model arguments are normally decoded
JSON, but undo descriptors are constructed by Python capability code and need the runtime
guard.

### R8. The proxy logs raw tool arguments and results

**Severity:** Medium privacy defect now; high once secrets or personal tools exist.

`TestbedAPI.async_call_tool()` writes complete arguments and results to debug logs. These may
contain memory values, calendar text, door codes, notification bodies, or provider responses.
This also violates the repository rule that logs never contain secrets. HA's current ChatLog
has similar debug and trace behavior, so removing the duplicate proxy log is necessary but
not sufficient for a broad privacy claim.

Log tool name, policy source, timing, and outcome classification without payloads. Before
personal capabilities ship, decide how live HA conversation traces are disclosed, retained,
and redacted; the model-visible ChatLog and diagnostic telemetry have different requirements.

### R9. The provider fork has already drifted behind correctness fixes in HA core

**Severity:** Medium provider-adapter defect.

The local `internal/claude/entity.py` is described as near-upstream and diffable, but its
content conversion lacks current core guards that drop whitespace-only text blocks and empty
messages. Anthropic rejects those payloads. The local diff also mixes the intended generation
trace changes with unrelated older behavior, making future upstream comparison harder.

Rebase the provider copy onto the checked-out core implementation, then reapply only the
documented adapter changes. Add a repeatable provenance note containing the upstream commit
and keep a mechanical diff or update procedure. Tests should cover whitespace-only content
and attachment-only user messages.

### R10. Prompt optimization assumes substitution succeeded

**Severity:** Medium latent configuration defect.

The testbed computes `skeleton_on` from the option and presence of any LLM API. The
substitution helper only replaces the plain Assist API; merged, custom, and already-resolved
APIs pass through unchanged. Tier-2 name injection still runs whenever `skeleton_on` is true,
so those configurations keep their original roster and receive the extra name block despite
the code comment saying that combination must not happen.

Have the substitution return whether it actually replaced the roster, or represent prompt
strategy as an explicit result rather than a boolean option. Gate Tier-2 injection on the
effective prompt strategy.

### R11. The documented bare pytest command collects the reference repositories

**Severity:** Low tooling defect.

`CLAUDE.md` documents `.venv/bin/pytest`, but the checked-out `references/core` tree contains
test-shaped files and its own test suite. A bare run currently fails during collection before
collecting this project's tests. `.venv/bin/pytest tests` runs the intended suite.

Constrain `testpaths` in `pytest.ini` and keep an explicit command for any separately run eval
tests. This prevents agents and CI jobs from reporting environmental reference-tree failures
as project failures.

### R12. Argument-dependent policy classifies unnormalized input

**Severity:** Foundational execution-policy defect.

The policy contract and documentation say `classify_call()` receives normalized arguments,
but `TestbedAPI` passes `tool_input.tool_args` directly before delegating. HA's base
`APIInstance.async_call_tool()` does not apply `tool.parameters`; individual tools validate or
coerce inside their own executors. A policy can therefore classify one representation while
the tool executes another. A string coerced to a number or boolean, an alias normalized by
the tool, or an omitted default can change the operation after the authorization decision.

The execution gateway needs one check/use representation. Normalize and validate once before
policy, then pass those exact arguments to the executor, or give a complex `ToolPolicy` a
tool-owned normalization method whose result is also the invocation payload. Add tests in
which coercion would cross a scope or consequence boundary.

## Pass 2: entity resolution and prompt context

Status: complete. Reviewed the candidate adapter, fuzzy scorer and ambiguity guard,
`find_entities`, taxonomy skeleton, request-conditioned name injection, scorer corpus, and
their HA matcher/exposure dependencies.

### R13. Tokenization drops or fragments non-ASCII languages

**Severity:** High localization defect in a central Wave 1 capability.

Both `fuzzy.py` and `capabilities/prompt_context.py` tokenize with
`re.compile(r"[^0-9a-z]+")`. Cyrillic and CJK text becomes an empty token set; accented Latin
words are split into unrelated fragments. Rapidfuzz's primary union scorer can still compare
some Unicode strings, but the IDF tie-break and localized domain-keyword selection use these
ASCII-only tokenizers. As a result, the code explicitly loads HA translations and then makes
many translated terms unusable.

Use Unicode-aware alphanumeric tokenization and normalization, and decide how languages
without whitespace segmentation are handled. Add the same behavioral cases in multiple HA
languages, including accented Latin, Cyrillic, and a language such as Chinese or Japanese.
Do not tune English thresholds and assume they transfer unchanged.

### R14. Model-facing capability instructions are hardcoded in English

**Severity:** Medium localization contract violation.

The skeleton header, `Unassigned` label, name-injection header, tool description, parameter
descriptions, and `find_entities` error strings are hardcoded English. These strings directly
shape what the model understands and may be paraphrased to the user. The project treats
localizability as a foundation requirement, not a later polish pass.

Move prompt fragments and tool/error descriptions behind a language-aware translation seam,
or return stable structured error codes whose rendering belongs to the conversation layer.
Tests should build prompts and tool schemas for at least one non-English language.

### R15. Registry-controlled text is inserted into the system prompt without provenance

**Severity:** High security gap while name injection is enabled by default.

Area names, floor names, entity friendly names, and aliases are concatenated directly into
the system message. Newlines and instruction-like text are not structurally encoded or
marked as untrusted data. The security design correctly lists registry and integration text
as an indirect prompt-injection source, but the active Wave 1 prompt path does not yet apply
its provenance-labeling or taint rules.

Quoting is not a complete injection defense, but the prompt should at least use a bounded,
clearly delimited data representation and label the values as data that must not supply
instructions. Add adversarial registry-name tests. Until stronger taint behavior exists,
this reinforces R1: external egress and dangerous sinks must not be available by default.

### R16. The required end-to-end interception test is missing

**Severity:** Medium test-boundary gap.

`build-sequence.md` requires a driven conversation test before tool interception is
considered complete: the provider emits a `tool_use`, the baseline follows its stock path,
and the testbed passes through its proxy path. Existing tests drive prompt construction end
to end and test `TestbedAPI` directly, but no conversation test drives a tool call through
the provider stream and proxy together.

Add the required test with observable execution and result behavior. It should cover a
successful allowed call, a denied call, private outcome stripping, and a provider follow-up
generation. This is the integration point most likely to expose lifecycle behavior that unit
tests around `TestbedAPI` cannot.

## Pass 3: provider adapter and integration lifecycle

Status: complete. Diffed the embedded Claude adapter against the checked-out HA Anthropic
integration, inspected configuration/setup/unload and provider failure paths, and compared
local coverage with the relevant upstream tests.

### R17. Authentication failure has no recovery flow

**Severity:** High production lifecycle defect.

The coordinator correctly raises `ConfigEntryAuthFailed` when the stored API key stops
working, which asks HA to start reauthentication. `MagicMicConfigFlow` implements only
`async_step_user`; it has no reauth step that updates the existing entry. The integration can
therefore detect an expired or revoked key but cannot guide the user through recovery. The
upstream Anthropic integration implements and tests this flow.

Add a reauthentication flow that validates the replacement key, updates the existing config
entry, and reloads it. Cover invalid replacement credentials, connection failure, success,
and coordinator-triggered reauth.

### R18. HACS advertises a Home Assistant version far older than the supported runtime

**Severity:** High installation correctness defect.

`hacs.json` declares Home Assistant `2025.1.0`, while development and tests target Python
3.14 and HA `2026.7.4`/the checked-out `2026.8.0.dev0` API. The integration uses current
conversation, ChatLog, LLM, config-entry runtime-data, and intent contracts without a
compatibility layer. HACS can offer the integration to installations on which it cannot
import or run.

Set the minimum to the earliest HA release actually exercised by CI. If support for older HA
is desired, establish a version matrix and compatibility code rather than advertising an
untested date. The manifest version, release process, and minimum HA version should move
together.

### R19. Provider parity is not guarded by upstream-derived tests

**Severity:** Medium maintenance defect that has already produced R9.

The provider module retains 61% statement coverage in the local suite. Core's Anthropic tests
cover streaming block variants, attachments, whitespace-only content, server-tool results,
API failures, reauth, and option combinations; almost none are carried into this project.
Because the adapter is copied rather than imported, upstream fixes do not arrive through a
dependency update.

Define which upstream tests are copied with the adapter and make syncing them part of the
documented update procedure. A compact parity suite should at least cover every locally
changed method and every content-block family the fork claims to support. Otherwise
"near-upstream" is a comment, not a maintained property.

## Pass 4: evaluation validity and cross-cutting behavior

Status: complete. Reviewed corpus validation, live observation, tool/answer scoring,
state-diff scoring, fixture reset, stored artifacts, A/B execution, and the deterministic
resolver benchmark. Also traced abnormal provider/tool lifetime against identity cleanup.

### R20. Unjudgeable and unbuilt cases are counted as LLM-correct

**Severity:** Foundational measurement defect. Invalidates the headline task-success count.

`case_correct()` returns `None` when a case has no tool or answer predicate. `classify()`
then places any non-error response in `LLM_CORRECT` unless correctness is exactly `False`.
The corpus field `resolves_at_wave0` is serialized into artifacts but never used in scoring.
An ordinary prose refusal, hallucinated completion, or request for clarification therefore
counts as a successful resolution when the case is unjudgeable.

The committed baseline demonstrates the problem. `undo-last` has `correct: null` but is in
the correct bucket. `conditional-reminder` is marked unbuilt, yet the model's false claim
that it created a conditional reminder passes because the predicate only looks for the word
"hour." The scorecard reports zero unresolved cases even though the corpus says these cases
should form the unresolved baseline bucket.

Introduce an explicit expected outcome for unsupported/unresolved behavior and a separate
`UNJUDGED` result that can never increase correctness. Remove `resolves_at_wave0` or make it
an enforced expectation. Recompute every stored headline after this correction.

### R21. Tool scoring permits dangerous extra calls and substring argument matches

**Severity:** High evaluation-safety defect.

Expected tools are matched as an ordered subsequence, so any number of unexpected calls may
occur before, between, or after them without failing the case. String arguments use
case-insensitive substring matching, so an expected entity or value may match a longer,
different target. A turn can perform the expected read plus an unrelated mutation and still
pass.

Make exact normalized matching the default, with explicit per-field match modes for the few
arguments that need containment or tolerance. Classify observed calls as expected,
permitted-supporting, or forbidden-extra; mutating extras should always fail. The effect
journal can eventually provide an independent mutation signal.

### R22. The executable fixture drops state-only entity metadata and exposure setup

**Severity:** High evaluation-fixture defect.

For domains without an executable entity class, `build_executable_world()` writes only the
state string. It drops the corpus friendly name, attributes, device class, registry entry,
area assignment, and explicit exposure. Reset likewise restores only the state string. The
weather fixture therefore loses its temperature and is not prepared like the executable
entities.

The baseline artifact shows the consequence: the weather case is marked correct for calling
`GetLiveContext`, while the spoken answer says no weather integration is configured even
though the corpus defines `weather.home`. Build state-only entities with the same registry,
attributes, area, and exposure semantics as `world.build_world`, and require an answer
predicate that proves the requested fact was returned.

### R23. State-diff scoring misses several classes of unintended side effect

**Severity:** High measurement gap for a safety-oriented assistant.

For undeclared existing entities, `unexpected_changes()` compares only the state string.
Unexpected brightness, volume, setpoint, position, and other attribute mutations pass when
the state remains unchanged. Entities created during a turn are ignored unless the case
declares them. Timers, todo rows, calendar records, notifications, broadcasts, and external
effects are not part of the snapshot at all.

Keep state-diff scoring, but pair it with an execution/effect ledger. Every mutating call or
created durable record must be either expected or forbidden. For entity attributes, define a
small domain-aware set of meaningful reproducible attributes rather than ignoring all of
them to avoid derived-attribute noise.

### R24. The live A/B delta is a single fixed-order sample

**Severity:** Foundational measurement-design defect for the Wave 1 thesis.

The variant runner executes the entire skeleton-only arm and then the entire names-on arm
once. Model sampling, provider load, prompt-cache warmth, and temporal drift are confounded
with the feature flag. The stored `-2 generations` result may be a feature effect or ordinary
model variance; `any_of` handles multiple acceptable outcomes but does not estimate metric
variance.

Run repeated paired trials and alternate or randomize arm order per case. Report per-case
paired deltas, distributions, confidence intervals, and failure counts rather than only
summed totals. Pin model/version and record provider request settings. A single run remains a
smoke test, not evidence for a go/no-go decision.

### R25. Several advertised scorecard dimensions have no live implementation

**Severity:** Medium completeness gap; required before the corresponding product claims.

The current runner does not collect TTFT/TTLT, execute the combined hassil-to-LLM routing
path, drive multi-turn clarification, count turns to completion, or score spoken duration.
`ObservedTurn.clarified` is never populated by a live runner, making the clarification bucket
unreachable. The present artifact measures single LLM-only turns, tool/state predicates,
generations, and provider token usage.

Keep the narrower harness, but label artifacts and build-sequence claims with exactly that
scope. Add each missing dimension before using it as a Wave 1 acceptance gate.

### R26. Abnormal streaming can outlive the resolved-identity registry entry

**Severity:** High cross-cutting failure-path risk; validate before personal tools perform
I/O.

HA's streaming ChatLog starts tool tasks as soon as tool-call deltas arrive. That upstream
code has no local `finally` that cancels and joins all started tasks if the stream fails
before normal tool-result collection. The testbed clears the request's resolved principal in
its outer `finally` as soon as the provider loop exits. A surviving tool task can therefore
resume after an await and see the unidentified fallback from `get_resolved_user()`, record an
effect after the turn ended, or mutate state without a returned tool result.

Add a fault-injection test that starts a blocking personal tool, fails or cancels the model
stream, and observes task cancellation, identity lifetime, and effect recording. Either own
and drain tool tasks at the provider/request boundary or make execution carry an immutable
request context that does not depend on a cleared global lookup while in flight.

## Declared limitations confirmed by the trace

These are not new findings, but they bound what the implemented foundation currently proves:

- Every live request resolves with `RequestSource.UNKNOWN`; no request adapter supplies text,
  voice, or established speaker identity yet.
- Every live turn has `is_continuation=False`; the policy behavior exists only in isolated
  tests until a trusted continuation-origin side channel is added.
- No yes/no entry point consumes a pending operation.
- No local intent or LLM tool invokes undo, and the built-in undo executors are not registered
  in live setup.
- Locally handled hassil mutations bypass the proxy and its effect journal.
- The default legacy policy registry is empty, and unclassified tools remain permissive.

These limitations are documented, but calling foundation sections 1 through 5 "complete"
means the data contracts exist, not that their safety properties are active end to end.

## Verification evidence

- `.venv/bin/pytest tests -q --cov=custom_components.magic_mic`: 146 passed, 86% statement
  coverage.
- The uncovered proxy line for an undeclared tool is the bypass in R3.
- A bare `.venv/bin/pytest` with the reference trees present fails during collection, as
  described in R11.
- The provider diff against `references/core/homeassistant/components/anthropic/entity.py`
  shows the missing empty-content guards in R9.

## Recommended remediation order

1. R1: disable unconfigured egress and establish the provider-capability boundary.
2. R2: make turn metadata request-local.
3. R3 and R12: reject undeclared calls and close the argument check/use gap.
4. R5: make storage enforce principal and scope before a capability consumes it.
5. R13 through R15: make the active prompt path localizable and bound registry text.
6. R4: finish conflict semantics before enabling confirmation-sensitive tools.
7. R6 through R10 before the corresponding prompt, personal-data, or provider feature grows.
8. R17 and R18 before treating the custom integration as generally installable.
9. R20 through R24 before using the current scorecard to accept or reject the architecture.
10. R25 and R26 before claiming end-to-end latency, clarification, or personal-tool safety.
11. R11, R16, and R19 as test/tooling cleanup that prevents recurrence.
