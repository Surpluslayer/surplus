"""
The configure CLI's verdict logic.

probe() already returns everything needed to judge a dataset; the value added
here is refusing to call a parse successful when it quietly is not. Both
failure modes below have happened once in this package already, which is why
they are asserted rather than trusted.
"""
from __future__ import annotations

import pytest

from backend import campaigns_generic as gen
from scripts import campaigns_configure as cli


class Args:
    def __init__(self, **kw):
        self.state = kw.get("state", "PA")
        self.dataset = kw.get("dataset", "")
        self.json = kw.get("json", False)
        self.query = kw.get("query", "candidate")
        self.limit = kw.get("limit", 50)


def _report(**kw) -> dict:
    base = {
        "ok": True, "stage": "parse", "state": "PA", "dataset": "abcd-1234",
        "rows": 900, "columns": ["name", "office", "district"],
        "offices_in_data": [], "offices_mapped": ["U.S. House"],
        "offices_unmapped": [], "candidates": 400,
        "house_districts_found": 17, "house_districts_expected": 17,
        "report": {}, "sample_rows": [],
    }
    base.update(kw)
    return base


def test_a_clean_parse_is_reported_usable(monkeypatch, capsys):
    monkeypatch.setattr(gen, "probe", lambda state: _report())
    assert cli.cmd_probe(Args()) == 0
    assert "USABLE" in capsys.readouterr().out


def test_an_unmapped_office_fails_the_verdict(monkeypatch, capsys):
    """Pennsylvania's actual bug: a statehouse wording the profile does not
    know is dropped, or filed as the federal office it resembles. The parse
    'works' either way."""
    monkeypatch.setattr(gen, "probe", lambda state: _report(
        offices_unmapped=["Representative in the General Assembly"]))
    assert cli.cmd_probe(Args()) == 1
    out = capsys.readouterr().out
    assert "UNMAPPED OFFICES" in out
    assert "Representative in the General Assembly" in out
    assert "USABLE" not in out


def test_a_thin_parse_fails_even_though_rows_came_back(monkeypatch, capsys):
    """The wrong universe or a stale cycle returns rows and produces records.
    Three districts out of seventeen did not fail -- it lied."""
    monkeypatch.setattr(gen, "probe", lambda state: _report(
        candidates=40, house_districts_found=3))
    assert cli.cmd_probe(Args()) == 1
    out = capsys.readouterr().out
    assert "THIN PARSE" in out
    assert "USABLE" not in out


def test_a_district_count_just_over_half_is_not_called_thin(monkeypatch, capsys):
    """The guard is for a dataset that is obviously the wrong thing, not for a
    state mid-filing-season with genuine gaps."""
    monkeypatch.setattr(gen, "probe", lambda state: _report(
        house_districts_found=9))
    assert cli.cmd_probe(Args()) == 0
    assert "THIN PARSE" not in capsys.readouterr().out


def test_a_failed_fetch_reports_the_source_page_rather_than_a_traceback(
        monkeypatch, capsys):
    """A fetch that fails has to say WHERE to go looking, because the state is
    unconfigured precisely when nobody knows the URL yet.

    The source page is a sentinel rather than a real URL on purpose. Asserting
    that some https:// string appears would pass if the URL turned up for an
    unrelated reason, and CodeQL is right to call a URL substring check a weak
    test; a sentinel proves the source_page FIELD is what reached stderr.
    """
    sentinel = "SOURCE-PAGE-REACHED-STDERR"
    monkeypatch.setattr(gen, "probe", lambda state: {
        "ok": False, "stage": "fetch", "state": "PA",
        "error": "SourceError: 403", "dataset": "(unset)",
        "source_page": sentinel})
    assert cli.cmd_probe(Args()) == 2
    assert sentinel in capsys.readouterr().err


def test_an_unknown_state_exits_rather_than_raising_keyerror(capsys):
    with pytest.raises(SystemExit):
        cli.cmd_probe(Args(state="ZZ"))


def test_status_and_next_run_without_network(capsys):
    assert cli.cmd_status(Args()) == 0
    assert cli.cmd_next(Args(limit=3)) == 0
    out = capsys.readouterr().out
    assert "configured:" in out and "unconfigured" in out


def test_the_ruled_out_pennsylvania_dataset_stays_ruled_out():
    """A campaign-finance filer list is the obvious catalogue hit for PA and is
    the wrong universe. Losing this note means wiring it is a fresh mistake
    rather than a repeated one."""
    from backend import campaigns_states as states
    note = states.PROFILES["PA"].note
    assert "53wp-ib3s" in note
    assert "WRONG UNIVERSE" in note
