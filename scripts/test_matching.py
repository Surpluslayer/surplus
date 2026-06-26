"""
scripts/test_matching.py : exercise the matcher algorithm with synthetic data.

No HTTP, no DB. Builds a fake event + 15 fake confirmed guests across a mix
of market sides + domains, runs build_edges() and form_groups() directly,
prints the result so you can read what the algorithm produced.

    python3 -m scripts.test_matching

Tweak SAMPLE_ATTENDEES below to play with different mixes (e.g., all
Builders, founder-heavy room, hackathon with 4 specialists per team).
"""
from __future__ import annotations
from types import SimpleNamespace

from backend.agents.events.matcher import build_edges, form_groups
from backend import config


# Edit this to test different mixes.
SAMPLE_ATTENDEES = [
    # name,                   side,       works_on,            offers,                  seeks,                              fit
    ("Maya Rodriguez",        "Builds",   "observability",     "Observability depth",   "Staff-scope role",                 94),
    ("Daniel Okafor",         "Builds",   "model-serving",     "Model-serving infra",   "Founding-level scope",             91),
    ("Theo Lindqvist",        "Builds",   "distributed-systems","OSS credibility",       "Unsolved systems problems",        90),
    ("Nadia Haddad",          "Builds",   "observability",     "Telemetry-at-scale",    "Advisory scope",                   89),
    ("Lucia Fernandes",       "Builds",   "distributed-systems","Reliability rigor",     "Bigger systems surface",           88),
    ("Raj Malhotra",          "Builds",   "model-serving",     "Inference optimization", "Founding opportunity",             86),
    ("Yuki Tanaka",           "Builds",   "model-serving",     "Low-level performance", "Frontier infra problems",          88),

    ("Priya Natarajan",       "Hires",    "ml-platform",       "Platform roles",        "Infra builders to hire",           96),
    ("Aisha Bello",           "Hires",    "data-infra",        "Data-team roles",       "Data-infra builders",              86),
    ("Kenji Watanabe",        "Hires",    "ml-platform",       "Senior infra roles",    "Builders who've shipped serving",  80),
    ("Ben Arsenault",         "Hires",    "distributed-systems","Leadership roles",     "Systems builders",                 84),

    ("Elena Popov",           "Operates", "observability",     "Distribution + community", "Technical cofounder",            81),
    ("Dana Cohen",            "Operates", "product",           "Vision + early traction", "Technical cofounder",              78),
    ("Marcus Reed",           "Operates", "product",           "Product sense",          "Eng cofounder",                    74),
]


def main() -> None:
    # Build fake Prospect-like objects (the matcher only reads the attributes
    # it cares about, so a SimpleNamespace is enough : no DB roundtrip).
    attending = [
        SimpleNamespace(
            id=i, name=name, side=side, works_on=works_on,
            offers=offers, seeks=seeks, fit_score=fit,
            company=f"Co-{i}",
        )
        for i, (name, side, works_on, offers, seeks, fit)
        in enumerate(SAMPLE_ATTENDEES, start=1)
    ]

    # Try each format so you can see how topology changes group sizes.
    for fmt in ("Sit-down dinner", "Hackathon", "Workshop", "Mixer"):
        event = SimpleNamespace(format=fmt)
        cfg = config.format_cfg(fmt)
        word = cfg["group_word"]

        print(f"\n{'═' * 70}")
        print(f"  FORMAT: {fmt}  ·  {word} (size {cfg['group_size']})  ·  {len(attending)} attendees")
        print(f"  topology: {cfg['topology']}")
        print('═' * 70)

        # 1. score every pair, identify symbiotic vs affinity edges
        edges = build_edges(attending)
        sym = [e for e in edges if e["edge_type"] == "symbiotic"]
        aff = [e for e in edges if e["edge_type"] == "affinity"]
        print(f"\n  EDGES: {len(sym)} symbiotic  ·  {len(aff)} affinity")

        # top symbiotic pairs : the ones the room exists to manufacture
        by_id = {p.id: p for p in attending}
        top_sym = sorted(sym, key=lambda e: -e["weight"])[:5]
        print("\n  TOP SYMBIOTIC PAIRS (highest predicted mutual value):")
        for e in top_sym:
            a, b = by_id[e["a_id"]], by_id[e["b_id"]]
            print(f"    {a.name:<22} ⟷  {b.name:<22}  weight {e['weight']}")
            print(f"      {a.offers:<38}  ↔  {b.seeks}")
            print(f"      {b.offers:<38}  ↔  {a.seeks}")
            print()

        # 2. pack attendees into balanced groups
        groups = form_groups(attending, event)
        print(f"  {word.upper()}S ({len(groups)} total):")
        for gid, members in sorted(groups.items()):
            builds = sum(1 for m in members if m.side == "Builds")
            counter = sum(1 for m in members if m.side != "Builds")
            roster = ", ".join(f"{m.name.split()[0]}({m.side[0]})" for m in members)
            print(f"    {word} {gid}:  [{builds} builds · {counter} other-side]  {roster}")

        print()


if __name__ == "__main__":
    main()
