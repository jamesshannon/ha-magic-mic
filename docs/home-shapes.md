# Home shapes: context strategy as a selection problem

> Design note, not a built feature. Records why the prompt-context mechanisms should stop
> competing for one default and start being chosen per home. Written 2026-08-06, prompted by
> the entity-argument measurements in [`find-entities.md`](find-entities.md) and the
> name-injection result in [`prompt-context.md`](prompt-context.md).

## The problem this reframes

Three mechanisms decide what the model knows about the home before it acts:

| mechanism | what it puts in the prompt | what it costs |
|---|---|---|
| full roster (stock Assist) | every exposed entity's name, domain, area | grows with the home; no ids |
| entity summary (Tier 1) | area/domain counts, no names | flat; forces a lookup turn |
| name injection (Tier 2) | names and ids for a request-relevant subset | per-turn, re-prefills the cached block |

They have been treated as candidates for a single default, and measured that way: summary
replaced the roster, injection was measured against summary-only, lost on cost, and was
switched off. That framing produces a winner and two losers, and it is the wrong shape for the
question.

Every one of those measurements ran on the same six-entity fixture home. On a home that size
the full roster is a handful of lines, so the summary's whole reason for existing (bounded
prompt growth) does not apply, and the lookup turn it forces is a pure loss. On a home with
four hundred entities the roster is the thing that cannot ship. The mechanisms are not
competing answers to one question. They are answers to different homes.

## The claim

**The right context strategy is a function of the home's shape, and should be selected rather
than configured.** The inputs are already available deterministically at setup and on registry
change: exposed entity count, tool/script count, area and floor count, how many entities share
a name, and whether object ids are slugified friendly names or opaque vendor strings.

Sketching where the boundaries plausibly sit, which is a hypothesis and not a measurement:

- **Small home, few tools.** Full roster. The model gets every name up front, passes one
  straight into the tool ([`find-entities.md`](find-entities.md) "Advertising the
  resolution"), and never spends a lookup turn. `find_entities` earns its place only for
  oblique references.
- **Large home.** Summary plus lookup. The roster is unaffordable, so the model reasons about
  rooms from counts and pays a turn for the names it actually needs.
- **In between, or a home with a hot subset.** Summary plus Tier-2 injection. This is the band
  injection was designed for and has never been measured in: enough entities that the roster
  hurts, few enough per request that a relevant subset is small, and enough repeat traffic
  that the same names recur. The 1.45x cost result is a measurement of injection on a home too
  small to need it, not a verdict on the mechanism.

## Why this matters beyond tidiness

It changes how existing results should be read. Three of our recorded findings are currently
phrased as properties of a mechanism when they are properties of a mechanism *on a six-entity
home*:

- Name injection costs 1.45x for no benefit (`evaluation.md` Part E).
- Entity-argument resolution has no measurable value (2026-08-06, summary on).
- Entity-argument resolution rescues 6 of 6 (2026-08-06, roster on, same corpus, same model).

The last two are the same code on the same cases, and they disagree because the prompt
differs. That is not noise. It is the strongest evidence available that the prompt strategy,
not the resolution machinery, is the variable that decides outcomes here.

## The analogy, and its limit

This is the shape of an LLM router: inspect the request, pick the strategy, and let a cheap
deterministic classifier stand in front of an expensive non-deterministic one. The analogy is
useful for the *structure* and misleading about the *inputs*. A router picks per request from
signals in the request. Home shape is stable across thousands of requests and changes only
when the registry does, so the selection is closer to a setup-time decision recomputed on
registry change than to a per-turn classifier. That distinction matters: it means the strategy
can be decided once and cached, and it means a wrong choice is systematically wrong rather
than occasionally wrong, which raises the bar on getting it right.

Per-request selection is a possible later refinement (a request naming a device might take the
roster path while a vague one takes the lookup path), and it is explicitly not what this note
proposes.

## What would have to be true first

1. **A corpus with more than one home shape.** Everything above is unfalsifiable on a
   six-entity fixture. The corpus needs at least a small home and a large one with the same
   cases, so a strategy can be scored per shape instead of per mechanism. This is the same gap
   `evaluation.md` Part E records as "the curated golden set over-cleans results", reached
   from a different direction.
2. **The mechanisms kept, not pruned.** Deleting injection because it lost on one home would
   destroy the evidence needed to test this. It stays behind `CONF_NAME_INJECTION`, off, with
   its measurement and reasoning intact, for exactly this reason.
3. **A selection rule simple enough to explain.** The point is fewer decisions for the user,
   not a new subsystem. If the rule cannot be stated in a sentence and shown in the UI as the
   strategy it picked and why, it is not an improvement over a documented default
   (`PRODUCT_PLAN.md` §5.3: prefer lookups and composed functions over new engines).

## Status

Recorded, not scheduled. No code depends on it. Revisit when the corpus carries a second home
shape, or when a real installation is large enough to make the roster path visibly fail.
