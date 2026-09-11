"""
scripts/campaigns_configure.py : find a state's candidate file, and check that
reading it produces the right thing before wiring it in.

Everything here is a thin CLI over campaigns_generic. The reason it exists is
that configuring a state is a THREE-step loop that was previously only
reachable from a Python REPL:

    python -m scripts.campaigns_configure discover PA
    python -m scripts.campaigns_configure probe PA --dataset abcd-1234
    # then set dataset= in campaigns_states.PROFILES['PA'] and re-probe

RUN IT WHERE THE NETWORK REACHES STATE PORTALS. Every state election portal is
denied by egress policy in the Claude Code container -- api.us.socrata.com,
data.pa.gov, data.ny.gov, sos.state.tx.us, ohiosos.gov all answer 403 at the
gateway -- so `discover` and `probe` are the two commands that cannot run
there. `status` and `leads` work anywhere.

WHAT PROBE IS FOR. Not "did it download". Two failure modes are silent, and
both have already happened once in this package:

  UNMAPPED OFFICE   A statehouse wording the profile does not know is either
                    dropped or, worse, filed as the federal office it
                    resembles. Pennsylvania's "Representative in the General
                    Assembly" did exactly this. probe reports offices_unmapped;
                    a non-empty list there means the profile is not finished,
                    even though the parse "worked".

  THIN PARSE        A dataset that is the wrong universe, or a stale cycle,
                    returns rows and produces records. The check is
                    house_districts_found against the state's apportionment: a
                    state with 17 seats that yields 3 districts did not fail,
                    it lied.

probe prints a verdict on both rather than leaving them in a dict for somebody
to notice.
"""
from __future__ import annotations

import argparse
import json
import sys

from backend import campaigns_generic as gen
from backend import campaigns_states as states


def _profile(state: str):
    try:
        return states.PROFILES[state.upper()]
    except KeyError:
        print(f"error: {state!r} is not a state in the registry", file=sys.stderr)
        raise SystemExit(2)


def cmd_discover(args) -> int:
    profile = _profile(args.state)
    try:
        hits = gen.discover(profile.state, query=args.query)
    except Exception as exc:                              # noqa: BLE001
        print(f"error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2

    if not hits:
        print(f"no dataset on {profile.socrata_domain} matched {args.query!r}.")
        return 1
    for hit in hits:
        print(f"{hit.get('id','?'):<12} {hit.get('updated','?'):<12} "
              f"rows={hit.get('rows','?'):<8} {hit.get('name','')}")
    print(f"\nNext: probe {profile.state} --dataset <id>. A campaign-FINANCE "
          f"dataset is not a ballot listing -- see `leads`.")
    return 0


def cmd_probe(args) -> int:
    profile = _profile(args.state)
    if args.dataset:
        import dataclasses
        profile = dataclasses.replace(profile, dataset=args.dataset)
        states.PROFILES[profile.state] = profile

    report = gen.probe(profile.state)
    if not report.get("ok") and report.get("stage") == "fetch":
        print(f"fetch failed: {report.get('error')}", file=sys.stderr)
        print(f"dataset: {report.get('dataset')}", file=sys.stderr)
        print(f"source:  {report.get('source_page')}", file=sys.stderr)
        return 2

    print(f"state:      {report['state']}   dataset: {report['dataset']}")
    print(f"rows:       {report['rows']}")
    print(f"candidates: {report['candidates']}")
    print(f"columns:    {', '.join(report['columns'][:12])}"
          + (" ..." if len(report["columns"]) > 12 else ""))
    print(f"offices mapped:   {', '.join(report['offices_mapped']) or '(none)'}")

    verdict = 0
    unmapped = report["offices_unmapped"]
    if unmapped:
        verdict = 1
        print(f"\nUNMAPPED OFFICES ({len(unmapped)}). Each is a row dropped, or "
              f"misfiled as the federal office it resembles:")
        for office in unmapped:
            print(f"  - {office!r}")
        print(f"  Add these to PROFILES['{report['state']}'].office_names.")

    found = report["house_districts_found"]
    expected = report["house_districts_expected"]
    if expected and found < expected * 0.5:
        verdict = 1
        print(f"\nTHIN PARSE. {found} of {expected} U.S. House districts found. "
              f"This dataset is the wrong universe, the wrong cycle, or the "
              f"district column is not being read.")
    else:
        print(f"house districts:  {found} of {expected}")

    if args.json:
        print("\n" + json.dumps(report, indent=2, default=str))
    if verdict == 0:
        print(f"\nUSABLE. Set PROFILES['{report['state']}'].dataset = "
              f"{report['dataset']!r} and publication = \"confirmed\".")
    return verdict


def cmd_status(args) -> int:
    snapshot = states.status()
    reach, actual = snapshot["reach"], snapshot["actual"]
    print(f"written:    {len(snapshot['written'])} states")
    print(f"configured: {len(snapshot['configured'])} "
          f"({', '.join(snapshot['configured']) or 'none'})")
    print(f"reach:      {reach['seats_reachable']} seats "
          f"({reach['pct_federal_and_gubernatorial']}%)")
    print(f"actual:     {actual['seats_reachable']} seats "
          f"({actual['pct_federal_and_gubernatorial']}%)")
    if snapshot["adapter_load_errors"]:
        print(f"\nadapter load errors: {snapshot['adapter_load_errors']}")
    return 0


def cmd_next(args) -> int:
    rows = states.unconfigured_states()
    print(f"{len(rows)} states unconfigured, largest first:\n")
    for row in rows[:args.limit]:
        print(f"{row['state']}  {row['seats']:>3} seats  "
              f"{row['shape']:<8} {row['publication']:<9} {row['how']}")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Find and verify a state's 2026 candidate file.")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("discover", help="search a Socrata state's catalogue")
    p.add_argument("state")
    p.add_argument("--query", default="candidate")
    p.set_defaults(func=cmd_discover)

    p = sub.add_parser("probe", help="fetch a dataset and check what it parses to")
    p.add_argument("state")
    p.add_argument("--dataset", default="", help="try this id/URL without editing the profile")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_probe)

    p = sub.add_parser("status", help="reach vs configured")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("next", help="what is unconfigured, largest first")
    p.add_argument("--limit", type=int, default=50)
    p.set_defaults(func=cmd_next)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
