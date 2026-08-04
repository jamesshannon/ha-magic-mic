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

from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
import math

from ..const import SELECTION_RELEVANCE_FLOOR, SELECTION_TOOL_BUDGET
from ..fuzzy import tokenize


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
    # Extra retrieval tokens that are not full sentences: a script's area, an alias, a
    # synonym. Pooled into the bundle's document alongside selection_text and examples, so
    # "start the living room movie thing" can reach a "Movie Night" script placed there.
    keywords: tuple[str, ...] = ()
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


def action_descriptor(
    tool_name: str,
    name: str,
    *,
    domain: str = "script",
    aliases: tuple[str, ...] = (),
    description: str | None = None,
    area: str | None = None,
) -> CapabilityDescriptor:
    """Build a single-tool descriptor for an individually-retrieved action.

    Scripts, and any other per-entity action a home exposes, arrive as one tool each named
    by their object id (`script.movie_night` becomes the tool `movie_night`). Unlike the
    curated bundles, they have no author-written selection text, so the descriptor's
    retrieval document is drawn from the entity's own configured metadata (docs "Bundles
    and tools": scripts need "individual retrieval by name, description, domain, and
    area"):

    - ``name`` is the primary selection text;
    - ``aliases`` are the home's own voice triggers and alternate names, the phrases a user
      actually says, so they are the strongest bridge from an oblique request to the script;
    - ``description`` is the script's configured description (already its tool description
      in HA), added as a retrieval sentence;
    - ``area`` is where it sits.

    This is the collection the tool budget actually has to choose within, so each is its own
    selectable unit rather than one giant bundle.
    """
    return CapabilityDescriptor(
        id=f"{domain}:{tool_name}",
        selection_text=name,
        tools=(tool_name,),
        examples=(description,) if description else (),
        keywords=(*aliases, *((area,) if area else ())),
        domains=(domain,),
    )


def extend_catalog(base: Catalog, extra: tuple[CapabilityDescriptor, ...]) -> Catalog:
    """Return a catalog of ``base`` plus ``extra`` descriptors (for example, a home's scripts)."""
    return Catalog((*base.descriptors, *extra))


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
                    keywords=descriptor.keywords,
                    instructions=descriptor.instructions,
                    context_loaders=descriptor.context_loaders,
                    dependencies=descriptor.dependencies,
                    resident=descriptor.resident,
                )
            )
    return AvailableCatalog(tuple(kept), tuple(filtered))


@dataclass(slots=True, frozen=True)
class ScoredDescriptor:
    """A descriptor with its Stage-2 relevance score.

    ``score`` is a ranking device on a 0-100 scale, not a calibrated probability (docs
    "Stage 2"): the IDF-weighted share of the request's retrievable content that this
    bundle's retrieval document covers. ``resident`` marks a bundle the assembler admits
    regardless of where it ranks.
    """

    descriptor: CapabilityDescriptor
    score: float
    resident: bool


def rank_descriptors(
    utterance: str, descriptors: tuple[CapabilityDescriptor, ...]
) -> list[ScoredDescriptor]:
    """Rank available descriptors by relevance to ``utterance`` (docs "Stage 2").

    High-recall lexical retrieval weighted by inverse document frequency across the
    catalog. Each bundle's retrieval document is the pooled tokens of its
    ``selection_text``, examples, and domains (via the shared Unicode-aware tokenizer). A
    token's weight is its IDF over the available bundles, so a word common to many bundles
    (a stopword, or "turn" and "set" shared by half the catalog) counts for little while a
    discriminating content word ("thermostat", "volume", "lamp") decides. A bundle scores
    the IDF-weighted share of the request's *retrievable* tokens it covers, ignoring query
    words no bundle knows so an out-of-vocabulary name cannot flatten every score together.
    This removes the short-document stopword inflation the first shadow run surfaced
    without any language-specific stopword list. Ties break on descriptor id.
    """
    query_tokens = tokenize(utterance)
    documents = {
        descriptor.id: _bundle_tokens(descriptor) for descriptor in descriptors
    }
    document_frequency: Counter[str] = Counter()
    for tokens in documents.values():
        document_frequency.update(tokens)
    total = len(documents)

    def idf(token: str) -> float:
        return math.log(1 + total / document_frequency[token])

    # Only query tokens some bundle actually carries are retrievable; an unknown word
    # cannot rank anything and would only shrink every score by the same denominator.
    retrievable = {token for token in query_tokens if document_frequency[token]}
    ranked = [
        ScoredDescriptor(
            descriptor=descriptor,
            score=_idf_coverage(retrievable, documents[descriptor.id], idf),
            resident=descriptor.resident,
        )
        for descriptor in descriptors
    ]
    ranked.sort(key=lambda scored: (-scored.score, scored.descriptor.id))
    return ranked


def _idf_coverage(
    retrievable: set[str], bundle_tokens: set[str], idf: Callable[[str], float]
) -> float:
    """Return the IDF-weighted share of ``retrievable`` the bundle covers, in ``[0, 100]``."""
    total = sum(idf(token) for token in retrievable)
    if not total:
        return 0.0
    matched = sum(idf(token) for token in retrievable if token in bundle_tokens)
    return 100.0 * matched / total


def _bundle_tokens(descriptor: CapabilityDescriptor) -> set[str]:
    """Pool a bundle's retrieval tokens: selection text, examples, keywords, domains."""
    tokens = tokenize(descriptor.selection_text)
    for example in descriptor.examples:
        tokens |= tokenize(example)
    for keyword in descriptor.keywords:
        tokens |= tokenize(keyword)
    for domain in descriptor.domains:
        # Split so media_player contributes the word "player".
        tokens |= tokenize(domain.replace("_", " "))
    return tokens


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
    "action_descriptor",
    "assemble_plan",
    "available_descriptors",
    "default_catalog",
    "extend_catalog",
    "rank_descriptors",
    "select_capabilities",
]
