"""
config.py — the mechanism levers.

Everything here is *policy*, not data. These two tables are what make the
engine adapt: FORMAT_CONFIG decides the matching topology, GOAL_CONFIG decides
what "converted" means and what it's worth. Change a goal's value table and the
ROI ledger re-prices itself; add a format and the matcher gets a new topology.
"""

# --- funnel ----------------------------------------------------------------
FUNNEL_CONVERSION = 0.6   # good-fits needed per confirmed seat
ABS_FLOOR = 55            # the floating threshold never drops below this fit score

# --- format -> matching topology ------------------------------------------
FORMAT_CONFIG = {
    "Sit-down dinner": {
        "group_word": "Table", "group_size": 5,
        "topology": "fixed seating — composition locked before doors open",
    },
    "Hackathon": {
        "group_word": "Team", "group_size": 4,
        "topology": "team formation — complementary skills balanced per team",
    },
    "Workshop": {
        "group_word": "Breakout", "group_size": 6,
        "topology": "fluid breakouts — groups regroup between sessions",
    },
    "Mixer": {
        "group_word": "Cluster", "group_size": 8,
        "topology": "soft clusters — seeded, not enforced",
    },
    "Roundtable": {
        "group_word": "Seat", "group_size": 10,
        "topology": "single ring — seating order is the lever",
    },
}
DEFAULT_FORMAT = FORMAT_CONFIG["Sit-down dinner"]

# --- goal -> outreach framing + conversion semantics + dollar values -------
# `outreach` is a format string; compose() feeds it headcount/format/city/
# seniority/role/co_stage. `tiers` maps a fit tier to a conversion outcome;
# `value` prices each outcome state.
GOAL_CONFIG = {
    "Hiring pipeline": {
        "outreach": "a {headcount}-person {format} in {city} — {seniority} {role} and the teams hiring them",
        "ledger_head": "Hiring outcome",
        "tiers": {
            "high": {"label": "Hired",       "state": "won",     "detail": "signed offer"},
            "mid":  {"label": "In pipeline", "state": "partial", "detail": "final round"},
            "low":  {"label": "No fit",      "state": "lost",    "detail": "passed"},
        },
        "value": {"won": 28000, "partial": 8000, "lost": 0},
    },
    "Fundraising": {
        "outreach": "a {format} in {city} — founders raising at {co_stage} and the investors who back them",
        "ledger_head": "Raise outcome",
        "tiers": {
            "high": {"label": "Term sheet", "state": "won",     "detail": "in diligence"},
            "mid":  {"label": "Warm intro", "state": "partial", "detail": "follow-up booked"},
            "low":  {"label": "Passed",     "state": "lost",    "detail": "not a fit"},
        },
        "value": {"won": 180000, "partial": 30000, "lost": 0},
    },
    "Sales pipeline": {
        "outreach": "a {format} in {city} with operators evaluating tools in your space this quarter",
        "ledger_head": "Deal outcome",
        "tiers": {
            "high": {"label": "Closed", "state": "won",     "detail": "contract signed"},
            "mid":  {"label": "Trial",  "state": "partial", "detail": "POC started"},
            "low":  {"label": "Cold",   "state": "lost",    "detail": "no pull"},
        },
        "value": {"won": 54000, "partial": 11000, "lost": 0},
    },
    "Product testing": {
        "outreach": "a {format} in {city} — hands-on {seniority} {role} to stress-test an early build",
        "ledger_head": "Testing outcome",
        "tiers": {
            "high": {"label": "Active tester", "state": "won",     "detail": "12 issues filed, weekly"},
            "mid":  {"label": "Gave feedback", "state": "partial", "detail": "one session"},
            "low":  {"label": "Lapsed",        "state": "lost",    "detail": "no activity"},
        },
        "value": {"won": 16000, "partial": 4000, "lost": 0},
    },
    "Community density": {
        "outreach": "a recurring {format} in {city} — the {seniority} {role} crowd, same room every month",
        "ledger_head": "Community outcome",
        "tiers": {
            "high": {"label": "Core member", "state": "won",     "detail": "returning + bringing others"},
            "mid":  {"label": "Returning",   "state": "partial", "detail": "came back once"},
            "low":  {"label": "One-off",     "state": "lost",    "detail": "no return"},
        },
        "value": {"won": 6000, "partial": 1800, "lost": 0},
    },
}
DEFAULT_GOAL = GOAL_CONFIG["Hiring pipeline"]


def goal_cfg(goal: str) -> dict:
    return GOAL_CONFIG.get(goal, DEFAULT_GOAL)


def format_cfg(fmt: str) -> dict:
    return FORMAT_CONFIG.get(fmt, DEFAULT_FORMAT)
