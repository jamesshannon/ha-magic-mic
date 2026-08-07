# Core Deltas: upstream behavior we compensate for

> Ledger, not a design doc. Each entry is a Home Assistant core behavior that Magic Mic
> either works around or depends on, recorded so the compensation is traceable to its
> cause and can be removed when core changes. Feeds the adoption sequence in
> `PRODUCT_PLAN.md` §7. Distinct from [`evaluation.md`](evaluation.md) Part E, which
> tracks *our* unproven empirical claims; these are *core's* observed behavior.

## Why this file exists

A finding like "core serializes entity fields as `entity_id` but never gives the model
one" is expensive to derive and cheap to lose. It arrives mid-task, gets worked around in
one commit, and then the workaround looks like ordinary code to the next reader. Six
months later nobody knows whether core still behaves that way, so the compensation stays
forever or gets removed on a guess.

Three rules keep an entry honest:

1. **Pin the version.** Every entry records the Home Assistant release it was verified
   against. `.venv` is the compatibility baseline (CLAUDE.md), line numbers drift on every
   bump, and an unpinned citation is an assertion rather than a fact.
2. **Pin the behavior in a test.** Every entry names a test in
   `tests/components/magic_mic/test_core_contracts.py` that asserts the upstream behavior
   still holds. When core fixes it, that test fails on the next dependency bump and points
   at the compensation to delete. Documentation alone does not survive a version bump;
   a failing test does.
3. **Say what a core fix looks like.** The point of the proving ground is evidence for
   upstream work (§7). An entry that only describes a workaround has thrown away the
   contribution.

Entries are retired, not deleted: keep the row, mark it fixed, note the release, and say
what came out of our tree.

---

## CD1. `ActionTool` resolves area and floor names but not entity names

**Verified against:** Home Assistant 2026.7.4.

**Behavior.** `ActionTool` is what exposes a script to the model (`helpers/llm.py:979`).
Its `async_call` walks the parameter schema and converts registry references from names to
ids before the service call, for exactly two selector types (`helpers/llm.py:1011-1035`):

```python
if isinstance(validator, selector.AreaSelector):
    area = list(intent.find_areas(area, area_reg))[0].id
elif isinstance(validator, selector.FloorSelector):
    ...  # find_floors(...) -> floor.floor_id
```

Entity fields get no such treatment. `EntitySelector` serializes to
`{"type": "string", "format": "entity_id"}` (`helpers/llm.py:816`), and the raw string the
model produced is passed to `hass.services.async_call` unchanged. Meanwhile the prompt
contains no entity ids at all: the `homeassistant` platform's static context is names,
aliases, domain, and areas (§2.5). `TargetSelector` has the same hole by a different route,
serializing to `cv.TARGET_FIELDS`, whose `entity_id` member is a bare
`strict_entity_id` list (`helpers/config_validation.py:1310`).

So core advertises a field as an entity_id, gives the model no entity_ids, and executes
whatever string comes back. Areas and floors are name-in and id-resolved; entities are
id-in and unresolved.

**Why it matters here.** Our match-layer fuzzy fallback
([`find-entities.md`](find-entities.md) Consumer 1) catches `intent.MatchFailedError`,
which only intent tools raise. A script tool handed a fabricated id never reaches the
matcher, so the service call quietly targets nothing. The bug reproduces inside Magic Mic
today.

**Compensation.** `capabilities/action_targets.py` (Consumer 3 of the resolver primitive),
wired into the proxy's tool-execution seam ahead of argument validation and policy
evaluation. Three exact rungs (live entity id passes through untouched, exact name or alias,
de-slugged id matched inside its own domain); when all three miss, fuzzy scoring only
populates the candidate list the model is asked to choose from. Nothing here resolves on a
fuzzy score, because the input is an identifier the model synthesized rather than the user's
own words. Design and the rejected raised-threshold alternative in
[`find-entities.md`](find-entities.md) "Consumer 3".

**What a core fix looks like.** Give `EntitySelector` fields the same name-to-id
conversion `AreaSelector` fields already get, inside `ActionTool.async_call`. The blast
radius is one method on the LLM path, and the pattern is already there to match. It needs
graceful failure, which the area path lacks (see CD3), and it wants a resolution step that
can report ambiguity rather than only success or failure, which exact matching cannot (see
CD2).

**Contract test:** `test_action_tool_does_not_resolve_entity_names`,
`test_action_tool_resolves_area_names`,
`test_entity_selector_serializes_as_entity_id`. Behavior tests for the compensation are in
`test_action_targets.py`.

**Measured by.** `evals/harness/entity_id_tools.py` over
`evals/corpus/wave1_entity_id_tools.yaml`, paired per case with resolution off and on. Off is
this entry's behavior, so the arm delta is the size of the problem rather than an assertion
about it. Unrun against a live model as of 2026-08-06; the arms are proven to differ
deterministically in `test_entity_id_tools.py`.

**Upstream.** Reported independently on the core issue tracker (2026-08) by a user whose
exposed script tools received invented ids from Gemini Flash. Their three proposed fixes
were prompt-side ids, a lookup intent, and a resolution action for script authors to call;
the selector asymmetry above argues for a fourth. Add the issue link here once the comment
is posted.

---

## CD2. Name matching in the LLM path is exact

**Verified against:** Home Assistant 2026.7.4.

**Behavior.** `_filter_by_name` compares `name.strip().casefold()` against the entity's
name and aliases with no edit distance (`helpers/intent.py:419`, `440`). A literal
`entity_id` is accepted verbatim on the way in (`:432`), which is the seam every
compensation here rides. hassil's fuzzy matcher is not in this path, so the LLM path has no
downstream safety net (§2.4).

Two properties are easy to get backwards and worth stating flatly:

- The comparison is against the **friendly name**, never the id. `light.office_lamp_a1b2c3`
  matches "Office Lamp" today. An unguessable id is not by itself a resolution failure.
- Duplicate names are handled by `MatchTargetsPreferences` (`helpers/intent.py:359`),
  threaded from the satellite's device area by `IntentTool.async_call`
  (`helpers/llm.py:315-330`). It is a **hard filter, not a ranking bias**, it runs only when
  `allow_duplicate_names` is false, and it groups on byte-identical `matched_name`. Two
  "Ceiling Light"s in one room still fail; "Kitchen Light" against "Kitchen Lights" gets no
  help from it at all.

**Why it matters here.** In the LLM path we are the natural-language layer. A paraphrase
("reading light" for "Reading Lamp") is a hard failure with no recovery.

**Compensation.** `capabilities/match_fallback.py`: on `MatchFailedReason.NAME`, re-run the
structured filters without the name, fuzzy-score against the recovered candidate set, and
either retry with the canonical id or hand back candidates for the model to ask about.
Wired at the proxy's tool-execution seam (`testbed/api.py:280`).

**What a core fix looks like.** Fuzzy fallback inside the match layer behind an opt-in
`fuzzy=` constraint, so the hassil path stays deterministic. Costs nothing on the happy
path because it runs only after an exact miss and rides the `tool_use` that was already
happening. Design and generation-count argument in [`find-entities.md`](find-entities.md).

**Contract test:** `test_filter_by_name_is_exact`,
`test_filter_by_name_accepts_entity_id`.

---

## CD3. Area and floor resolution in `ActionTool` raises on no match

**Verified against:** Home Assistant 2026.7.4.

**Behavior.** `list(intent.find_areas(area, area_reg))[0].id` (`helpers/llm.py:1020`)
indexes an empty list when the model names an area that does not exist. `find_areas` is a
generator over exact name and alias comparisons (`helpers/intent.py:381`), so any typo or
paraphrase yields nothing and the tool call raises `IndexError` from inside
`ActionTool.async_call`. The floor branch has the same shape.

**Why it matters here.** It is the model of what *not* to copy when CD1's compensation
lands. A resolution failure should reach the model as a readable tool result it can act on,
not as an exception from a helper.

**Compensation.** None needed today; we do not call this path. Recorded because Consumer 3
will be implementing the same conversion one selector type over, and the obvious move is to
mirror the existing code.

**What a core fix looks like.** Return a structured failure the conversation layer can turn
into a `tool_result`, the way `MatchFailedError` already flows through `async_handle` into
`ChatLog` (`conversation/chat_log.py:462-465`, which catches `HomeAssistantError` and
returns `{"error", "error_text"}`). Small, self-contained, and a reasonable
first upstream patch to pair with CD1.

**Contract test:** `test_action_tool_unknown_area_raises_index_error`.

---

## CD4. Nothing validates tool arguments against the tool schema

**Verified against:** Home Assistant 2026.7.4.

**Behavior.** `APIInstance.async_call_tool` is documented as "Call a LLM tool, validate args
and return the response" (`helpers/llm.py:243`). It traces the call, finds the tool by name,
and dispatches to `tool.async_call` (`:242-260`). No validation happens, there or in
`ActionTool.async_call`. The model's arguments reach the service call as typed.

The selectors are perfectly capable of rejecting them. `EntitySelector.__call__` runs
`cv.entity_id_or_uuid` over each value and enforces the config's `domain`,
`include_entities`, and `exclude_entities` (`helpers/selector.py:1018`). It is simply
never invoked on this path, so "Office Lamp" in an entity field is passed along rather than
refused.

**Why it matters here.** It cuts both ways, which is why it is worth pinning rather than
enjoying quietly.

- It is what makes CD1's compensation possible. Consumer 3 can only resolve a friendly name
  in an entity field because that name survives long enough to be seen. If core started
  validating here, the model's non-id string would raise `vol.Invalid` before any
  resolution ran, and Consumer 3 would have to move to a different seam or feed the model
  ids after all.
- Magic Mic's own tools validate their arguments themselves rather than relying on the
  caller (`FindEntitiesTool.async_call` calls `self.parameters(tool_input.tool_args)`).
  That is the correct habit given this behavior, and it is a habit, not something the
  framework enforces.

**Compensation.** Per-tool argument validation in our capability tools. No workaround for
the framework gap itself.

**What a core fix looks like.** Either validate in `async_call_tool` and make the docstring
true, or fix the docstring. If the former, the entity-name case (CD1) needs its resolution
step to run *before* validation, which is an argument for putting the conversion in
`ActionTool.async_call` where the area conversion already lives, rather than in a generic
validating wrapper.

**Contract test:** `test_async_call_tool_does_not_validate_arguments`.
