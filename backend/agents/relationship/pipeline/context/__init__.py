"""Stage 2: gather one contact's context for agent + composer."""
from .gather import (
    as_agent_context,
    as_composer_context,
    gather_contact_context,
    thread_from_timeline,
)
from .reconcile import (
    DEFAULT_RECENT_MESSAGES,
    apply_to_facts,
    clear_prospect_next_step_if_fulfilled,
    obligation_still_open,
    reconcile_next_step,
    summarize_older_thread,
    window_thread,
)
from .summary import summarize_older_messages, window_and_summarize

# Back-compat alias used across the package.
_thread_from_timeline = thread_from_timeline
