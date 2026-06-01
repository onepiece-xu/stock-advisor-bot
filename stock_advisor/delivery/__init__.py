"""Phase 4: Delivery renderers — pure formatting, no data fetching.

Each renderer takes pre-computed data and returns formatted markdown strings.
They do NOT fetch quotes, scan opportunities, or make trading decisions.
"""

from .pre_market import render_pre_market_briefing
from .intraday import render_intraday_action_card, render_intraday_instruction
from .close_review import render_close_review_summary

__all__ = [
    "render_pre_market_briefing",
    "render_intraday_action_card",
    "render_intraday_instruction",
    "render_close_review_summary",
]
