"""Seed-data tests.

Two things must hold for the demo to work: the fixtures load, and the numbers
behind the headline scenarios are actually correct — Priya really does have three
prior international trips, the two tenants really are opposite, and no read
crosses a tenant boundary.
"""

import sys
from datetime import date
from pathlib import Path

from app.eligibility import count_international_trips
from app.models import BookingMode, CabinClass, Currency, PolicyStatus, TripStatus
from seed import (
    ADAEZE_ID,
    PRIYA_ID,
    SAM_ADEWALE_ID,
    SAM_OKONJO_ID,
    SAM_WHITFIELD_ID,
    seeded_repository,
)
from seed.tenants import GLOBEX, INITECH


class TestLoads:
    def test_both_tenants_present(self):
        repo = seeded_repository()
        assert repo.tenant_config("globex").display_name == "Globex Corporation"
        assert repo.tenant_config("initech").display_name == "Initech"

    def test_travelers_per_tenant(self):
        repo = seeded_repository()
        globex = {t.traveler_id for t in repo.travelers("globex")}
        assert globex == {PRIYA_ID, ADAEZE_ID, SAM_OKONJO_ID, SAM_ADEWALE_ID}
        assert {t.traveler_id for t in repo.travelers("initech")} == {SAM_WHITFIELD_ID}

    def test_ids_are_opaque(self):
        """No traveler id reveals a name — they appear in logs and audit trails."""
        repo = seeded_repository()
        for tenant in ("globex", "initech"):
            for traveler in repo.travelers(tenant):
                assert traveler.traveler_id.startswith("trv_")
                first_name = traveler.full_name.split()[0].lower()
                assert first_name not in traveler.traveler_id

    def test_idempotent(self):
        """Re-seeding must not duplicate."""
        repo = seeded_repository()
        from seed import seed

        seed(repo)
        assert len(repo.travelers("globex")) == 4


class TestTenantContrast:
    def test_currencies_differ(self):
        assert GLOBEX.currency is Currency.USD
        assert INITECH.currency is Currency.EUR

    def test_booking_modes_differ(self):
        assert GLOBEX.booking_mode is BookingMode.CONFIRM_IN_CHAT
        assert INITECH.booking_mode is BookingMode.HANDOFF

    def test_hotel_caps_differ(self):
        repo = seeded_repository()
        assert repo.policy("globex", "hotel").core.hotel_nightly_cap.amount == 250
        assert repo.policy("initech", "hotel").core.hotel_nightly_cap.amount == 150

    def test_air_policy_contrast(self):
        repo = seeded_repository()
        # Globex grants business on the 4th international trip; Initech never does.
        globex_air = repo.policy("globex", "air")
        initech_air = repo.policy("initech", "air")
        assert globex_air.core.cabin_rule.every_nth_trip == 4
        assert initech_air.air_status(CabinClass.BUSINESS, 13.0) is PolicyStatus.OUT_OF_POLICY


class TestPriyaEligibility:
    """The headline demo: Priya's next international trip is her 4th."""

    def test_three_prior_international_trips(self):
        repo = seeded_repository()
        trips = repo.trips("globex", PRIYA_ID)
        rule = repo.policy("globex", "air").core.cabin_rule
        # As of her upcoming Singapore trip, excluding it: exactly 3 prior.
        prior = count_international_trips(
            trips, rule, as_of=date(2026, 11, 3), exclude_trip_id="trip_priya_next"
        )
        assert prior == 3

    def test_singapore_trip_earns_business(self):
        repo = seeded_repository()
        trips = repo.trips("globex", PRIYA_ID)
        rule = repo.policy("globex", "air").core.cabin_rule
        prior = count_international_trips(
            trips, rule, as_of=date(2026, 11, 3), exclude_trip_id="trip_priya_next"
        )
        # 3 prior + this one = the 4th -> business permitted.
        assert rule.permitted_cabin(20.0, prior) is CabinClass.BUSINESS

    def test_has_in_progress_trip_for_context_questions(self):
        repo = seeded_repository()
        trips = repo.trips("globex", PRIYA_ID)
        in_progress = [t for t in trips if t.status is TripStatus.IN_PROGRESS]
        assert len(in_progress) == 1
        assert in_progress[0].primary_hotel is not None  # "which hotel am I at?"


class TestArranger:
    def test_adaeze_can_book_for_priya(self):
        repo = seeded_repository()
        adaeze = repo.traveler("globex", ADAEZE_ID)
        assert PRIYA_ID in adaeze.can_book_for

    def test_plain_traveler_books_for_nobody(self):
        repo = seeded_repository()
        assert repo.traveler("globex", PRIYA_ID).can_book_for == []


class TestIsolation:
    """App-level scoping here; IAM enforces the same boundary on the assumed data role."""

    def test_sam_invisible_under_globex(self):
        repo = seeded_repository()
        assert repo.traveler("globex", SAM_WHITFIELD_ID) is None

    def test_priya_invisible_under_initech(self):
        repo = seeded_repository()
        assert repo.traveler("initech", PRIYA_ID) is None

    def test_trips_never_cross_tenants(self):
        repo = seeded_repository()
        assert repo.trips("initech", PRIYA_ID) == []
        assert all(t.tenant_id == "initech" for t in repo.trips("initech"))


class TestPiiIsPresent:
    """PII exists on purpose so the tool layer has something to curate."""

    def test_passport_and_card_are_stored(self):
        repo = seeded_repository()
        priya = repo.traveler("globex", PRIYA_ID)
        assert priya.primary_passport.number  # raw number present in the backend
        assert priya.payment_instruments[0].last_four


class TestPolicyDocuments:
    """The KB must answer questions the structured policy cannot.

    If these documents only restated the typed fields, `search_policy_knowledge`
    would duplicate `get_travel_policy` and the sample would have no reason to
    demonstrate both.
    """

    def test_one_document_per_tenant(self):
        from seed.documents import DOCUMENTS, documents_for

        assert len(DOCUMENTS) == 2
        assert len(documents_for("globex")) == 1
        assert len(documents_for("initech")) == 1

    def test_documents_are_readable(self):
        from seed.documents import DOCUMENTS

        for doc in DOCUMENTS:
            assert doc.path.exists()
            assert len(doc.read()) > 1000

    def test_metadata_carries_the_isolation_field(self):
        """Every retrieval filters on `tenant_id` — it must be present."""
        from seed.documents import DOCUMENTS

        for doc in DOCUMENTS:
            assert doc.kb_metadata()["tenant_id"] == doc.tenant_id

    def test_s3_keys_are_tenant_prefixed(self):
        from seed.documents import documents_for

        assert documents_for("globex")[0].s3_key().startswith("policy/globex/")

    def test_contain_prose_no_schema_holds(self):
        """Spot-check the unstructured content that justifies the KB's existence."""
        from seed.documents import documents_for

        globex = documents_for("globex")[0].read().lower()
        # City-specific cap exceptions — not representable in a single cap field.
        assert "san francisco" in globex
        # The approval chain for an exception — process, not a value.
        assert "vp or above" in globex
        # What to do when a conference inflates every rate.
        assert "conference" in globex

        initech = documents_for("initech")[0].read().lower()
        # The *reasoning* behind non-refundable defaults.
        assert "change fee" in initech
        # Guidance that contradicts naive cap-satisfying behavior.
        assert "unreasonable distance" in initech

    def test_tenant_documents_differ_substantively(self):
        from seed.documents import documents_for

        globex = documents_for("globex")[0].read().lower()
        initech = documents_for("initech")[0].read().lower()
        # Globex maintains city-specific cap exceptions; Initech explicitly does
        # not, and says why. Normalize whitespace so line wrapping doesn't matter.
        globex_flat = " ".join(globex.split())
        initech_flat = " ".join(initech.split())
        assert "city-specific exception" in globex_flat
        assert "does not maintain city-specific exceptions" in initech_flat


class TestNameAmbiguityFixture:
    """The seed must actually contain the ambiguity the resolver is tested against.

    Behavior lives in `test_arrangers.py`; what belongs here is the precondition
    those tests silently depend on. If someone renames a Sam, this fails loudly
    instead of quietly turning the ambiguity suite into a tautology.
    """

    def test_two_globex_travelers_share_a_first_name(self):
        repo = seeded_repository()
        first_names = [t.full_name.split()[0] for t in repo.travelers("globex")]
        assert first_names.count("Sam") == 2

    def test_both_shared_name_travelers_are_bookable_by_the_arranger(self):
        repo = seeded_repository()
        adaeze = repo.traveler("globex", ADAEZE_ID)
        assert {SAM_OKONJO_ID, SAM_ADEWALE_ID} <= set(adaeze.can_book_for)

    def test_a_third_traveler_shares_the_name_in_another_tenant(self):
        """The cross-tenant near-miss: matches the string, must never be a candidate."""
        repo = seeded_repository()
        assert repo.traveler("initech", SAM_WHITFIELD_ID).full_name.startswith("Sam")

    def test_shared_name_travelers_are_distinguishable(self):
        """A disambiguation question needs something to differ on."""
        repo = seeded_repository()
        sams = [t for t in repo.travelers("globex") if t.full_name.startswith("Sam")]
        assert len({t.preferences.home_airport for t in sams}) == len(sams)


class TestCognitoUserSeed:
    """The Cognito users must correspond to real traveler fixtures.

    No AWS call here — what is testable offline is the correspondence, and that is
    exactly the part that drifts. A username mapped to a renamed fixture would
    authenticate fine in the demo and then resolve to no profile.
    """

    def test_every_demo_username_maps_to_a_fixture(self):
        from seed.travelers import TRAVELERS
        from seed.users import DEMO_USERNAMES

        names = {t.full_name for t in TRAVELERS}
        assert set(DEMO_USERNAMES) <= names

    def test_demo_users_span_both_tenants(self):
        """One tenant's users could not demonstrate isolation."""
        from seed.travelers import TRAVELERS
        from seed.users import DEMO_USERNAMES

        by_name = {t.full_name: t for t in TRAVELERS}
        tenants = {by_name[name].tenant_id for name in DEMO_USERNAMES}
        assert tenants == {"globex", "initech"}

    def test_demo_users_include_an_arranger(self):
        from seed.travelers import TRAVELERS
        from seed.users import DEMO_USERNAMES

        by_name = {t.full_name: t for t in TRAVELERS}
        roles = {by_name[name].role.value for name in DEMO_USERNAMES}
        assert "arranger" in roles

    def test_claims_carry_identity_only(self):
        """No authorization data in a token — `can_book_for` is resolved live."""
        from seed.travelers import ADAEZE
        from seed.users import _attributes

        names = {a["Name"] for a in _attributes(ADAEZE, "adaeze")}
        assert names == {
            "email",
            "email_verified",
            "name",
            "custom:tenant_id",
            "custom:traveler_id",
            "custom:role",
        }

    def test_arranger_claims_omit_the_relationship(self):
        """Adaeze has four people in `can_book_for`; none of them reach her token."""
        from seed.travelers import ADAEZE
        from seed.users import _attributes

        values = " ".join(a["Value"] for a in _attributes(ADAEZE, "adaeze"))
        assert ADAEZE.can_book_for
        for traveler_id in ADAEZE.can_book_for:
            assert traveler_id not in values

    def test_scope_format_uses_a_slash(self):
        """Cognito composes resource-server scopes as `<server>/<scope>`.

        The Gateway matches the emitted string exactly, so a colon here fails
        closed with no useful error. Asserted in Python because the pre-token
        Lambda and `identity.ts` must agree, and nothing else compares them.
        """
        source = (
            Path(__file__).resolve().parents[2] / "infra/lambda/pre-token/index.mjs"
        ).read_text()
        assert "'travel/read'" in source
        assert "'travel:read'" not in source

    def test_pre_token_lambda_carries_no_authorization_data(self):
        """The trigger copies identity. `can_book_for` must never appear in it."""
        source = (
            Path(__file__).resolve().parents[2] / "infra/lambda/pre-token/index.mjs"
        ).read_text()
        # The rejection is documented in prose, so only count executable mentions.
        code = "\n".join(
            line for line in source.splitlines() if not line.lstrip().startswith(("*", "//", "/*"))
        )
        assert "can_book_for" not in code

    def test_generated_password_satisfies_the_pool_policy(self):
        """12+ chars, upper, lower, digit, symbol — matching identity.ts."""
        from seed.users import _password

        password = _password()
        assert len(password) >= 12
        assert any(c.isupper() for c in password)
        assert any(c.islower() for c in password)
        assert any(c.isdigit() for c in password)
        assert any(not c.isalnum() for c in password)


class TestKnowledgeBaseMetadata:
    """The sidecar is the only metadata Bedrock reads during ingestion.

    Worth pinning: S3 *object* metadata is silently ignored, so a KB built that way indexes
    documents with no `tenant_id` and every filtered query returns nothing — a failure that
    looks like a broken filter rather than missing metadata.
    """

    def test_sidecar_key_is_document_key_plus_suffix(self):
        from seed.documents import DOCUMENTS

        doc = DOCUMENTS[0]
        assert doc.metadata_key() == f"{doc.s3_key()}.metadata.json"

    def test_sidecar_wraps_attributes_in_metadataAttributes(self):
        import json

        from seed.documents import DOCUMENTS

        payload = json.loads(DOCUMENTS[0].metadata_sidecar())
        assert set(payload) == {"metadataAttributes"}
        assert payload["metadataAttributes"]["tenant_id"] == DOCUMENTS[0].tenant_id

    def test_every_document_carries_a_tenant(self):
        """The field every retrieval filters on — absent means no isolation at all."""
        from seed.documents import DOCUMENTS

        assert all(doc.kb_metadata().get("tenant_id") for doc in DOCUMENTS)

    def test_metadata_is_ascii(self):
        """Kept ASCII so a title cannot differ between the index and everywhere else."""
        from seed.documents import DOCUMENTS

        for doc in DOCUMENTS:
            for value in doc.kb_metadata().values():
                assert value.isascii(), value

    def test_both_tenants_have_a_document(self):
        from seed.documents import documents_for

        assert documents_for("globex") and documents_for("initech")


class TestPolicyDocumentTenantAgreement:
    """The prefix, the retrieval metadata and the document's own content must name one tenant.

    **A security test, not a tidiness test.** One knowledge base and one vector index
    hold every tenant's policy text, so separation at retrieval is a metadata filter
    rather than a partition key. The filter is correct and built from verified context,
    but it trusts the metadata assigned at ingestion. Wrong metadata means retrieval
    faithfully serves one tenant's policy to another, and keeps doing so, with every
    layer above behaving exactly as designed.

    **What cannot go wrong, and is worth stating.** `s3_key()` and `kb_metadata()` both
    derive from the same `tenant_id` field, so the S3 prefix and the indexed metadata
    cannot disagree with each other through this path. A threat model claiming they can
    is overstating the gap.

    **What can.** The body is a separate file, so a copied or swapped document leaves the
    metadata right and the content wrong, which is the one part of this chain a human
    edits by hand. That is decidable here, because each fixture's heading names its
    company.
    """

    def test_s3_prefix_and_metadata_agree_on_the_tenant(self):
        from seed.documents import DOCUMENTS

        for doc in DOCUMENTS:
            assert doc.s3_key().startswith(f"policy/{doc.tenant_id}/")
            assert doc.kb_metadata()["tenant_id"] == doc.tenant_id
            # The sidecar is what Bedrock actually reads; object metadata is ignored silently.
            assert doc.metadata_key() == f"{doc.s3_key()}.metadata.json"

    def test_document_body_names_the_tenant_it_is_registered_under(self):
        """Catches a swapped file: right metadata, wrong content."""
        from seed.documents import DOCUMENTS

        for doc in DOCUMENTS:
            heading = doc.read().splitlines()[0].lower()
            assert doc.tenant_id in heading, (
                f"{doc.filename} is registered to {doc.tenant_id} but its heading does "
                f"not name it: {heading!r}. A swapped document keeps the metadata correct "
                f"and makes retrieval serve the wrong tenant's policy."
            )

    def test_document_heading_names_no_other_tenant(self):
        """A heading naming two tenants means the fixture was edited from another one."""
        from seed.documents import DOCUMENTS

        tenants = {doc.tenant_id for doc in DOCUMENTS}
        for doc in DOCUMENTS:
            heading = doc.read().splitlines()[0].lower()
            for other in tenants - {doc.tenant_id}:
                assert other not in heading, (
                    f"{doc.filename} belongs to {doc.tenant_id} but its heading also names {other}"
                )


class TestSeedOutputWithholdsThePassword:
    """`main()` must never print the shared password, however it was obtained.

    **A regression test rather than a unit test, and the distinction is the point.** The password
    used
    to be printed, and Holmes flagged it HIGH on the pre-publish scan. No pattern matcher found it,
    because there is no literal secret in the source: the value only exists at runtime. So nothing
    would stop it coming back except a test that runs the real entry point and reads what came out.

    stdout is the least contained place a credential can go. It persists in terminal scrollback,
    in CI
    job logs, and in anything that captured the deploy output, none of which expire the way the
    SecureString parameter does.

    Both paths are covered: an operator-supplied `--password` and a generated one, because the
    second
    is the one nobody thinks to check.
    """

    def _run(self, monkeypatch, capsys, password_arg: list[str], created_password: str):
        from seed import users as seed_users_module

        monkeypatch.setattr(
            seed_users_module,
            "seed_users",
            lambda _pool, *, password=None: [
                ("priya", created_password),
                ("sam", created_password),
            ],
        )
        monkeypatch.setattr(seed_users_module, "verify_claims", lambda _pool: None)
        monkeypatch.setattr(
            sys, "argv", ["users.py", "--user-pool-id", "us-east-1_TEST", *password_arg]
        )
        assert seed_users_module.main() == 0
        return capsys.readouterr().out

    @staticmethod
    def _sentinel() -> str:
        """A stand-in password, composed rather than written as a literal.

        **A password-shaped literal in a test is exactly what secret scanners are for**, and the
        first version of these tests earned its own Bandit B105 on the Holmes re-scan: a test
        guarding against credentials in output had put a credential-shaped string in the tree.

        Composing it costs nothing. These assertions only need a value distinctive enough that
        its presence in output would be unambiguous, which a hyphenated phrase satisfies as well
        as a plausible password does. Shared by every test in this class so there is one place
        for it rather than three literals to notice.
        """
        return "-".join(["fixture", "value", "must", "not", "be", "echoed", "42"])

    def test_supplied_password_is_not_printed(self, monkeypatch, capsys):
        sentinel = self._sentinel()
        out = self._run(monkeypatch, capsys, ["--password", sentinel], sentinel)
        assert sentinel not in out

    def test_generated_password_is_not_printed(self, monkeypatch, capsys):
        """The path nobody checks: no `--password`, so the value is invented at runtime."""
        from seed.users import _password

        generated = _password()
        out = self._run(monkeypatch, capsys, [], generated)
        assert generated not in out

    def test_output_still_says_where_to_find_it(self, monkeypatch, capsys):
        """Withholding the value must not make the script unusable.

        Removing a secret from output is only correct if the operator can still reach it, so
        this asserts the pointer survives. A future tidy-up should not be able to drop the
        location along with the value.
        """
        from seed.users import PASSWORD_PARAM

        out = self._run(monkeypatch, capsys, [], self._sentinel())
        assert PASSWORD_PARAM in out
        assert "get-parameter" in out
