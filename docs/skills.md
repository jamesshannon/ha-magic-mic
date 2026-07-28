# SKILLs (gated instructional payloads)

> A **SKILL** is a block of *procedural instruction* — how to reason through a
> multi-step, rare, or reasoning-heavy task — injected into the prompt only when
> relevant, the way Claude Code's own skills load on demand. This doc separates the
> two mechanisms that both look like "load instructions when needed," names the
> **three gating classes by who owns the gate**, and scopes what's v1. Cross-refs:
> [`ephemeral-automations.md`](ephemeral-automations.md) (the v1 consumer),
> [`learning.md`](learning.md) (the *other* mechanism, not this one),
> [`prompt-context.md`](prompt-context.md) (budget + cache), PRODUCT_PLAN §2.5
> (dynamic tool lists), §5.4 (determinism-in-tools), §5.6 (primitives), §6.2
> (multi-provider budget).

---

## TL;DR

- The dynamic prompt-assembly seam (§2.5 `async_get_tools` + §5.2 tier-2 injection)
  already carries **three payload types, all gated on predicates: tool definitions,
  context data, and — this doc — instructions.** A SKILL is the instruction payload.
- **Two mechanisms wear the word "skill." Keep them apart:**
  - **Machinery-gated injection** — *we* observe deterministic state (friction
    fired, a tool ran, turn>1) and inject the instruction block **whole**, zero
    extra generation. This is [`learning.md`](learning.md)'s Resolve-Friction text.
    **It is not a registry SKILL.**
  - **SKILL registry** — a bounded set of skills whose **~25-token headers stay
    resident** in the cached prefix; the model pulls a body via a **`read_file`** tool
    (named for the trained affordance, sandboxed to skills — see below) when it judges
    the task warrants it (one generation). This doc owns *this*.
- **The two are chosen by whether a deterministic gate exists.** When we can see the
  need in our own machinery → inject whole (machinery-gated). When the need lives
  only in user *intent* with no crisp signal (authoring an automation) → resident
  header + LLM-pulled body (registry).
- **v1 = the registry with exactly one consumer: ephemeral-automation authoring.**
  It's the case with no deterministic gate and a body too big/rare to keep resident.
- **v2 = third-party integrations publishing skills** — deferred, because it inherits
  the §6.2 budget arbiter (resident-header cost scales with installed add-ons).
- **Name it `read_file` (trained affordance), sandbox it to skills (authority).**
  The model has seen `read_file` load a skill thousands of times; a novel
  `load_skill` fights that prior. Keep the name, make the *implementation* a bounded
  skill-only resolver — the security property is the sandbox, not the name
  ([`security.md`](security.md)).

---

## Two mechanisms, one word

Both "load instructions when relevant," but they are different machinery and must
not be merged:

| | **Machinery-gated injection** | **SKILL registry** |
|---|---|---|
| Owns the trigger | **We do** — observable deterministic state | **The model** — its own relevance judgment |
| What's resident | nothing until the gate fires | **~25-tok header per skill**, always (cached prefix) |
| How the body arrives | injected **whole** on the gated request | model calls **`read_file`** (skill-sandboxed) → body |
| Extra generations | **zero** (rides gen-2's tool-list reissue) | **one** (the pull) — acceptable on rare/deliberate flows |
| Example | Resolve-Friction text + resolver tools ([`learning.md`](learning.md)) | ephemeral-automation authoring guide |

The friction case works *because we own the match layer* — a disambiguation firing
is state in our own code, so we inject the whole instruction block for free. The
automation case has **no such signal** (whether "when the laundry's done, start the
dryer" is an automation vs. a reminder vs. a one-shot is semantic, invisible to our
machinery) — so it must be model-pulled. That absence isn't a gap to engineer
around; it's what sorts a skill into the registry.

---

## Gating classes — by who owns the gate

The axis is **who owns the gate**, and only the third class carries a
trust-and-budget problem:

1. **Machinery-gated (we own the state).** Friction fired, a tool ran, turn>1.
   Precise, free, injected whole. *Not a registry SKILL* — see
   [`learning.md`](learning.md). Precision/recall follows the signal owner exactly
   as there: HA-owned signal → precise; LLM-owned → coarse necessary-condition.
2. **LLM-signaled (the model owns the judgment).** No deterministic signal exists;
   a resident header lets the model recognize the task and `read_file` the body.
   Cost: one generation, fine on deliberate/rare flows. **Ephemeral-automation
   authoring is the worked example.** (A cheap shape-classifier on the way in —
   temporal/conditional connectives ∧ not-locally-resolved — can *coarsely* pre-load
   as a necessary-condition excluder, but that's a semantic judgment in a cheap hat,
   not determinism.)
3. **Provider-declared (a third party owns the gate).** An integration ships
   `{SKILL header, tools, gate}`. **The author's incentive is over-inclusion**, so
   the platform cannot treat a publisher's gate as authoritative. The
   resident-header model caps a greedy author's blast radius to their ~25-token
   header (they can't force body-injection — the model decides), but resident cost
   still scales with the *number of installed add-ons* → at scale needs the **§6.2
   relevance filter over headers** to decide which even load. This is the
   extension-contract question §6.2 says to settle in the Magic Mic proving ground
   **before it freezes in core.** → **v2.**

A publisher keyword gate ("wear" ∈ request) is a **lossy semantic proxy**, not
determinism: it misses "what should I put on tomorrow" and false-fires on "wear and
tear on the compressor." The resident-header + model-selects route is
paraphrase-robust and is the same mechanism as the §6.2 budget arbiter — **one
filter, two payoffs.**

---

## Cost model

- **Headers live in the stable cached prefix.** N skills × ~25 tok is a per-request
  constant that never varies → it sits in the cached instruction prefix
  ([`prompt-context.md`](prompt-context.md)), so it's cache-cheap, not a TTFT
  villain. The resident cost is paid once and cached.
- **The pull is one generation, and it amortizes.** the `read_file` pull is a
  `tool_use` round-trip, but authoring is already a multi-gen, deliberate, rare flow — never
  the "turn on the light" hot path. And the skill-loaded reasoning happens **once at
  authoring**; the compiled automation then fires deterministically forever
  (compile-once/run-deterministic, [`ephemeral-automations.md`](ephemeral-automations.md)
  / [`offline.md`](offline.md) L2). You pay a generation to *build* the rule, not to
  run it.
- **`read_file`-named, skill-sandboxed.** Name it `read_file` to match the trained
  affordance (the model uses it reliably), but implement a **bounded resolver**:
  headers advertise skills *as* paths (`skills/ephemeral_automation.md`) so the model
  reads what it was shown and never invents `/config/secrets.yaml`; the resolver
  **hard-refuses out-of-registry paths** anyway, because an *injection* will try them
  regardless ([`security.md`](security.md)). Name = ergonomics; authority = the
  resolver. Turn an out-of-registry refusal into a **redirect** ("available skills:
  …") — a refusal that burns a generation is worse than one that points the way.

---

## v1 scope & open questions

- **v1:** the registry + `load_skill`, scoped to **one skill — ephemeral-automation
  authoring** (the `{trigger, condition, action}` grammar, entity-id discipline,
  worked examples). It earns the mechanism by itself; nothing else needs it yet
  (friction is machinery-gated; everything tool-shaped needs no skill).
- **Deferred to v2:** third-party integrations publishing skills — carries the §6.2
  header-budget arbiter (relevance filter over headers) as a hard dependency, so
  it's publishing **+** the filter, not publishing alone.
- **Open (implementation-level):**
  - Header token budget and how tight the resident line must be.
  - Whether v1's single skill even needs the pull, or is cheap enough to inject on a
    coarse class-2 shape-classifier (excluder) — measure before adding the round-trip.
  - Registry placement relative to `capabilities/` and the friction registry (both
    gate *other* capabilities' behavior).

---

## Related docs

- [`ephemeral-automations.md`](ephemeral-automations.md) — the v1 consumer; authoring
  is the class-2 case.
- [`learning.md`](learning.md) — the **machinery-gated** mechanism (Resolve-Friction
  text); explicitly *not* this registry.
- [`prompt-context.md`](prompt-context.md) — resident-header budget + the cache model.
- [`security.md`](security.md) — why `load_skill`, not `read_file`.
- PRODUCT_PLAN §6.2 — multi-provider budget; where v2 provider-declared skills live.
