"""
triage/icp_compiler.py : deterministic ICP -> triage_config compiler.

WHY THIS EXISTS
---------------
The part of an event's scoring config that encodes *priority policy* — boost
founders, cap investors, auto-accept corroborated founders, and the accept /
maybe / reject threshold band — used to be hand-authored as a big JSON blob per
event (see icp_bryankim.json). That is error-prone and we re-fix it every event.

This module turns a higher-level, structured ICP (what the operator actually
states: who they want in the room, what to boost, what to cap) into the exact
``triage_config`` policy the engine already consumes. It is the deterministic
"rules layer":

  - NO LLM call. The LLM rubric synthesis lives in ``rubric.py``; this is the
    pure, data-driven policy layer beneath it.
  - DETERMINISTIC: same ICP in -> same triage_config out. No network, no
    randomness, no time-dependence.
  - EVENT-AGNOSTIC: generic rules over generic ICP fields. No event-, sponsor-,
    or person-specific strings are baked in here.

The output schema is validated against its real consumers:
  - ``recommend.apply_archetype_priority`` (boost/cap shape, corroboration gate)
  - ``recommend.Thresholds.from_dict``     (threshold keys)
  - ``consolidate._auto_accept_ok``        (auto_accept: archetype /
                                            require_corroboration / min_dimension)
"""
from __future__ import annotations

from typing import Any, Optional


# --- tunable, but FIXED, policy constants -----------------------------------
# Default magnitudes for the archetype priority nudge. Clamped to the ranges
# below so a malformed ICP can never emit a runaway boost/cap.
_DEFAULT_BOOST = 12
_DEFAULT_CAP = 68
_BOOST_MIN, _BOOST_MAX = 0, 25
_CAP_MIN, _CAP_MAX = 40, 90

# On-thesis bar an auto-accepted priority archetype must clear.
_DEFAULT_AUTO_ACCEPT_MIN_DIMENSION = {"company_relevance": 55}

# Confidence cutoffs are format-independent (lifted from the rubric prompt).
_ACCEPT_CONFIDENCE_MIN = 60
_MAYBE_CONFIDENCE_MIN = 45

# Threshold bands keyed by event format, copied verbatim from the rubric
# synthesis prompt (rubric._RUBRIC_SYSTEM) so the deterministic compiler and the
# LLM path stay consistent. Each band is (accept_fit_min, maybe_fit_min,
# reject_fit_max); confidence mins are added uniformly below.
_BAND_MIXER = (65, 50, 35)
_BAND_STANDARD = (72, 55, 40)
_BAND_EXCLUSIVE = (78, 60, 45)

# Substrings (matched case-insensitively against icp.format) that select a band.
# Order matters: the most selective bands are checked first so "small intimate
# mixer" resolves to exclusive, not mixer.
_EXCLUSIVE_HINTS = ("exclusive", "invite-only", "invite only", "intimate", "small")
_MIXER_HINTS = ("mixer", "café", "cafe", "coworking", "open")

# Formats where a physical-location hard filter makes no sense.
_REMOTE_HINTS = ("remote", "virtual", "online")


# --- small, defensive coercion helpers --------------------------------------
def _clean_str(value: Any) -> str:
    """Coerce to a stripped string; non-strings and None -> ''."""
    if value is None:
        return ""
    if not isinstance(value, str):
        try:
            value = str(value)
        except Exception:
            return ""
    return value.strip()


def _clean_str_list(value: Any) -> list[str]:
    """Coerce to a list of non-blank, de-duplicated (order-preserving) strings.

    Accepts a list, a single string, or junk. Drops empties so a malformed ICP
    never injects blank entries into the config."""
    if value is None:
        return []
    if isinstance(value, str):
        candidates = [value]
    elif isinstance(value, (list, tuple, set)):
        candidates = list(value)
    else:
        return []
    out: list[str] = []
    seen: set[str] = set()
    for item in candidates:
        s = _clean_str(item)
        if s and s.lower() not in seen:
            seen.add(s.lower())
            out.append(s)
    return out


def _clean_archetype_list(value: Any) -> list[str]:
    """Like _clean_str_list but lowercases — archetypes are matched lowercase
    by both apply_archetype_priority and _auto_accept_ok."""
    return [s.lower() for s in _clean_str_list(value)]


def _clamp(value: int, lo: int, hi: int) -> int:
    return max(lo, min(hi, value))


def _coerce_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _coerce_bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    if isinstance(value, str):
        v = value.strip().lower()
        if v in ("true", "yes", "1", "y"):
            return True
        if v in ("false", "no", "0", "n"):
            return False
    return default


# --- band selection ----------------------------------------------------------
def _select_thresholds(fmt: str) -> dict[str, int]:
    """Pick the accept/maybe/reject band from the (free-text) event format.

    Unknown / blank format falls back to the standard dinner band. Confidence
    mins are uniform. See the band tables lifted from the rubric prompt above.
    """
    f = fmt.lower()
    if any(h in f for h in _EXCLUSIVE_HINTS):
        accept, maybe, reject = _BAND_EXCLUSIVE
    elif any(h in f for h in _MIXER_HINTS):
        accept, maybe, reject = _BAND_MIXER
    else:
        # Standard dinner/reception AND the default for unknown formats.
        accept, maybe, reject = _BAND_STANDARD
    return {
        "accept_fit_min": accept,
        "accept_confidence_min": _ACCEPT_CONFIDENCE_MIN,
        "maybe_fit_min": maybe,
        "maybe_confidence_min": _MAYBE_CONFIDENCE_MIN,
        "reject_fit_max": reject,
    }


# --- archetype priority ------------------------------------------------------
def _compile_archetype_priority(
    priority: list[str],
    deprioritize: list[str],
    require_corroboration: bool,
    warnings: list[str],
) -> dict[str, Any]:
    """Build the ``archetype_priority`` policy from the high-level intent lists.

    - priority archetypes -> boost (clamped to [_BOOST_MIN, _BOOST_MAX])
    - deprioritized archetypes -> cap (clamped to [_CAP_MIN, _CAP_MAX])
    - an ``auto_accept`` block is emitted ONLY when a priority archetype exists,
      keyed on the first priority archetype.

    Conflict resolution: an archetype appearing in BOTH lists is a contradiction
    (you cannot both boost and cap the same group). Priority wins — the archetype
    is dropped from the cap set — and a warning is recorded.
    """
    # Resolve conflicts deterministically: priority wins.
    priority_set = set(priority)
    resolved_deprioritize: list[str] = []
    for arch in deprioritize:
        if arch in priority_set:
            warnings.append(
                "archetype '%s' is in both priority and deprioritize lists; "
                "priority wins (not capped)" % arch
            )
            continue
        resolved_deprioritize.append(arch)

    boost = {arch: _clamp(_DEFAULT_BOOST, _BOOST_MIN, _BOOST_MAX) for arch in priority}
    cap = {
        arch: _clamp(_DEFAULT_CAP, _CAP_MIN, _CAP_MAX)
        for arch in resolved_deprioritize
    }

    policy: dict[str, Any] = {
        "boost": boost,
        "cap": cap,
        "require_corroboration_for_boost": require_corroboration,
    }

    # Auto-accept only makes sense when there's a priority archetype to admit.
    if priority:
        policy["auto_accept"] = {
            "archetype": priority[0],
            "require_corroboration": require_corroboration,
            "min_dimension": dict(_DEFAULT_AUTO_ACCEPT_MIN_DIMENSION),
        }

    return policy


# --- descriptive-string synthesis (deterministic, NOT an LLM) ----------------
def _build_ideal_attendee_profile(
    role: str,
    seniority: str,
    co_stage: str,
    city: str,
    priority: list[str],
    deprioritize: list[str],
) -> str:
    """Assemble a concise human-readable profile string from the ICP fields.

    Pure deterministic string assembly — the rubric synthesizer handles the rich
    prose; this is a stable fallback/anchor the operator can read at a glance."""
    parts: list[str] = []
    if priority:
        parts.append("Prioritize " + ", ".join(priority) + ".")
    bits: list[str] = []
    if role:
        bits.append(role)
    if seniority:
        bits.append("(%s)" % seniority)
    if co_stage:
        bits.append("at a %s company" % co_stage)
    if bits:
        parts.append("Ideal guest: " + " ".join(bits) + ".")
    if city:
        parts.append("Based in %s." % city)
    if deprioritize:
        parts.append("Deprioritize " + ", ".join(deprioritize) + ".")
    return " ".join(parts)


def compile_icp(icp: Optional[dict]) -> dict:
    """Compile a structured ICP into a ``triage_config`` policy dict.

    This is the entry point. It is total over malformed input: a sparse, empty,
    or partially-broken ICP yields a valid minimal config, never an exception.

    Parameters
    ----------
    icp:
        Operator-supplied structured ICP. Recognized keys (all optional):
        ``role``, ``seniority``, ``co_stage``, ``format``, ``city``, ``goal``,
        ``priority_archetypes`` (list[str], boosted), ``deprioritize_archetypes``
        (list[str], capped), ``anti_fit`` (list[str]), ``nice_to_have``
        (list[str]), ``capacity`` (int), ``require_corroboration`` (bool,
        default True).

    Returns
    -------
    dict
        A ``triage_config`` with keys: ``event_goal``,
        ``ideal_attendee_profile``, ``hard_filters``, ``nice_to_have_signals``,
        ``anti_fit_examples``, ``capacity``, ``archetype_priority``,
        ``thresholds``. A ``_compiler_warnings`` list is included only when a
        conflict or coercion was resolved.
    """
    if not isinstance(icp, dict):
        icp = {}

    warnings: list[str] = []

    role = _clean_str(icp.get("role"))
    seniority = _clean_str(icp.get("seniority"))
    co_stage = _clean_str(icp.get("co_stage"))
    fmt = _clean_str(icp.get("format"))
    city = _clean_str(icp.get("city"))
    goal = _clean_str(icp.get("goal"))

    priority = _clean_archetype_list(icp.get("priority_archetypes"))
    deprioritize = _clean_archetype_list(icp.get("deprioritize_archetypes"))
    anti_fit = _clean_str_list(icp.get("anti_fit"))
    nice_to_have = _clean_str_list(icp.get("nice_to_have"))
    require_corroboration = _coerce_bool(icp.get("require_corroboration"), True)

    capacity = _coerce_int(icp.get("capacity"), 0)
    if capacity < 0:
        warnings.append("capacity was negative; coerced to 0")
        capacity = 0

    # hard_filters: a location gate only when a city is set AND the event is not
    # remote/virtual (a remote event has no physical-location requirement).
    hard_filters: list[str] = []
    is_remote = any(h in fmt.lower() for h in _REMOTE_HINTS)
    if city and not is_remote:
        hard_filters.append("Must be based in %s" % city)

    # nice_to_have_signals: operator-stated, plus a derived city signal so being
    # local is rewarded (soft) even when it isn't a hard filter (e.g. remote).
    nice_to_have_signals = list(nice_to_have)
    if city:
        derived_city = "%s-based or attending locally" % city
        if derived_city.lower() not in {s.lower() for s in nice_to_have_signals}:
            nice_to_have_signals.append(derived_city)

    archetype_priority = _compile_archetype_priority(
        priority, deprioritize, require_corroboration, warnings
    )

    thresholds = _select_thresholds(fmt)

    ideal_attendee_profile = _build_ideal_attendee_profile(
        role, seniority, co_stage, city, priority, deprioritize
    )

    config: dict[str, Any] = {
        "event_goal": goal,
        "ideal_attendee_profile": ideal_attendee_profile,
        "hard_filters": hard_filters,
        "nice_to_have_signals": nice_to_have_signals,
        "anti_fit_examples": anti_fit,
        "capacity": capacity,
        "archetype_priority": archetype_priority,
        "thresholds": thresholds,
    }

    if warnings:
        config["_compiler_warnings"] = warnings

    return config
