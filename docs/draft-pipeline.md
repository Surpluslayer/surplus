# The draft pipeline

> One composer, four stages. Messaging is the product crux, so the way a draft is
> built is a first-class subsystem — not an accreting pile of prompt clauses.

`backend/agents/drafting.py` turns a `Contact` into a follow-up message. Every
surface uses it (autodraft on a new update, the Book `/draft` tap, the `/ask`
agent batch) so there is exactly one place where "how a follow-up is written"
lives, and one eval (`messaging_eval.py`) that measures it.

## Why a pipeline

The composer grew by accretion: each "make it hone in better" ask bolted another
clause onto one `_user_prompt` function (voice block, register line, met-at,
next-step, latest update, post text, mirror-the-thread, the About...). Three
problems followed:

1. **Voice cues contradicted each other.** The host's casual voice profile, the
   contact's formal register, and an established thread's dynamic were all
   injected at once. A formal contact with a prior message got both "write like
   the casual host" and "be formal" — and the casual one won, so Dr. Vance got
   "Hey! 🙌". (The eval caught exactly this: `formal_contact` intent 1.2.)
2. **Fabrication was a prompt plea, not structure.** Every fact was stated the
   same way regardless of how much we trusted it, with a "use only stated facts"
   sentence doing all the work of stopping the model from overreaching on weak
   signals (e.g. an enriched "what they work on" that's often just "general").
3. **No clear seam to add the next signal** (mutuals, shared interests, warm
   intros) without making the tangle worse.

The fix is to name the stages a draft already moves through and give each one job.

## The four stages

```
Contact
  │
  ▼
┌─────────────────────────────────────────────────────────────────────┐
│ ① GATHER   build_context() → _relationship_facts()                   │
│            ALL DB reads, on the request thread (Session not thread-   │
│            safe). Produces the context dict: packaged voice, person   │
│            facts, the real prior thread, relationship grounding, the  │
│            latest update + its real content, register, low-conf About.│
└─────────────────────────────────────────────────────────────────────┘
  │   (pure data from here down — safe to run concurrently in the fan-out)
  ▼
┌─────────────────────────────────────────────────────────────────────┐
│ ② RESOLVE  _resolve_voice()  +  _natural_action()                    │
│            Collapse competing signals into decisions:                 │
│            • voice  → ONE instruction, by precedence (see below)      │
│            • intent → the situational move (deliver / react / reply / │
│                       re-engage)                                       │
└─────────────────────────────────────────────────────────────────────┘
  │
  ▼
┌─────────────────────────────────────────────────────────────────────┐
│ ③ SELECT   _select_grounding()                                       │
│            Order facts strongest-first, gate by confidence:           │
│            • asserted  = verified (update, open loop, met-at, types)  │
│            • optional  = low-confidence color (About) — may be used,  │
│                          never required → anti-fabrication is structural│
└─────────────────────────────────────────────────────────────────────┘
  │
  ▼
┌─────────────────────────────────────────────────────────────────────┐
│ ④ RENDER   _user_prompt()  →  compose_from_context() / stream        │
│            Assemble the user message from the resolved situation; the │
│            system prompt carries the resolved voice. Brevity + use-   │
│            only-stated-facts enforced here. LLM call → scrub dashes.  │
└─────────────────────────────────────────────────────────────────────┘
  │
  ▼
draft  {subject?, body}
```

### The one real judgment call: voice precedence

Three signals fight over *how a draft should sound*. `_resolve_voice` resolves
them to a single instruction with an explicit order:

> **FORMAL register  >  thread dynamic  >  host voice profile**

- **Formal is a hard constraint** (no emoji, no slang, fuller greeting). It must
  outrank everything, including an active thread — a formal contact has to get a
  professional draft *even mid-conversation*, or the casual host voice leaks in.
- **Thread dynamic** (the contact has written): continue the rapport already
  established and mirror how *they* write (length, energy, emoji), keeping the
  host's identity. This is for non-formal threads.
- **Host voice profile** (the default): no thread, not formal → write in the
  host's own distilled voice, with a light register nudge for casual/neutral.

Mindset and grounding (the facts, the intent, "don't fabricate") always outrank
voice — they live in the system prompt and the SELECT stage, not here.

## Where each past "win" slots in

The pipeline isn't a rewrite of behavior; it's the same wins, placed coherently:

| Win                                   | Stage | How                                               |
|---------------------------------------|-------|---------------------------------------------------|
| Real post text (`latest_update_detail`) | ①→③ | gathered, asserted as a verified fact             |
| Per-person conversational mirroring   | ②     | the thread branch of `_resolve_voice`             |
| Formal-register adaptation            | ②     | the formal branch (now outranks thread)           |
| Contact About / "what they work on"   | ①→③ | gathered (graceful), offered as **optional** color |
| Situational move                      | ②     | `_natural_action`                                  |
| Ask-bar directive (batch intent)      | ④     | shared `directive`, per-person facts differentiate |

## How we know it's not a regression

`messaging_eval.py` runs a fixed scenario set through the real composer:
deterministic gates (no em dash / concise / not-generic) + an LLM judge
(voice_match, specificity, correct_intent, natural) + a position-randomized
**pairwise** old-vs-new judge (lower variance than absolute means).

Pipeline vs the pre-pipeline composer, 5 runs each:

- Aggregates at parity: voice 4.17 / spec 3.71 / intent 4.54 / natural 4.60
  (vs 4.23 / 3.69 / 4.51 / 4.57), gates clean on both.
- Pairwise: new 48% / old 40% / tie 11% — a statistical tie overall, with a
  **decisive win on `formal_contact` (4-1)** and `voiced_open_loop` (4-1), i.e.
  the cases the structure was designed to fix.

So: equal quality, cleaner structure, the formal bug fixed by design, and a clean
seam for the next signal.

## Adding the next signal (the extension point)

To add e.g. mutual connections or shared interests:

1. **GATHER** it in `_relationship_facts` (read-only, graceful when absent).
2. **SELECT** it with the right confidence — asserted only if verified, else
   optional color.
3. If it changes *how to sound* (rare), add a branch to `_resolve_voice` with an
   explicit precedence; otherwise it never touches voice.
4. Add/extend a case in `messaging_eval.py` and check the pairwise before merge.

No stage reaches across to another's job, so a new signal is additive, not another
clause in a growing prompt.
