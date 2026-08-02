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

`internal/claude/const.py` sets both `CONF_WEB_SEARCH` and `CONF_WEB_FETCH` to `True`.
`ClaudeBaseLLMEntity._get_model_args()` merges those defaults into both the baseline and
testbed agents, then sends Anthropic's server-side tools directly in the provider request.
Those tools do not appear in `chat_log.llm_api.tools`, so `TestbedAPI` cannot filter, classify,
confirm, trace, or journal them.

This conflicts with the source-of-truth product plan and `security.md`, which say external
retrieval ships off as an explicit egress and untrusted-content opt-in. `web-search.md` says
the opposite in several places and is internally inconsistent with its own security
cross-reference. The checked-out HA Anthropic integration defaults both tools off; Magic Mic
changed them to on.

The immediate correction is to default both provider-native tools off. Before either is
configurable, define a provider-capability control passed from the proxy/configuration layer
into the provider adapter. Do not claim that `TestbedAPI` bounds every capability while
provider-native tools can be added after the wrapped `APIInstance` is formatted.

### R2. Concurrent turns in one conversation corrupt turn attribution

**Severity:** Foundational contract defect.

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
8. R11 and R16 as independent test/tooling cleanup.
