"""Capability selection: compile the full tool catalog into a per-turn API.

The prompt-time system from `docs/capability-selection.md` (PRODUCT_PLAN §5.6/§6.2). A
real home exposes far more tools than any one request needs; left unbounded, that dumps
every integration's schema and instructions into the prompt and competes for the
provider's 128-tool ceiling. This module turns the installed catalog into a small,
relevant, authorized subset for one turn:

1. enumerate a provider-neutral `CapabilityDescriptor` catalog (`default_catalog`);
2. deterministically drop what the request cannot use (`available_descriptors`, Stage 1);
3. rank the rest from the utterance (`rank_descriptors`, Stage 2 — chunk 2);
4. fit tools + dependencies under a budget (`assemble_plan`, Stage 4 — chunk 2).

This is *not* the security boundary: exposure is a prompt/UX bound, the execution recheck
in `TestbedAPI` stays authoritative (docs "Stage 5"). Wave 1 runs the whole thing in
shadow mode against the full-roster baseline before it may remove a real tool.

Descriptors are provider-neutral and contributed by ordinary internal composition, not
manufactured provider registrations (docs "Capability catalog"). `selection_text` and
`examples` are *retrieval documents*, not always-injected prompt text: they may be richer
than a tool description without charging every request for those tokens.
"""

from dataclasses import dataclass, field

from ..const import (
    SELECTION_DOMAIN_BOOST,
    SELECTION_RELEVANCE_FLOOR,
    SELECTION_TOOL_BUDGET,
)
from ..fuzzy import score, tokenize


@dataclass(slots=True, frozen=True)
class CapabilityDescriptor:
    """Compact retrieval metadata for one selectable capability bundle.

    A bundle keeps its tools, instructions, context, and dependencies together so the
    model never has to understand internal wiring (docs "Bundles and tools"). ``tools``
    is the executable payload; ``selection_text``/``examples``/``domains`` are the
    retrieval documents Stage 2 ranks; ``dependencies`` names other descriptor ids that
    must be admitted alongside this one (Stage 4 dependency closure).
    """

    id: str
    selection_text: str
    tools: tuple[str, ...]
    examples: tuple[str, ...] = ()
    domains: tuple[str, ...] = ()
    instructions: tuple[str, ...] = ()
    context_loaders: tuple[str, ...] = ()
    dependencies: tuple[str, ...] = ()
    # A resident bundle bypasses the relevance floor and budget pruning: a cheap,
    # frequently used read the model reaches for on almost any turn (docs "Stage 4",
    # residency "earned through traces"). Kept rare and marked, never assumed by intuition.
    resident: bool = False


@dataclass(slots=True, frozen=True)
class Catalog:
    """The installed capability catalog: descriptors indexed by id.

    ``by_tool`` maps each declared tool name back to its owning descriptor id, so shadow
    scoring can ask "which bundle would have exposed the tool the model actually used".
    """

    descriptors: tuple[CapabilityDescriptor, ...]
    by_id: dict[str, CapabilityDescriptor] = field(init=False)
    by_tool: dict[str, str] = field(init=False)

    def __post_init__(self) -> None:
        """Index descriptors by id and by declared tool name."""
        by_id = {descriptor.id: descriptor for descriptor in self.descriptors}
        by_tool: dict[str, str] = {}
        for descriptor in self.descriptors:
            for tool in descriptor.tools:
                by_tool[tool] = descriptor.id
        object.__setattr__(self, "by_id", by_id)
        object.__setattr__(self, "by_tool", by_tool)

    def tool_names(self) -> frozenset[str]:
        """Return every tool name any descriptor in the catalog declares."""
        return frozenset(self.by_tool)


# The provider-neutral demo catalog. It groups the HA Assist intent tools (plus Magic
# Mic's `find_entities`) into bundles by function, following the design doc's worked
# examples. The tool *names* mirror HA's registered intents; availability filtering
# (Stage 1) intersects them with what the running system actually exposes, so a bundle
# whose integration is absent drops out rather than advertising a tool that cannot run.
#
# selection_text/examples are English here. That is acceptable for *shadow* measurement,
# but localized retrieval documents are a hard requirement before lexical selection may
# gate a live request (docs "Localization"; tracked in evaluation.md's ledger).
_DEFAULT_DESCRIPTORS: tuple[CapabilityDescriptor, ...] = (
    CapabilityDescriptor(
        id="device_control",
        selection_text="Turn devices, lights, switches, fans, and plugs on or off",
        tools=("HassTurnOn", "HassTurnOff", "HassToggle"),
        examples=(
            "turn on the kitchen light",
            "switch off the fan",
            "turn everything off",
        ),
        domains=("light", "switch", "fan", "media_player", "cover"),
    ),
    CapabilityDescriptor(
        id="light_settings",
        selection_text="Set light brightness, dim level, or color",
        tools=("HassLightSet",),
        examples=(
            "dim the bedroom lights",
            "set the lamp to blue",
            "brightness to fifty percent",
        ),
        domains=("light",),
    ),
    CapabilityDescriptor(
        id="covers",
        selection_text="Open, close, or set the position of covers, blinds, and garage doors",
        tools=("HassSetPosition",),
        examples=(
            "open the garage door",
            "close the blinds",
            "set the shades halfway",
        ),
        domains=("cover",),
    ),
    CapabilityDescriptor(
        id="media",
        selection_text="Control media playback: pause, resume, skip, or go back",
        tools=(
            "HassMediaPause",
            "HassMediaUnpause",
            "HassMediaNext",
            "HassMediaPrevious",
        ),
        examples=(
            "pause the music",
            "skip this track",
            "resume playback",
        ),
        domains=("media_player",),
    ),
    CapabilityDescriptor(
        id="volume",
        selection_text="Set the volume of a speaker or media player",
        tools=("HassSetVolume",),
        examples=(
            "turn the volume up",
            "set volume to thirty percent",
            "make it quieter",
        ),
        domains=("media_player",),
    ),
    CapabilityDescriptor(
        id="climate",
        selection_text="Read or set the thermostat temperature and heating or cooling",
        tools=("HassClimateGetTemperature", "HassClimateSetTemperature"),
        examples=(
            "set the thermostat to seventy",
            "how warm is it in here",
            "turn up the heat",
        ),
        domains=("climate",),
    ),
    CapabilityDescriptor(
        id="timers",
        selection_text="Set, cancel, pause, or check countdown timers",
        tools=(
            "HassStartTimer",
            "HassCancelTimer",
            "HassPauseTimer",
            "HassUnpauseTimer",
            "HassTimerStatus",
        ),
        examples=(
            "set a timer for ten minutes",
            "how much time is left",
            "cancel the timer",
        ),
    ),
    CapabilityDescriptor(
        id="lists",
        selection_text="Add to, complete, or read shopping lists and to-do lists",
        tools=("HassListAddItem", "HassListCompleteItem", "todo_get_items"),
        examples=(
            "add milk to my shopping list",
            "what is on my to-do list",
            "mark the eggs as done",
        ),
        domains=("todo",),
    ),
    CapabilityDescriptor(
        id="weather",
        selection_text="Weather forecast, conditions, and temperature outside",
        tools=("HassGetWeather",),
        examples=(
            "what is the weather tomorrow",
            "is it going to rain",
            "how cold is it outside",
        ),
        domains=("weather",),
    ),
    CapabilityDescriptor(
        id="find_entities",
        selection_text="Look up devices, areas, or lists by an approximate or partial name",
        tools=("find_entities",),
        examples=(
            "which lights do I have",
            "find the thing by the couch",
        ),
    ),
    # Resident reads: cheap, reached on almost any turn, and the model needs them to answer
    # state and time questions before acting. Marked resident so they survive the budget
    # rather than being pruned as low-relevance on a device-control turn.
    CapabilityDescriptor(
        id="live_context",
        selection_text="Answer questions about the current state of devices and the home",
        tools=("GetLiveContext",),
        examples=(
            "is the garage door open",
            "which lights are on",
        ),
        resident=True,
    ),
    CapabilityDescriptor(
        id="datetime",
        selection_text="The current date, time, and day of the week",
        tools=("GetDateTime",),
        examples=(
            "what time is it",
            "what is today's date",
        ),
        resident=True,
    ),
)


def default_catalog() -> Catalog:
    """Return the provider-neutral demo capability catalog."""
    return Catalog(_DEFAULT_DESCRIPTORS)


@dataclass(slots=True, frozen=True)
class FilteredCapability:
    """A descriptor removed by Stage 1, with a safe reason code for the trace."""

    id: str
    reason: str


@dataclass(slots=True, frozen=True)
class AvailableCatalog:
    """Stage 1 output: descriptors the request can use, plus what was filtered and why.

    Each surviving descriptor is projected to only the tools the running system actually
    exposes (``exposed_tools``), so relevance retrieval and the budget never count a tool
    that could not execute. A descriptor whose tools are all absent is filtered.
    """

    descriptors: tuple[CapabilityDescriptor, ...]
    filtered: tuple[FilteredCapability, ...]


def available_descriptors(
    catalog: Catalog, exposed_tools: frozenset[str] | set[str]
) -> AvailableCatalog:
    """Deterministically drop capabilities the request cannot use (docs "Stage 1").

    ``exposed_tools`` is the set of tool names the running system would actually offer
    this turn (the inner ``APIInstance.tools`` names, after the policy exposure filter).
    A descriptor survives only for its tools present in that set; a descriptor with no
    present tool is filtered as ``unavailable``. Retrieval cannot restore a filtered
    capability. This grounds availability in the live system rather than a fabricated
    requirements engine: an absent integration simply contributes no exposed tool.
    """
    exposed = frozenset(exposed_tools)
    kept: list[CapabilityDescriptor] = []
    filtered: list[FilteredCapability] = []
    for descriptor in catalog.descriptors:
        present = tuple(tool for tool in descriptor.tools if tool in exposed)
        if not present:
            filtered.append(FilteredCapability(descriptor.id, "unavailable"))
            continue
        if len(present) == len(descriptor.tools):
            kept.append(descriptor)
        else:
            # Keep the bundle but expose only the runnable subset.
            kept.append(
                CapabilityDescriptor(
                    id=descriptor.id,
                    selection_text=descriptor.selection_text,
                    tools=present,
                    examples=descriptor.examples,
                    domains=descriptor.domains,
                    instructions=descriptor.instructions,
                    context_loaders=descriptor.context_loaders,
                    dependencies=descriptor.dependencies,
                    resident=descriptor.resident,
                )
            )
    return AvailableCatalog(tuple(kept), tuple(filtered))


@dataclass(slots=True, frozen=True)
class ScoredDescriptor:
    """A descriptor with its Stage-2 relevance score and the signals that produced it.

    ``score`` is a ranking device on a 0-100 scale, not a calibrated probability (docs
    "Stage 2"). ``text`` is the lexical match against the retrieval documents; ``domain``
    is the structural boost from an explicit domain word in the request. ``resident``
    marks a bundle that is admitted regardless of score.
    """

    descriptor: CapabilityDescriptor
    score: float
    text: float
    domain: float
    resident: bool


def rank_descriptors(
    utterance: str, descriptors: tuple[CapabilityDescriptor, ...]
) -> list[ScoredDescriptor]:
    """Rank available descriptors by relevance to ``utterance`` (docs "Stage 2").

    High-recall lexical retrieval: score the request against each retrieval document
    (``selection_text`` and each example) with the shared Unicode-aware fuzzy scorer and
    keep the best, so a near-paraphrase example lifts the bundle without diluting the
    match across a long combined string. Add a structural boost when the request names one
    of the bundle's declared domains outright. Resident bundles are ranked too, but the
    assembler admits them regardless of where they land. Ties break on descriptor id for a
    deterministic order.
    """
    query_tokens = tokenize(utterance)
    ranked = [
        _score_descriptor(utterance, query_tokens, descriptor)
        for descriptor in descriptors
    ]
    ranked.sort(key=lambda scored: (-scored.score, scored.descriptor.id))
    return ranked


def _score_descriptor(
    utterance: str, query_tokens: set[str], descriptor: CapabilityDescriptor
) -> ScoredDescriptor:
    """Score one descriptor: best lexical document match plus the domain boost."""
    documents = (descriptor.selection_text, *descriptor.examples)
    text = max((score(utterance, document) for document in documents), default=0.0)
    domain = (
        SELECTION_DOMAIN_BOOST
        if any(_domain_tokens(domain) & query_tokens for domain in descriptor.domains)
        else 0.0
    )
    return ScoredDescriptor(
        descriptor=descriptor,
        score=text + domain,
        text=text,
        domain=domain,
        resident=descriptor.resident,
    )


def _domain_tokens(domain: str) -> set[str]:
    """Tokenize a domain name so ``media_player`` matches the word ``player``."""
    return tokenize(domain.replace("_", " "))


@dataclass(slots=True, frozen=True)
class SelectionPlan:
    """Stage-4 output: the tools/instructions/context selected for one turn, plus trace.

    The ``tools`` set is what an enforcing selector *would* expose. Everything else is the
    selection trace (docs "Selection trace and observability"): why each bundle was
    present or absent, so shadow mode can explain a recall miss. ``admitted`` is the
    admitted descriptor ids in rank order; ``dependency_expansions`` records bundles
    pulled in by another's dependency closure; ``omitted`` pairs a non-admitted bundle id
    with a safe reason (``below_floor``, ``budget``, ``unavailable``); ``scores`` is the
    Stage-2 score per ranked bundle.
    """

    tools: frozenset[str]
    instructions: tuple[str, ...]
    context_loaders: tuple[str, ...]
    admitted: tuple[str, ...]
    dependency_expansions: tuple[tuple[str, str], ...]
    omitted: tuple[tuple[str, str], ...]
    scores: dict[str, float]
    tool_count: int

    def exposes(self, tool_name: str) -> bool:
        """Return whether ``tool_name`` would have been exposed by this plan."""
        return tool_name in self.tools


def assemble_plan(
    ranked: list[ScoredDescriptor],
    available: AvailableCatalog,
    *,
    budget: int = SELECTION_TOOL_BUDGET,
    floor: float = SELECTION_RELEVANCE_FLOOR,
) -> SelectionPlan:
    """Fit ranked bundles and their dependencies under the tool budget (docs "Stage 4").

    Resident bundles are admitted first and unconditionally. Then, walking high score to
    low, admit a bundle when it clears the relevance ``floor`` and its full dependency
    closure fits the remaining tool ``budget``; a bundle is never admitted half-functional,
    so a closure that would overflow the budget omits the whole bundle rather than its
    primary tool alone. Below-floor and budget-displaced bundles are recorded with a safe
    reason. Stage-1 filtered bundles carry through as ``unavailable`` omissions so the
    trace explains every catalog entry.
    """
    by_id = {scored.descriptor.id: scored for scored in ranked}
    admitted: dict[str, CapabilityDescriptor] = {}
    order: list[str] = []
    dependency_expansions: list[tuple[str, str]] = []
    omitted: list[tuple[str, str]] = []
    tools: set[str] = set()

    def admit(descriptor: CapabilityDescriptor, required_by: str | None) -> None:
        if descriptor.id in admitted:
            return
        admitted[descriptor.id] = descriptor
        order.append(descriptor.id)
        tools.update(descriptor.tools)
        if required_by is not None:
            dependency_expansions.append((descriptor.id, required_by))

    def closure(descriptor: CapabilityDescriptor) -> list[CapabilityDescriptor]:
        # The bundle plus every not-yet-admitted dependency, so budgeting counts the
        # whole functional unit. Missing dependency ids are skipped rather than raising:
        # a catalog gap should not abort selection.
        chain: list[CapabilityDescriptor] = []
        pending = [descriptor.id]
        seen: set[str] = set()
        while pending:
            current_id = pending.pop()
            if current_id in seen or current_id in admitted:
                continue
            seen.add(current_id)
            scored = by_id.get(current_id)
            if scored is None:
                continue
            chain.append(scored.descriptor)
            pending.extend(scored.descriptor.dependencies)
        return chain

    for scored in ranked:
        if scored.resident:
            admit(scored.descriptor, None)

    for scored in ranked:
        descriptor = scored.descriptor
        if descriptor.id in admitted:
            continue
        if scored.score < floor:
            omitted.append((descriptor.id, "below_floor"))
            continue
        chain = closure(descriptor)
        new_tools = set().union(*(set(item.tools) for item in chain))
        if len(tools | new_tools) > budget:
            omitted.append((descriptor.id, "budget"))
            continue
        for item in chain:
            admit(item, None if item.id == descriptor.id else descriptor.id)

    omitted.extend((item.id, "unavailable") for item in available.filtered)
    return SelectionPlan(
        tools=frozenset(tools),
        instructions=tuple(
            instruction
            for descriptor_id in order
            for instruction in admitted[descriptor_id].instructions
        ),
        context_loaders=tuple(
            loader
            for descriptor_id in order
            for loader in admitted[descriptor_id].context_loaders
        ),
        admitted=tuple(order),
        dependency_expansions=tuple(dependency_expansions),
        omitted=tuple(omitted),
        scores={scored.descriptor.id: scored.score for scored in ranked},
        tool_count=len(tools),
    )


def select_capabilities(
    utterance: str,
    exposed_tools: frozenset[str] | set[str],
    *,
    catalog: Catalog | None = None,
    budget: int = SELECTION_TOOL_BUDGET,
    floor: float = SELECTION_RELEVANCE_FLOOR,
) -> SelectionPlan:
    """Run the full per-turn selection: filter, rank, and assemble (docs "Recommended v1").

    The one entry point a consumer (today the shadow harness) calls. ``exposed_tools`` is
    the roster the running system would offer; ``catalog`` defaults to the demo catalog.
    Returns the `SelectionPlan` without applying it: Wave 1 measures it against the
    full-roster baseline before enforcement.
    """
    resolved_catalog = catalog if catalog is not None else default_catalog()
    available = available_descriptors(resolved_catalog, exposed_tools)
    ranked = rank_descriptors(utterance, available.descriptors)
    return assemble_plan(ranked, available, budget=budget, floor=floor)


__all__ = [
    "AvailableCatalog",
    "CapabilityDescriptor",
    "Catalog",
    "FilteredCapability",
    "ScoredDescriptor",
    "SelectionPlan",
    "assemble_plan",
    "available_descriptors",
    "default_catalog",
    "rank_descriptors",
    "select_capabilities",
]
