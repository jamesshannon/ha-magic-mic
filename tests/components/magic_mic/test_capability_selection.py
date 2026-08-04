"""Tests for the capability-selection catalog, filter, retrieval, and assembler."""

from custom_components.magic_mic.capabilities.capability_selection import (
    CapabilityDescriptor,
    Catalog,
    action_descriptor,
    assemble_plan,
    available_descriptors,
    default_catalog,
    extend_catalog,
    rank_descriptors,
    select_capabilities,
)

# The tools the Wave-0 fixture home exposes to the model.
_ROSTER = frozenset(
    {
        "GetDateTime",
        "GetLiveContext",
        "HassCancelTimer",
        "HassClimateGetTemperature",
        "HassClimateSetTemperature",
        "HassGetWeather",
        "HassLightSet",
        "HassListAddItem",
        "HassListCompleteItem",
        "HassMediaNext",
        "HassMediaPause",
        "HassMediaPrevious",
        "HassMediaUnpause",
        "HassPauseTimer",
        "HassSetPosition",
        "HassSetVolume",
        "HassStartTimer",
        "HassTimerStatus",
        "HassToggle",
        "HassTurnOff",
        "HassTurnOn",
        "HassUnpauseTimer",
        "find_entities",
        "todo_get_items",
    }
)


def test_catalog_indexes_by_id_and_tool() -> None:
    """Every descriptor is reachable by id, and every tool maps back to its bundle."""
    catalog = default_catalog()

    assert catalog.by_id["device_control"].tools[0] == "HassTurnOn"
    assert catalog.by_tool["HassTurnOn"] == "device_control"
    assert catalog.by_tool["find_entities"] == "find_entities"
    # No tool is claimed by two bundles: shadow scoring needs one owner per tool.
    owners = [tool for descriptor in catalog.descriptors for tool in descriptor.tools]
    assert len(owners) == len(set(owners))


def test_catalog_covers_the_wave0_corpus_tools() -> None:
    """The demo catalog can name every tool the Wave-0 baseline actually used.

    A tool the corpus exercises but no descriptor declares would silently look like a
    retrieval miss in shadow mode, so pin the coverage here.
    """
    catalog = default_catalog()
    observed = {
        "GetDateTime",
        "GetLiveContext",
        "HassLightSet",
        "HassListAddItem",
        "HassMediaPause",
        "HassSetPosition",
        "HassSetVolume",
        "HassStartTimer",
        "HassTurnOff",
        "HassTurnOn",
    }
    assert observed <= catalog.tool_names()


def test_availability_filter_drops_absent_bundles() -> None:
    """A descriptor with no exposed tool is filtered with a safe reason code."""
    catalog = default_catalog()

    # A home with only lighting exposed: everything else should filter out.
    result = available_descriptors(catalog, {"HassTurnOn", "HassTurnOff", "HassToggle"})

    kept_ids = {descriptor.id for descriptor in result.descriptors}
    assert kept_ids == {"device_control"}
    filtered_ids = {item.id for item in result.filtered}
    assert "covers" in filtered_ids
    assert all(item.reason == "unavailable" for item in result.filtered)


def test_availability_filter_projects_to_exposed_subset() -> None:
    """A partly-exposed bundle keeps only the tools the system actually offers."""
    catalog = Catalog(
        (
            CapabilityDescriptor(
                id="media",
                selection_text="control playback",
                tools=("HassMediaPause", "HassMediaUnpause", "HassMediaNext"),
            ),
        )
    )

    # Only pause is exposed (a player without next/prev support).
    result = available_descriptors(catalog, {"HassMediaPause"})

    assert len(result.descriptors) == 1
    assert result.descriptors[0].tools == ("HassMediaPause",)
    assert result.filtered == ()


def test_ranking_puts_the_relevant_bundle_first() -> None:
    """A direct request ranks its owning bundle at the top of retrieval."""
    catalog = default_catalog()
    available = available_descriptors(catalog, _ROSTER)

    ranked = rank_descriptors("add milk to my shopping list", available.descriptors)

    assert ranked[0].descriptor.id == "lists"
    assert ranked[0].score > ranked[-1].score


def test_idf_ignores_shared_words_and_ranks_on_the_discriminator() -> None:
    """A stopword shared with the query does not lift a bundle; a rare content word does.

    "turn off the living room lamp" shares "turn"/"the" with several bundles, but only the
    device-control and light bundles carry the discriminating words, so the thermostat and
    volume bundles must not outrank them the way the raw token-set scorer let them.
    """
    catalog = default_catalog()
    available = available_descriptors(catalog, _ROSTER)

    ranked = rank_descriptors("turn off the living room lamp", available.descriptors)
    rank = {scored.descriptor.id: position for position, scored in enumerate(ranked)}

    assert rank["device_control"] < rank["climate"]
    assert rank["device_control"] < rank["volume"]


def test_out_of_vocabulary_word_does_not_flatten_scores() -> None:
    """A query word no bundle knows is ignored, so a real match still scores high."""
    catalog = default_catalog()
    available = available_descriptors(catalog, _ROSTER)

    # "xyzzy" is in no descriptor; the volume signal should still dominate.
    ranked = rank_descriptors("set the xyzzy volume", available.descriptors)

    assert ranked[0].descriptor.id == "volume"
    assert ranked[0].score > 50.0


def test_plan_exposes_the_used_tool_across_the_corpus() -> None:
    """At the default budget the plan exposes the tool each request needs."""
    for utterance, used in (
        ("turn off the living room lamp", "HassTurnOff"),
        ("what time is it?", "GetDateTime"),
        ("set a timer for five minutes", "HassStartTimer"),
        ("add milk to my shopping list", "HassListAddItem"),
        ("dim the bedroom lights", "HassLightSet"),
        ("open the garage door", "HassSetPosition"),
        ("is the garage door open?", "GetLiveContext"),
        ("pause the music", "HassMediaPause"),
        ("set the volume to twenty percent", "HassSetVolume"),
    ):
        plan = select_capabilities(utterance, _ROSTER)
        assert plan.exposes(used), f"{utterance!r} did not expose {used}"


def test_residents_are_admitted_regardless_of_budget() -> None:
    """Resident reads survive even a budget too small to hold them."""
    plan = select_capabilities("random unrelated words", _ROSTER, budget=1)

    assert "live_context" in plan.admitted
    assert "datetime" in plan.admitted
    assert plan.exposes("GetLiveContext")


def test_budget_prunes_the_lowest_ranked_bundles() -> None:
    """A budget smaller than the roster omits low-ranked bundles with a reason."""
    # Residents (GetLiveContext, GetDateTime) plus the lists bundle fill five slots.
    plan = select_capabilities("add milk to my shopping list", _ROSTER, budget=5)

    assert plan.exposes("HassListAddItem")
    assert plan.tool_count <= 5
    assert not plan.exposes("HassSetPosition")
    assert any(reason == "budget" for _, reason in plan.omitted)


def test_below_floor_bundles_are_omitted() -> None:
    """With a high floor only the paraphrased bundle survives on relevance."""
    plan = select_capabilities("add milk to my shopping list", _ROSTER, floor=90.0)

    assert "lists" in plan.admitted
    assert any(reason == "below_floor" for _, reason in plan.omitted)


def test_dependency_closure_pulls_in_a_below_floor_dependency() -> None:
    """A bundle admitted on relevance drags in its dependency, floor notwithstanding."""
    catalog = Catalog(
        (
            CapabilityDescriptor(
                id="reminders",
                selection_text="remind me later, conditional reminders and alarms",
                tools=("create_reminder",),
                examples=("remind me tomorrow if the door is open",),
                dependencies=("find_entities",),
            ),
            CapabilityDescriptor(
                id="find_entities",
                selection_text="look up a device by approximate name",
                tools=("find_entities",),
            ),
        )
    )
    exposed = {"create_reminder", "find_entities"}

    # A high floor would drop find_entities on its own relevance to this utterance.
    plan = select_capabilities(
        "remind me tomorrow if the door is open",
        exposed,
        catalog=catalog,
        floor=90.0,
    )

    assert plan.exposes("create_reminder")
    assert plan.exposes("find_entities")
    assert ("find_entities", "reminders") in plan.dependency_expansions


def test_unavailable_bundles_carry_into_the_trace() -> None:
    """A Stage-1 filtered bundle is recorded as an unavailable omission."""
    # A lighting-only home: covers, media, and the rest are unavailable.
    plan = select_capabilities("turn on the lights", {"HassTurnOn", "HassTurnOff"})

    assert ("covers", "unavailable") in plan.omitted
    assert not plan.exposes("HassSetPosition")


def test_action_descriptor_names_the_tool_by_object_id() -> None:
    """A script becomes a single-tool descriptor whose retrieval doc is its name/area."""
    descriptor = action_descriptor(
        "movie_night", "Movie Night", aliases=("film mode",), area="living room"
    )

    assert descriptor.id == "script:movie_night"
    assert descriptor.tools == ("movie_night",)
    assert descriptor.keywords == ("film mode", "living room")


def test_script_retrieval_discriminates_among_similar_scripts() -> None:
    """A named request picks the right script out of a collection of near-namesakes."""
    scripts = (
        action_descriptor("movie_night", "Movie Night", area="living room"),
        action_descriptor("game_night", "Game Night", area="living room"),
        action_descriptor("good_morning", "Good Morning", area="bedroom"),
        action_descriptor("good_night", "Good Night", area="bedroom"),
    )
    catalog = extend_catalog(default_catalog(), scripts)
    roster = catalog.tool_names()

    plan = select_capabilities("start movie night", roster, catalog=catalog, budget=8)

    assert plan.exposes("movie_night")
    # The near-namesake must not outrank the requested script.
    assert plan.scores["script:movie_night"] > plan.scores["script:game_night"]


def test_extend_catalog_indexes_the_added_tools() -> None:
    """Added action tools are reachable by tool name for shadow scoring."""
    catalog = extend_catalog(
        default_catalog(), (action_descriptor("movie_night", "Movie Night"),)
    )

    assert catalog.by_tool["movie_night"] == "script:movie_night"
    assert "movie_night" in catalog.tool_names()


def test_assemble_is_deterministic_on_score_ties() -> None:
    """Equal scores break on descriptor id, so a plan is reproducible."""
    catalog = default_catalog()
    available = available_descriptors(catalog, _ROSTER)
    ranked = rank_descriptors("hello there", available.descriptors)

    first = assemble_plan(ranked, available)
    second = assemble_plan(ranked, available)

    assert first.admitted == second.admitted
