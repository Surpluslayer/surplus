"""
Company resolver + account backfill (agents/relationship/company_resolve.py).

Follows the repo convention: functions called directly with an in-memory
SQLAlchemy session, ANTHROPIC_API_KEY removed so every path exercised here is
the deterministic one (the LLM is an optional upgrade, never a dependency).

Covers: domain strong-key resolve + company creation, freemail filtering,
name matching against existing companies, deterministic headline extraction,
ambiguous-name pending_review, idempotent re-resolve, job-change close/open,
lazy Account creation with contact_count rollup, and the backfill dry-run
report contract (shape + rollback leaves zero rows).
"""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend import models
from backend.agents.relationship import company_resolve as cr
from backend.db import Base


@pytest.fixture
def env(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)


@pytest.fixture
def db(env):
    engine = create_engine("sqlite:///:memory:",
                           connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, autoflush=False, autocommit=False)()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture
def user(db):
    u = models.User(name="Operator", email="op@example.com")
    db.add(u); db.commit()
    return u


_seq = {"n": 0}


def _contact(db, user, **kw):
    _seq["n"] += 1
    c = models.Contact(user_id=user.id,
                       primary_identity_key=f"li:test-{_seq['n']}",
                       name=kw.pop("name", f"Person {_seq['n']}"), **kw)
    db.add(c); db.commit()
    return c


def _memberships(db, contact):
    return (db.query(models.AccountMembership)
              .filter_by(contact_id=contact.id).all())


# -- normalization helpers ----------------------------------------------------

@pytest.mark.parametrize("raw,expected", [
    ("https://www.Acme.com/about?x=1", "acme.com"),
    ("http://acme.io", "acme.io"),
    ("WWW.ACME.CO.UK", "acme.co.uk"),
    ("acme.com.", "acme.com"),
    ("jane@acme.com", "acme.com"),
    ("acme.com:8080/path", "acme.com"),
    ("not a domain", ""),
    ("localhost", ""),
    ("", ""),
    (None, ""),
])
def test_normalize_domain(raw, expected):
    assert cr.normalize_domain(raw) == expected


@pytest.mark.parametrize("raw,expected", [
    ("Acme, Inc.", "acme"),
    ("ACME LLC", "acme"),
    ("Acme Corp", "acme"),
    ("Sullivan & Cromwell LLP", "sullivan cromwell"),
    ("The Coca-Cola Company", "the coca cola"),
    ("Inc", "inc"),                      # a suffix that IS the name survives
    ("  Acme   Health  ", "acme health"),
    ("", ""),
    (None, ""),
])
def test_normalize_company_name(raw, expected):
    assert cr.normalize_company_name(raw) == expected


def test_freemail_blocklist():
    for dom in ("gmail.com", "googlemail.com", "outlook.com", "hotmail.com",
                "yahoo.com", "icloud.com", "me.com", "aol.com", "proton.me",
                "protonmail.com"):
        assert cr.is_freemail(dom), dom
    assert not cr.is_freemail("acme.com")
    assert not cr.is_freemail("")


# -- headline extraction (deterministic, keyless) ------------------------------

@pytest.mark.parametrize("headline,expected", [
    ("Head of Growth at Acme | Angel Investor", "Acme"),
    ("CEO at Acme Corp | ex-Google | speaker", "Acme Corp"),
    ("Software Engineer @ Stripe", "Stripe"),
    ("Growth @Notion", "Notion"),
    ("Partner at Sullivan & Cromwell, New York", "Sullivan & Cromwell"),
    ("CTO at Acme - building the future of ops", "Acme"),
    ("Dreamer. Doer. Builder.", None),           # no employer named
    ("Working at scale on hard problems", None),  # grammar, not a company
    ("", None),
    (None, None),
])
def test_headline_extraction(headline, expected):
    assert cr.extract_employer_from_headline(headline) == expected


# -- strong path: domain ------------------------------------------------------

def test_domain_strong_resolve_creates_company(db, user):
    contact = _contact(db, user, company="Acme Inc.",
                       company_domain="https://www.acme.com")
    m = cr.resolve_contact(db, contact)
    db.commit()

    assert m is not None and m.status == "linked"
    assert m.confidence == 1.0 and m.is_current
    assert m.resolved_via == "domain"
    company = db.get(models.Company, m.company_id)
    assert company.primary_domain == "acme.com"
    assert company.canonical_name == "Acme Inc."
    kinds = {(i.kind, i.value) for i in
             db.query(models.CompanyIdentity).filter_by(company_id=company.id)}
    assert ("domain", "acme.com") in kinds
    assert ("name_norm", "acme") in kinds


def test_email_domain_links_to_existing_company(db, user):
    first = _contact(db, user, company_domain="acme.com")
    cr.resolve_contact(db, first)
    second = _contact(db, user, email="jane@acme.com")
    m = cr.resolve_contact(db, second)
    db.commit()

    assert m.confidence == 1.0
    assert db.query(models.Company).count() == 1  # linked, not duplicated


def test_freemail_email_is_skipped(db, user):
    contact = _contact(db, user, email="jane@gmail.com")
    assert cr.resolve_contact(db, contact) is None
    assert db.query(models.Company).count() == 0
    assert db.query(models.AccountMembership).count() == 0


def test_freemail_company_domain_falls_through_to_name(db, user):
    contact = _contact(db, user, company="Acme",
                       company_domain="gmail.com", email="j@hotmail.com")
    m = cr.resolve_contact(db, contact)
    assert m is not None and m.resolved_via == "name"
    company = db.get(models.Company, m.company_id)
    assert company.primary_domain is None  # freemail never became an identity


# -- name path ----------------------------------------------------------------

def test_company_name_exact_match_links_existing(db, user):
    seeded = cr.resolve_contact(db, _contact(db, user, company_domain="acme.com",
                                             company="Acme"))
    contact = _contact(db, user, company="Acme, Inc.")
    m = cr.resolve_contact(db, contact)
    db.commit()

    assert m.company_id == seeded.company_id
    assert m.status == "linked" and m.confidence == 0.9
    assert m.resolved_via == "name"
    assert db.query(models.Company).count() == 1


def test_new_name_creates_company(db, user):
    m = cr.resolve_contact(db, _contact(db, user, company="Verci Labs"))
    assert m.status == "linked" and m.confidence == 0.9
    assert db.get(models.Company, m.company_id).canonical_name == "Verci Labs"


def test_headline_resolves_when_no_company_field(db, user):
    contact = _contact(db, user,
                       headline="Head of Growth at Acme | Angel | Advisor")
    m = cr.resolve_contact(db, contact)
    assert m is not None and m.status == "linked"
    assert m.resolved_via == "headline"
    assert db.get(models.Company, m.company_id).canonical_name == "Acme"


def test_no_signal_at_all_is_skipped(db, user):
    contact = _contact(db, user, headline="Dreamer. Doer. Builder.")
    assert cr.resolve_contact(db, contact) is None


def test_ambiguous_name_goes_to_pending_review(db, user):
    # Two REAL companies that normalize to "acme" (each anchored by its own
    # domain). With no API key the resolver must refuse to pick and flag it.
    a = cr.resolve_contact(db, _contact(db, user, company="Acme",
                                        company_domain="acme.com"))
    b = cr.resolve_contact(db, _contact(db, user, company="Acme Inc",
                                        company_domain="acme.io"))
    assert a.company_id != b.company_id

    contact = _contact(db, user, company="Acme")
    m = cr.resolve_contact(db, contact)
    db.commit()

    assert m.status == "pending_review"
    assert m.confidence < cr.AUTO_LINK_THRESHOLD
    assert m.company_id in (a.company_id, b.company_id)
    assert db.query(models.Company).count() == 2  # no third Acme minted


def test_name_norm_identity_never_repoints(db, user):
    cr.resolve_contact(db, _contact(db, user, company="Acme",
                                    company_domain="acme.com"))
    cr.resolve_contact(db, _contact(db, user, company="Acme Inc",
                                    company_domain="acme.io"))
    rows = (db.query(models.CompanyIdentity)
              .filter_by(kind="name_norm", value="acme").all())
    assert len(rows) == 1  # unique slot held by the first, never re-pointed


# -- idempotency + job change -------------------------------------------------

def test_reresolve_is_idempotent(db, user):
    contact = _contact(db, user, company_domain="acme.com")
    m1 = cr.resolve_contact(db, contact)
    db.commit()
    m2 = cr.resolve_contact(db, contact)
    db.commit()

    assert m1.id == m2.id
    assert len(_memberships(db, contact)) == 1
    acct = db.query(models.Account).one()
    assert acct.contact_count == 1  # recomputed, not double-bumped


def test_job_change_close_and_reopen(db, user):
    contact = _contact(db, user, company_domain="oldco.com")
    old = cr.resolve_contact(db, contact)
    old_company_id = old.company_id
    newco = cr.resolve_contact(db, _contact(db, user,
                                            company_domain="newco.com"))
    db.commit()

    m = cr.close_and_reopen_membership(db, contact, newco.company_id)
    db.commit()

    rows = _memberships(db, contact)
    assert len(rows) == 2
    closed = next(r for r in rows if r.company_id == old_company_id)
    opened = next(r for r in rows if r.company_id == newco.company_id)
    assert closed.is_current is False and closed.ended_at is not None
    assert opened.is_current is True and opened.ended_at is None
    assert opened.id == m.id and m.source == "job_change_event"

    counts = {a.company_id: a.contact_count
              for a in db.query(models.Account).filter_by(owner_id=user.id)}
    assert counts[old_company_id] == 0        # coverage at OldCo dropped
    assert counts[newco.company_id] == 2      # existing contact + the mover


def test_account_lazy_creation_and_contact_count(db, user):
    c1 = _contact(db, user, company_domain="acme.com")
    c2 = _contact(db, user, email="jo@acme.com")
    m1 = cr.resolve_contact(db, c1)
    cr.resolve_contact(db, c2)
    db.commit()

    accounts = (db.query(models.Account)
                  .filter_by(owner_type="user", owner_id=user.id).all())
    assert len(accounts) == 1
    assert accounts[0].company_id == m1.company_id
    assert accounts[0].contact_count == 2


# -- backfill -----------------------------------------------------------------

def _seed_backfill_book(db, user):
    _contact(db, user, name="Dana Domain", company_domain="acme.com",
             company="Acme")
    _contact(db, user, name="Eve Email", email="eve@stripe.com")
    _contact(db, user, name="Nia Name", company="Verci Labs")
    _contact(db, user, name="Hal Headline",
             headline="CTO at Notion | ex-Google")
    _contact(db, user, name="Gil Gmail", email="gil@gmail.com")
    _contact(db, user, name="Nova Nothing")


def test_backfill_dry_run_report_shape_and_rollback(db, user):
    _seed_backfill_book(db, user)
    report = cr.backfill(db, user_id=user.id, dry_run=True)

    assert report["total"] == 6
    assert report["resolved_strong"] == 2      # Dana + Eve
    assert report["resolved_name"] == 2        # Nia + Hal
    assert report["pending_review"] == 0
    assert report["skipped_no_signal"] == 2    # Gil (freemail) + Nova
    assert report["companies_created"] == 4
    assert len(report["sample"]) == 4
    names = {row[0] for row in report["sample"]}
    assert names == {"Dana Domain", "Eve Email", "Nia Name", "Hal Headline"}
    for _, company_name, via, confidence in report["sample"]:
        assert company_name
        assert via in ("domain", "name", "headline")
        assert 0.0 < confidence <= 1.0

    # dry run rolled back: NOTHING landed
    assert db.query(models.Company).count() == 0
    assert db.query(models.CompanyIdentity).count() == 0
    assert db.query(models.AccountMembership).count() == 0
    assert db.query(models.Account).count() == 0


def test_backfill_execute_writes_and_is_rerunnable(db, user):
    _seed_backfill_book(db, user)
    first = cr.backfill(db, user_id=user.id, dry_run=False)
    assert db.query(models.AccountMembership).count() == 4
    assert db.query(models.Company).count() == 4

    second = cr.backfill(db, user_id=user.id, dry_run=False)
    assert second["companies_created"] == 0
    assert db.query(models.AccountMembership).count() == 4  # no dupes
    assert first["total"] == second["total"] == 6


def test_backfill_scopes_to_user(db, user):
    other = models.User(name="Other", email="other@example.com")
    db.add(other); db.commit()
    _contact(db, user, company_domain="acme.com")
    _contact(db, other, company_domain="rivals.com")

    report = cr.backfill(db, user_id=user.id, dry_run=True)
    assert report["total"] == 1


def test_spine_clean_strips_control_characters():
    """A LinkedIn display name with a raw control character must never reach
    the DB: one stored 0x01 breaks every strict JSON consumer downstream."""
    from backend.agents.relationship.spine.relationships import _clean
    assert _clean("Jane\x01 Doe\x00") == "Jane Doe"
    assert _clean("  ok\tname  ") == "ok\tname"
    assert _clean("\x1f") is None


def test_extract_employer_collapses_newlines():
    """Multi-line headlines must never mint a company name containing a raw
    newline (one reached prod and broke strict JSON consumers)."""
    from backend.agents.relationship.company_resolve import (
        extract_employer_from_headline)
    got = extract_employer_from_headline(
        "Partner at Meridian\nCapital | investing", allow_llm=False)
    assert got == "Meridian Capital"
