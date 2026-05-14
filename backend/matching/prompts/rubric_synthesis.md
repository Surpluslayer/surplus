# Match rubric synthesis prompt

You design a person-to-person matching rubric for ONE specific event. Given the event's name, description, and a summary of the guest list, you produce a JSON rubric that a deterministic scorer will apply to every pair of attendees.

The rubric must be **event-aware**: a hackathon needs teammate matching (skill complementarity), a fellowship reunion needs cofounder/mentor matching, a salon needs conversational compatibility. You decide what "good match" means for THIS event.

## Input

```
EVENT NAME:        {event_name}
EVENT DESCRIPTION: {event_description}

GUEST LIST SUMMARY:
  Total approved attendees:  {total_count}
  Ticket types present:      {ticket_type_counts}
  Top role keywords:         {top_roles}
  Top company keywords:      {top_companies}
  Experience levels:         {exp_level_counts}
```

## Your task

1. Infer the **event type**: hackathon, fellowship cohort, founder summit, salon, mixer, conference, dinner, meetup, etc.
2. Decide the **match intent**: what does "good match" mean here? (teammate / cofounder / intro / mentor / deal_flow / conversation / hire / mixed)
3. Produce a **role_pair_matrix** that scores every observed ticket_type × ticket_type pair from 0.0 to 1.0. Use the actual ticket_type values from the guest list (not generic ones). Symmetric.
4. Decide **hard gates** — dimensions that must clear a threshold or the pair scores 0.
5. Choose **weights** on the two axes (similar and complementary). User has expressed: more weight on complementary, less on similar. Tune for this event.
6. Pick **anti-signals** with their penalty multipliers.

## Output

Return only one JSON object. No prose, no markdown fence:

```json
{
  "event_type": "hackathon | fellowship | founder_summit | salon | mixer | conference | dinner | other",
  "event_type_reasoning": "1-2 sentences: why you classified it this way",

  "match_intent": "hackathon_teammate | cofounder | intro | mentor | deal_flow | conversation | hire | mixed",
  "match_intent_reasoning": "1-2 sentences",

  "role_pair_matrix": {
    "Attendee|Attendee": 1.0,
    "Attendee|Investor": 0.7,
    "Investor|Judge": 0.6,
    "...": 0.0
  },

  "hard_gates": {
    "min_similar_score": 0.20,
    "min_role_pair_score": 0.30,
    "require_same_city": false
  },

  "weights": {
    "axis_blend": {
      "similar": 0.30,
      "complementary": 0.70
    },
    "similar": {
      "domain_overlap": 0.40,
      "conviction_overlap": 0.30,
      "background_resonance": 0.20,
      "city_match": 0.10
    },
    "complementary": {
      "skill_complement": 0.40,
      "experience_asymmetry": 0.25,
      "role_complement": 0.20,
      "domain_expansion": 0.15
    }
  },

  "anti_signals": {
    "direct_competitor_multiplier": 0.25,
    "profile_clone_multiplier": 0.70,
    "seniority_gap_3_or_more_multiplier": 0.65,
    "explicit_mismatch_multiplier": 0.40
  },

  "notes_for_humans": "1-2 sentence summary of how this rubric prioritizes matches at this event — used in the UI when the rubric is shown to the organizer."
}
```

## Design rules

- **`role_pair_matrix` keys must use the actual ticket_type values from the input** (e.g. "Attendee", "Spectator", "Judge"). Generic placeholders like "Founder" are wrong unless that literal value appears in the guest list. Always include both orderings or document that it's symmetric.
- **Sum of weights inside each block must equal 1.0** (`axis_blend`, `similar`, `complementary`).
- **`axis_blend.complementary` should be higher** than `axis_blend.similar` by default (user preference: ~70/30), but you may adjust ±10pp if the event strongly favors one (e.g. a private peer dinner needs more similar; a cofounder mixer needs even more complementary).
- **hard_gates** — `min_similar_score` should be **0.03–0.10** (very permissive). Real-world data is sparse: most pairs share at most one or two domain tags. A gate above 0.10 kills almost all cross-role matches. Use 0.10 only for narrow single-topic events; default to 0.05. `min_role_pair_score` of 0.20–0.30 is the bigger filter — it cuts out genuinely incompatible role pairs (e.g. Volunteer↔Volunteer).
- **anti_signals** — multipliers in (0.0, 1.0). Lower number = harsher penalty. Direct competitor is the most punishing.
- **No fabrication.** Don't invent ticket types or roles not present in the input. If a ticket type is unknown how to weight, default its row to 0.5.

## Anchoring examples

- **Hackathon (Attendees + Judges + Investors + Spectators)**:
  - match_intent: `hackathon_teammate`
  - High weight on `skill_complement` + `experience_asymmetry`; high on `domain_overlap` (same hackathon track)
  - Attendee|Attendee = 1.0 (teammate); Attendee|Judge = 0.8 (mentor); Attendee|Investor = 0.5 (limited; investors come for deal flow, not teaming)

- **Fellowship reunion (everyone "Fellow" or "Alumni")**:
  - match_intent: `mixed` or `cofounder`
  - High weight on `conviction_overlap` (shared worldview) + `skill_complement` (cofounder potential)
  - Lower weight on `role_complement` (everyone is a founder/builder)

- **Founder summit (Founders + Investors + Speakers)**:
  - match_intent: `deal_flow`
  - Founder|Investor = 1.0; Founder|Founder = 0.8; Investor|Investor = 0.4

## Output format

Output exactly one JSON object. No preamble, no markdown fence, no explanation. Start with `{` and end with `}`.
