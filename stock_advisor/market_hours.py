from __future__ import annotations

from datetime import datetime, time
from zoneinfo import ZoneInfo


MARKET_TZ = ZoneInfo("Asia/Shanghai")
MORNING_START = time(9, 30)
MORNING_END = time(11, 30)
AFTERNOON_START = time(13, 0)
AFTERNOON_END = time(15, 0)


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
