"""Build capability retrieval documents from Home Assistant's own intent sentences.

`capability-selection.md` "Localization" blocks lexical selection from gating a live
request while the catalog's retrieval documents are hand-authored English. This module
tests whether that blocker can be retired with data Home Assistant already ships rather
than a hand-translated catalog: `home_assistant_intents` carries the HASSIL sentence
templates for 60 languages, keyed by intent name, which is exactly "the phrases a person
says in this language to trigger this capability".

It is measurement, not shipped behavior. If the comparison holds, moving the builder into
`capabilities/capability_selection.py` is Wave 2 work; until then the component keeps its
English demo catalog and the enforcement flag stays off.

Documents are built by *rendering* templates, not by harvesting their text chunks. German
templates express inflection as optional suffixes (`dreh[e]`), so chunk harvesting yields
stems the exact-token scorer cannot match; rendering yields real surface forms. Rendering
is capped per template, so a template with a large slot list contributes its first
`SAMPLES_PER_TEMPLATE` renderings rather than its full product.

    .venv/bin/python -m evals.harness.localized_catalog --compare
    .venv/bin/python -m evals.harness.localized_catalog --language de --held-out
"""

import argparse
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
import itertools
import json
from pathlib import Path

from hassil import parse_sentence
from hassil.errors import HassilError
from hassil.intents import TextSlotList
from hassil.sample import sample_expression
import home_assistant_intents as intents_package

from custom_components.magic_mic.capabilities.capability_selection import (
    CapabilityDescriptor,
    Catalog,
    default_catalog,
    select_capabilities,
)
from custom_components.magic_mic.fuzzy import tokenize

from .baseline import RESULTS_DIR, write_artifact
from .corpus import CORPUS_DIR, load_corpus
from .selection_shadow import (
    CaseTools,
    build_shadow_artifact,
    catalog_for_world,
    load_case_tools,
    load_case_tools_from_corpus,
)

LOCALIZED_ARTIFACT = RESULTS_DIR / "wave1_localized_catalog.json"
# Renderings kept per sentence template. A template referencing a large slot list has a
# combinatorial product; the vocabulary converges long before the product is exhausted.
SAMPLES_PER_TEMPLATE = 24
# Placeholder values for the slot lists a home supplies at runtime. Entity and area names
# belong to the household, not to the language, and `action_descriptor` already retrieves
# them; substituting a neutral placeholder keeps them out of the bundle vocabulary.
_HOME_SLOT_PLACEHOLDER = ""


class LocalizedCatalogError(Exception):
    """A localized catalog could not be built for the requested language."""


class _StubbedSlotLists(dict):
    """Slot lists that yield a placeholder for any list the home would supply.

    `sample_expression` raises on an unknown list, but `{name}` and `{area}` have no
    language-level vocabulary: they are the household's own entity and area names. Every
    unrequested list resolves to a single empty value so the rendering keeps the template's
    language and drops its home-specific slot.
    """

    def __contains__(self, key: object) -> bool:
        """Report every list as present so rendering never raises on a home slot."""
        return True

    def __missing__(self, key: str) -> TextSlotList:
        """Create and cache a placeholder list for a home-supplied slot."""
        stub = TextSlotList.from_strings([_HOME_SLOT_PLACEHOLDER])
        self[key] = stub
        return stub


@dataclass(frozen=True)
class LanguageIntents:
    """One language's parsed sentence templates, expansion rules, and slot lists."""

    language: str
    expansion_rules: dict
    slot_lists: _StubbedSlotLists
    sentences: dict[str, tuple[str, ...]]


def load_language(language: str) -> LanguageIntents:
    """Load and index one language's shipped intent sentences.

    Raises `LocalizedCatalogError` when Home Assistant ships no intents for the language,
    which is the honest failure mode: a locale with no upstream sentences cannot have a
    catalog derived this way and would need translated retrieval documents of its own.
    """
    if language not in intents_package.get_languages():
        raise LocalizedCatalogError(f"no shipped intents for language {language!r}")
    data = intents_package.get_intents(language)
    if not data:
        raise LocalizedCatalogError(f"empty intent data for language {language!r}")

    slot_lists = _StubbedSlotLists()
    for name, spec in data.get("lists", {}).items():
        values = spec.get("values")
        if not values:
            continue
        try:
            slot_lists[name] = TextSlotList.from_strings(
                [value["in"] if isinstance(value, dict) else value for value in values]
            )
        except (AttributeError, HassilError, KeyError, TypeError):
            # A range or wildcard list has no enumerable text; the stub covers it.
            continue

    sentences = {
        intent_name: tuple(
            template
            for block in intent.get("data", [])
            for template in block.get("sentences", [])
        )
        for intent_name, intent in data.get("intents", {}).items()
    }
    return LanguageIntents(
        language=language,
        expansion_rules={
            name: parse_sentence(body)
            for name, body in data.get("expansion_rules", {}).items()
        },
        slot_lists=slot_lists,
        sentences=sentences,
    )


def render_template(language: LanguageIntents, template: str) -> Iterator[str]:
    """Yield up to `SAMPLES_PER_TEMPLATE` concrete sentences for one template."""
    try:
        expression = parse_sentence(template).expression
    except HassilError:
        return
    try:
        yield from itertools.islice(
            sample_expression(
                expression,
                slot_lists=language.slot_lists,
                expansion_rules=language.expansion_rules,
            ),
            SAMPLES_PER_TEMPLATE,
        )
    except (HassilError, RecursionError):
        # A template we cannot render contributes nothing rather than failing the build.
        return


def intent_vocabulary(
    language: LanguageIntents,
    intent_name: str,
    *,
    keep: Callable[[int], bool] | None = None,
) -> tuple[str, ...]:
    """Return the rendered vocabulary for one intent, deduplicated and ordered.

    `keep` selects which of the intent's templates contribute, by index. It exists for the
    held-out split: build the document from one half of a language's phrasings and test
    retrieval with the other half, so the measurement is not the document matching itself.
    """
    templates = language.sentences.get(intent_name, ())
    seen: dict[str, None] = {}
    for index, template in enumerate(templates):
        if keep is not None and not keep(index):
            continue
        for rendering in render_template(language, template):
            for word in rendering.split():
                seen.setdefault(word.strip().lower(), None)
    return tuple(seen)


def localized_descriptor(
    descriptor: CapabilityDescriptor,
    language: LanguageIntents,
    *,
    keep: Callable[[int], bool] | None = None,
) -> tuple[CapabilityDescriptor, bool]:
    """Rebuild one descriptor's retrieval document from upstream sentences.

    Returns the descriptor and whether it was actually derived. Tools with no upstream
    sentences (`find_entities`, `GetLiveContext`, `GetDateTime`, `todo_get_items`) keep
    their authored document: those are Magic Mic's own capabilities, whose localization
    runs through `strings.json` like every other user-facing string, not through the
    intents package.
    """
    vocabulary: list[str] = []
    for tool in descriptor.tools:
        vocabulary.extend(intent_vocabulary(language, tool, keep=keep))
    if not vocabulary:
        return descriptor, False
    unique = tuple(dict.fromkeys(vocabulary))
    return (
        replace(descriptor, selection_text=" ".join(unique), examples=()),
        True,
    )


def localized_catalog(
    catalog: Catalog,
    language: LanguageIntents,
    *,
    keep: Callable[[int], bool] | None = None,
) -> tuple[Catalog, tuple[str, ...]]:
    """Return `catalog` with upstream-derived documents, plus the ids left authored.

    Script descriptors are passed through untouched: their documents are already the
    household's own name, aliases, and description, so they are in the home's language
    without any of this machinery.
    """
    rebuilt: list[CapabilityDescriptor] = []
    authored: list[str] = []
    for descriptor in catalog.descriptors:
        new_descriptor, derived = localized_descriptor(descriptor, language, keep=keep)
        rebuilt.append(new_descriptor)
        if not derived:
            authored.append(descriptor.id)
    return Catalog(tuple(rebuilt)), tuple(authored)


@dataclass(frozen=True)
class HeldOutCase:
    """One rendered utterance from the held-out half of a language's templates."""

    id: str
    utterance: str
    expected: str


def held_out_cases(
    language: LanguageIntents,
    intent_names: Sequence[str],
    *,
    per_intent: int = 4,
    keep: Callable[[int], bool] = lambda index: index % 2 == 0,
) -> tuple[HeldOutCase, ...]:
    """Render test utterances the document build did not see.

    `keep` selects the build half by template index; cases come from the rest. Excluding
    templates is not enough on its own, because two templates can render the same
    sentence (one template's optional word produces what another writes literally), so
    any rendering the build half also produced is dropped. What survives is genuinely
    unseen *as a sentence*.

    It is still not disjoint by vocabulary: an intent's phrasings reuse the same words, so
    the build half has usually seen most held-out tokens already. `held_out_novelty`
    measures that, and it is the ceiling on what a high recall here can claim, namely that
    the derived document covers this language's vocabulary for this intent, not that it
    generalizes to unseen wording. The different-speaker question is the out-of-vocabulary
    regime in `wave1_scripts.yaml`, and no rendering of upstream templates can stand in
    for it.
    """
    cases: list[HeldOutCase] = []
    for intent_name in intent_names:
        templates = language.sentences.get(intent_name, ())
        build_half = {
            " ".join(rendering.split())
            for index, template in enumerate(templates)
            if keep(index)
            for rendering in render_template(language, template)
        }
        rendered: list[str] = []
        for index, template in enumerate(templates):
            if keep(index):
                continue
            for sentence in render_template(language, template):
                text = " ".join(sentence.split())
                if len(text.split()) >= 2 and text not in rendered:
                    if text not in build_half:
                        rendered.append(text)
            if len(rendered) >= per_intent:
                break
        for position, text in enumerate(rendered[:per_intent]):
            cases.append(
                HeldOutCase(
                    id=f"{intent_name}-{position}",
                    utterance=text,
                    expected=intent_name,
                )
            )
    return tuple(cases)


def held_out_novelty(
    cases: Sequence[HeldOutCase],
    language: LanguageIntents,
    *,
    keep: Callable[[int], bool],
) -> dict:
    """Measure how much of the held-out utterances the build half had not already seen.

    This is the honesty bound on the held-out recall. If novelty is near zero, a perfect
    score says the derived document contains the language's words for these intents, which
    is enough to refute an English document's cross-language collapse but is not evidence
    that retrieval survives wording nobody upstream wrote down.
    """
    seen_by_intent = {
        intent_name: set(intent_vocabulary(language, intent_name, keep=keep))
        for intent_name in {case.expected for case in cases}
    }
    tokens_total = 0
    tokens_novel = 0
    cases_with_novel = 0
    for case in cases:
        tokens = set(tokenize(case.utterance))
        novel = tokens - seen_by_intent[case.expected]
        tokens_total += len(tokens)
        tokens_novel += len(novel)
        cases_with_novel += bool(novel)
    return {
        "cases_with_a_novel_token": cases_with_novel,
        "novel_token_share": (
            round(tokens_novel / tokens_total, 4) if tokens_total else 0.0
        ),
        "tokens_novel": tokens_novel,
        "tokens_total": tokens_total,
    }


def _document_tokens(descriptor: CapabilityDescriptor) -> set[str]:
    """Return the retrieval tokens the scorer pools for one descriptor."""
    return set(
        tokenize(
            " ".join(
                (
                    descriptor.selection_text,
                    *descriptor.examples,
                    *descriptor.keywords,
                    *descriptor.domains,
                )
            )
        )
    )


def score_held_out(cases: Sequence[HeldOutCase], catalog: Catalog, budget: int) -> dict:
    """Return recall, exposure, and the tokens that carried each hit.

    `carrying_tokens` is what keeps a cross-language number honest. An English catalog
    scores above zero on German input through loanwords (`Timer`, `Position`, `Track`) and
    the occasional short word that exists in both languages, not through any understanding
    of the request. Reporting which tokens produced the hits separates real coverage from
    coincidence. `residents_only` counts the cases whose plan collapsed to the resident
    reads, which is what a live request would experience as its capability disappearing.
    """
    roster = catalog.tool_names()
    covered: list[str] = []
    missed: list[str] = []
    exposed: list[int] = []
    carrying: dict[str, int] = {}
    residents = sum(
        len(descriptor.tools)
        for descriptor in catalog.descriptors
        if descriptor.resident
    )
    for case in cases:
        plan = select_capabilities(
            case.utterance, roster, catalog=catalog, budget=budget
        )
        exposed.append(plan.tool_count)
        if not plan.exposes(case.expected):
            missed.append(case.id)
            continue
        covered.append(case.id)
        owner = catalog.by_id[catalog.by_tool[case.expected]]
        for token in set(tokenize(case.utterance)) & _document_tokens(owner):
            carrying[token] = carrying.get(token, 0) + 1
    total = len(cases)
    return {
        "budget": budget,
        "cases": total,
        "covered": len(covered),
        "recall": round(len(covered) / total, 4) if total else 0.0,
        "avg_exposed_tools": round(sum(exposed) / total, 2) if total else 0.0,
        "residents_only": sum(1 for count in exposed if count <= residents),
        "carrying_tokens": dict(
            sorted(carrying.items(), key=lambda item: (-item[1], item[0]))
        ),
        "misses": missed,
    }


def _compare_english(budgets: Sequence[int]) -> dict:
    """Score the authored and derived English catalogs on both Wave 1 corpora.

    Both regimes are reported for the script corpus, because the in/out-of-vocabulary
    split is where a document change shows its real effect: an aggregate can rise on
    in-vocabulary cases while the out-of-vocabulary number that gates enforcement falls.
    """
    language = load_language("en")
    baseline = json.loads((RESULTS_DIR / "wave0_baseline.json").read_text())
    scripts = load_corpus(CORPUS_DIR / "wave1_scripts.yaml")

    sources: tuple[tuple[str, Sequence[CaseTools], Catalog], ...] = (
        ("wave0_golden_set", load_case_tools(baseline), default_catalog()),
        (
            "wave1_scripts",
            load_case_tools_from_corpus(scripts),
            catalog_for_world(scripts.world),
        ),
    )
    results: dict[str, dict] = {}
    for name, cases, base in sources:
        derived, authored = localized_catalog(base, language)
        authored_artifact = build_shadow_artifact(Path(name), base, cases, budgets)
        derived_artifact = build_shadow_artifact(Path(name), derived, cases, budgets)
        results[name] = {
            "authored": authored_artifact["recall"],
            "authored_by_phrasing": authored_artifact["recall_by_phrasing"],
            "derived": derived_artifact["recall"],
            "derived_by_phrasing": derived_artifact["recall_by_phrasing"],
            "authored_bundles_kept": list(authored),
        }
    return results


def render_report(artifact: dict) -> str:
    """Render the comparison as a plain-text block."""
    run = artifact["run"]
    lines = [
        (
            f"Localized retrieval documents (language {run['language']}, "
            f"{run['samples_per_template']} renderings/template)"
        ),
        "",
        "  English corpora, authored document vs derived document:",
    ]
    for corpus, result in artifact["english_comparison"].items():
        lines.append(f"    {corpus}")
        for budget in run["budgets"]:
            authored = result["authored"][str(budget)]
            derived = result["derived"][str(budget)]
            lines.append(
                f"      budget {budget:>3}  authored {authored['case_recall']:>6.0%} "
                f"({authored['avg_exposed_tools']:>5} exposed)   "
                f"derived {derived['case_recall']:>6.0%} "
                f"({derived['avg_exposed_tools']:>5} exposed)"
            )
        for regime, by_budget in result["authored_by_phrasing"].items():
            budget = run["budgets"][0]
            authored = by_budget[str(budget)]
            derived = result["derived_by_phrasing"][regime][str(budget)]
            lines.append(
                f"      {regime} @{budget}: authored {authored['case_recall']:.0%}, "
                f"derived {derived['case_recall']:.0%} "
                f"({authored['cases_total']} cases)"
            )

    held = artifact["localized_held_out"]
    lines += [
        "",
        (
            f"  {held['language']} held-out utterances ({held['held_out_cases']} cases, "
            f"budget {held['authored_english_catalog']['budget']}):"
        ),
    ]
    for label, key in (
        ("authored English catalog", "authored_english_catalog"),
        ("derived localized catalog", "derived_localized_catalog"),
    ):
        result = held[key]
        lines.append(
            f"    {label:<26} recall {result['recall']:>6.0%}  "
            f"{result['avg_exposed_tools']:>5} tools exposed  "
            f"{result['residents_only']} cases left with residents only"
        )
    carrying = held["authored_english_catalog"]["carrying_tokens"]
    if carrying:
        lines += [
            "",
            (
                "    tokens carrying the English catalog's cross-language hits "
                "(loanwords and coincidence, not coverage):"
            ),
            "      "
            + ", ".join(f"{token} x{count}" for token, count in carrying.items()),
        ]
    novelty = held["held_out_novelty"]
    lines += [
        "",
        (
            f"    honesty bound: {novelty['novel_token_share']:.0%} of held-out tokens "
            f"were unseen by the build half "
            f"({novelty['cases_with_a_novel_token']}/{held['held_out_cases']} cases "
            f"carry any novel token), so a high derived recall reads as vocabulary "
            f"coverage, not generalization"
        ),
        "",
        "    bundles with no upstream sentences, document left authored: "
        + ", ".join(held["authored_bundles_kept"]),
    ]
    return "\n".join(lines)


def main() -> None:
    """Run the localized-document comparison and write its artifact."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--language", default="de")
    parser.add_argument("--budgets", default="8,12,24")
    parser.add_argument("--artifact", type=Path, default=LOCALIZED_ARTIFACT)
    args = parser.parse_args()
    budgets = tuple(int(value) for value in args.budgets.split(","))

    english = _compare_english(budgets)

    # The cliff and its fix, in the requested language: build the document from half the
    # templates, then score the other half against the authored English catalog and the
    # derived one.
    target = load_language(args.language)
    derived, authored_ids = localized_catalog(
        default_catalog(), target, keep=lambda index: index % 2 == 0
    )
    intent_names = [
        name
        for descriptor in default_catalog().descriptors
        for name in descriptor.tools
        if target.sentences.get(name)
    ]
    cases = held_out_cases(target, intent_names)
    budget = budgets[0]
    localized = {
        "language": args.language,
        "held_out_cases": len(cases),
        "held_out_novelty": held_out_novelty(
            cases, target, keep=lambda index: index % 2 == 0
        ),
        "authored_english_catalog": score_held_out(cases, default_catalog(), budget),
        "derived_localized_catalog": score_held_out(cases, derived, budget),
        "authored_bundles_kept": list(authored_ids),
    }

    artifact = {
        "run": {
            "kind": "wave1-localized-catalog",
            "timestamp": datetime.now(UTC).isoformat(),
            "budgets": list(budgets),
            "samples_per_template": SAMPLES_PER_TEMPLATE,
            "language": args.language,
        },
        "english_comparison": english,
        "localized_held_out": localized,
    }
    print(render_report(artifact))
    written = write_artifact(artifact, args.artifact)
    print(f"\nartifact: {written}")


if __name__ == "__main__":
    main()
