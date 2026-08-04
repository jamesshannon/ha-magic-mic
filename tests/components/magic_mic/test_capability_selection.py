"""Tests for the capability-selection catalog and availability filter (Stage 1)."""

from custom_components.magic_mic.capabilities.capability_selection import (
    CapabilityDescriptor,
    Catalog,
    available_descriptors,
    default_catalog,
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
