from __future__ import annotations

from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo


MARKET_TZ = ZoneInfo("Asia/Shanghai")
AUCTION_START = time(9, 25)
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
    in_auction = AUCTION_START <= current < MORNING_START
    in_morning = MORNING_START <= current <= MORNING_END
    in_afternoon = AFTERNOON_START <= current <= AFTERNOON_END
    return in_auction or in_morning or in_afternoon


def seconds_until_next_session(now: datetime | None = None) -> float:
    """Calculate seconds until the next A-share trading session starts.

    After market close (15:00+), returns seconds until next day's auction (9:25).
    On weekends, returns seconds until Monday's auction.
    """
    if now is None:
        now = datetime.now(MARKET_TZ)
    elif now.tzinfo is None:
        now = now.replace(tzinfo=MARKET_TZ)
    else:
        now = now.astimezone(MARKET_TZ)

    today = now.date()
    today_auction = datetime.combine(today, time(9, 25), tzinfo=MARKET_TZ)

    # If it's early morning (before 9:25) and today is a trading day, today's auction
    if now < today_auction and today.weekday() < 5:
        return (today_auction - now).total_seconds()

    # Find the next trading day's auction
    next_day = today + timedelta(days=1)
    next_auction = datetime.combine(next_day, time(9, 25), tzinfo=MARKET_TZ)
    while next_auction.weekday() >= 5:
        next_day = next_auction.date() + timedelta(days=1)
        next_auction = datetime.combine(next_day, time(9, 25), tzinfo=MARKET_TZ)

    return (next_auction - now).total_seconds()


def next_session_str(now: datetime | None = None) -> str:
    """Human-readable string for the next trading session start."""
    if now is None:
        now = datetime.now(MARKET_TZ)
    elif now.tzinfo is None:
        now = now.replace(tzinfo=MARKET_TZ)
    else:
        now = now.astimezone(MARKET_TZ)

    # Find next auction
    today_auction = now.replace(hour=9, minute=25, second=0, microsecond=0)
    if now < today_auction and now.weekday() < 5:
        return f"今天 {today_auction.strftime('%H:%M')}"
    next_day = now + timedelta(days=1)
    next_auction = next_day.replace(hour=9, minute=25, second=0, microsecond=0)
    while next_auction.weekday() >= 5:
        next_auction += timedelta(days=1)
    return f"{next_auction.strftime('%m月%d日 %H:%M')}（{['周一','周二','周三','周四','周五','周六','周日'][next_auction.weekday()]}）"


def is_opening_grace_period(now: datetime | None = None) -> bool:
    """Returns True during 9:30-10:00 — the first 30 minutes after market open.

    Minute-level MA signals during this window are dominated by overnight
    order digestion and gap-filling noise.  Suppressing reduce/avoid pushes
    here prevents false alarms like telling a user to sell a stock they
    just bought yesterday.
    """
    if now is None:
        now = datetime.now(MARKET_TZ)
    elif now.tzinfo is None:
        now = now.replace(tzinfo=MARKET_TZ)
    else:
        now = now.astimezone(MARKET_TZ)
    if now.weekday() >= 5:
        return False
    t = now.time()
    return time(9, 30) <= t < time(10, 0)
