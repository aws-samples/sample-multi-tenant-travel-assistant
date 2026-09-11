"""Citation deduplication — no AWS, no network.

`facts.passages` is model context and never reaches the frontend; only cards cross the stream
boundary. `_deduplicated_citations` turns passages into one clickable card per document. Duplicate
cards fail silently as repeated tiles, so the behavior is pinned here.
"""

from __future__ import annotations

from shared.cards import CardType, assert_valid, card
from tools.knowledge.handler import _deduplicated_citations

GLOBEX_CAP = {
    "text": "cap text",
    "citation": {"label": "Globex policy", "doc_id": "pol_globex_2026"},
}
GLOBEX_CABIN = {
    "text": "cabin text",
    "citation": {"label": "Globex policy", "doc_id": "pol_globex_2026"},
}
INITECH_CAP = {
    "text": "initech cap",
    "citation": {"label": "Initech policy", "doc_id": "pol_initech_2026"},
}
NO_DOC_ID = {"text": "orphan passage", "citation": {"label": "Untitled", "doc_id": None}}


class TestDeduplication:
    def test_one_document_cited_twice_becomes_one_card(self):
        """A cap rule and a cabin rule from the same policy file must not double the tile."""
        kept = _deduplicated_citations([GLOBEX_CAP, GLOBEX_CABIN])
        assert kept == [GLOBEX_CAP]

    def test_first_occurrence_wins(self):
        """The most relevant passage decides the card's position — retrieval already ranked
        these."""
        kept = _deduplicated_citations([GLOBEX_CABIN, GLOBEX_CAP])
        assert kept == [GLOBEX_CABIN]

    def test_two_documents_produce_two_cards_in_order(self):
        kept = _deduplicated_citations([GLOBEX_CAP, INITECH_CAP])
        assert kept == [GLOBEX_CAP, INITECH_CAP]

    def test_a_passage_with_no_doc_id_is_dropped(self):
        """No id means nothing to presign — a card here would be a button that 404s on click."""
        kept = _deduplicated_citations([NO_DOC_ID, GLOBEX_CAP])
        assert kept == [GLOBEX_CAP]

    def test_empty_input_produces_no_cards(self):
        assert _deduplicated_citations([]) == []

    def test_all_orphaned_produces_no_cards(self):
        assert _deduplicated_citations([NO_DOC_ID]) == []


class TestCardContract:
    """Build the exact citation card and validate it against `shared/cards.py`."""

    def test_a_citation_built_from_a_passage_satisfies_the_card_contract(self):
        for passage in (GLOBEX_CAP, INITECH_CAP):
            built = card(
                CardType.CITATION,
                f"citation-{passage['citation']['doc_id']}",
                passage["citation"],
            )
            assert_valid(built)  # raises CardContractError on failure

    def test_a_citation_with_no_version_metadata_still_satisfies_the_contract(self):
        """`_citation()` sends `version: metadata.get('version')`, which is `None` for a document
        with no declared version — `REQUIRED_DATA` checks key presence, not truthiness, and this
        pins that a `None` value does not fail validation the way a missing key would."""
        built = card(
            CardType.CITATION,
            "citation-pol_globex_2026",
            {"label": "Globex policy", "doc_id": "pol_globex_2026", "version": None},
        )
        assert_valid(built)
