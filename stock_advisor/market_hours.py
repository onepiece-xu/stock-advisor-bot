from __future__ import annotations

from datetime import datetime, time
from zoneinfo import ZoneInfo


MARKET_TZ = ZoneInfo("Asia/Shanghai")
MORNING_START = time(9, 30)
MORNING_END = time(11, 30)
AFTERNOON_START = time(13, 0)
AFTERNOON_END = time(15, 0)


def is_high_volatility_period(now: datetime | None = None) -> bool:
    """Returns True during the most unreliable signal windows (open/close chaos)."""
    if now is None:
        now = datetime.now(MARKET_TZ)
    elif now.tzinfo is None:
        now = now.replace(tzinfo=MARKET_TZ)
    else:
        now = now.astimezone(MARKET_TZ)
    t = now.time()
    opening = time(9, 30) <= t < time(9, 36)
    closing = time(14, 50) <= t <= time(15, 0)
    return opening or closing


def is_auction_period(now: datetime | None = None) -> bool:
    """Returns True during the call auction window (9:25-9:30) for pre-market briefing."""
    if now is None:
        now = datetime.now(MARKET_TZ)
    elif now.tzinfo is None:
        now = now.replace(tzinfo=MARKET_TZ)
    else:
        now = now.astimezone(MARKET_TZ)
    if now.weekday() >= 5:
        return False
    t = now.time()
    return time(9, 25) <= t < time(9, 30)


def is_a_share_trading_time(now: datetime | None = None) -> bool:
    if now is None:
        now = datetime.now(MARKET_TZ)
    elif now.tzinfo is None:
        now = now.replace(tzinfo=MARKET_TZ)
    else:
        now = now.astimezone(MARKET_TZ)

    if now.weekday() >= 5:
        return False

    current = now.time()
    in_morning = MORNING_START <= current <= MORNING_END
    in_afternoon = AFTERNOON_START <= current <= AFTERNOON_END
    return in_morning or in_afternoon
