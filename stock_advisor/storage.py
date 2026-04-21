from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from statistics import median

from .models import DecisionSignal, ObservationMetrics, ObservationResult, StockQuote, TradeFillRecord


def connect_db(db_path: str | Path) -> sqlite3.Connection:
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    init_db(conn)
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS quotes (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          symbol TEXT NOT NULL,
          code TEXT NOT NULL,
          name TEXT NOT NULL,
          provider TEXT NOT NULL,
          quote_time TEXT NOT NULL,
          current_price REAL NOT NULL,
          open_price REAL NOT NULL,
          previous_close REAL NOT NULL,
          high_price REAL NOT NULL,
          low_price REAL NOT NULL,
          change_amount REAL NOT NULL,
          change_percent REAL NOT NULL,
          volume_shares REAL NOT NULL,
          turnover_yuan REAL NOT NULL,
          raw_payload TEXT,
          created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );

        CREATE UNIQUE INDEX IF NOT EXISTS uq_quotes_symbol_time ON quotes(symbol, quote_time);
        CREATE INDEX IF NOT EXISTS idx_quotes_code_time ON quotes(code, quote_time);

        CREATE TABLE IF NOT EXISTS signals (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          quote_id INTEGER,
          symbol TEXT NOT NULL,
          code TEXT NOT NULL,
          signal_time TEXT NOT NULL,
          signal_level TEXT NOT NULL,
          should_notify INTEGER NOT NULL,
          observations_json TEXT NOT NULL,
          message TEXT NOT NULL,
          created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
          FOREIGN KEY (quote_id) REFERENCES quotes(id)
        );

        CREATE INDEX IF NOT EXISTS idx_signals_symbol_time ON signals(symbol, signal_time);
        CREATE INDEX IF NOT EXISTS idx_signals_level_time ON signals(signal_level, signal_time);

        CREATE TABLE IF NOT EXISTS signal_metrics (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          signal_id INTEGER NOT NULL,
          ma5 REAL,
          ma15 REAL,
          ma60 REAL,
          ma240 REAL,
          rsi14 REAL,
          bias_to_ma15 REAL,
          bias_to_ma60 REAL,
          step_change_pct REAL,
          recent_range_pct REAL,
          intraday_amplitude_pct REAL,
          minute_volume_shares REAL,
          avg5_minute_volume_shares REAL,
          avg30_minute_volume_shares REAL,
          volume_ratio REAL,
          volume_ratio_30 REAL,
          volume_trend_ratio REAL,
          breakout_above_prev30_high_pct REAL,
          breakdown_below_prev30_low_pct REAL,
          benchmark_change_pct REAL,
          relative_strength_pct REAL,
          macd_line REAL,
          macd_signal REAL,
          macd_histogram REAL,
          macd_prev_histogram REAL,
          market_advance_ratio REAL,
          hot_stock_rank INTEGER,
          FOREIGN KEY (signal_id) REFERENCES signals(id)
        );

        CREATE TABLE IF NOT EXISTS decision_signals (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          signal_id INTEGER NOT NULL,
          symbol TEXT NOT NULL,
          code TEXT NOT NULL,
          action TEXT NOT NULL,
          score REAL NOT NULL,
          confidence TEXT NOT NULL,
          regime TEXT NOT NULL,
          rationale_json TEXT NOT NULL,
          risk_flags_json TEXT NOT NULL,
          trade_advice TEXT NOT NULL DEFAULT '',
          trade_size_hint TEXT NOT NULL DEFAULT '',
          entry_note TEXT NOT NULL DEFAULT '',
          created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
          FOREIGN KEY (signal_id) REFERENCES signals(id)
        );

        CREATE INDEX IF NOT EXISTS idx_decisions_symbol_time ON decision_signals(symbol, created_at);
        CREATE INDEX IF NOT EXISTS idx_decisions_action_time ON decision_signals(action, created_at);

        CREATE TABLE IF NOT EXISTS trade_fills (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          side TEXT NOT NULL,
          code TEXT NOT NULL,
          quantity INTEGER NOT NULL,
          price REAL NOT NULL,
          before_quantity INTEGER NOT NULL,
          after_quantity INTEGER NOT NULL,
          filled_at TEXT NOT NULL,
          created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );

        CREATE INDEX IF NOT EXISTS idx_trade_fills_code_time ON trade_fills(code, filled_at);
        CREATE INDEX IF NOT EXISTS idx_trade_fills_side_time ON trade_fills(side, filled_at);
        """
    )
    _ensure_column(conn, "decision_signals", "trade_advice", "TEXT NOT NULL DEFAULT ''")
    _ensure_column(conn, "decision_signals", "trade_size_hint", "TEXT NOT NULL DEFAULT ''")
    _ensure_column(conn, "decision_signals", "entry_note", "TEXT NOT NULL DEFAULT ''")
    for column_name, ddl in (
        ("ma5", "REAL"),
        ("ma15", "REAL"),
        ("ma60", "REAL"),
        ("ma240", "REAL"),
        ("rsi14", "REAL"),
        ("bias_to_ma15", "REAL"),
        ("bias_to_ma60", "REAL"),
        ("minute_volume_shares", "REAL"),
        ("avg5_minute_volume_shares", "REAL"),
        ("avg30_minute_volume_shares", "REAL"),
        ("volume_ratio", "REAL"),
        ("volume_ratio_30", "REAL"),
        ("volume_trend_ratio", "REAL"),
        ("breakout_above_prev30_high_pct", "REAL"),
        ("breakdown_below_prev30_low_pct", "REAL"),
        ("benchmark_change_pct", "REAL"),
        ("relative_strength_pct", "REAL"),
        ("macd_line", "REAL"),
        ("macd_signal", "REAL"),
        ("macd_histogram", "REAL"),
        ("macd_prev_histogram", "REAL"),
        ("market_advance_ratio", "REAL"),
        ("hot_stock_rank", "INTEGER"),
    ):
        _ensure_column(conn, "signal_metrics", column_name, ddl)
    conn.commit()


def insert_quote(conn: sqlite3.Connection, quote: StockQuote) -> int:
    conn.execute(
        """
        INSERT OR IGNORE INTO quotes (
          symbol, code, name, provider, quote_time, current_price, open_price,
          previous_close, high_price, low_price, change_amount, change_percent,
          volume_shares, turnover_yuan, raw_payload
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            quote.symbol,
            quote.code,
            quote.name,
            quote.provider,
            quote.quote_time.isoformat(sep=" "),
            float(quote.current_price),
            float(quote.open_price),
            float(quote.previous_close),
            float(quote.high_price),
            float(quote.low_price),
            float(quote.change_amount),
            float(quote.change_percent),
            float(quote.volume_shares),
            float(quote.turnover_yuan),
            quote.raw_payload,
        ),
    )
    row = conn.execute(
        "SELECT id FROM quotes WHERE symbol = ? AND quote_time = ?",
        (quote.symbol, quote.quote_time.isoformat(sep=" ")),
    ).fetchone()
    if row is None:
        raise RuntimeError("Failed to persist quote")
    return int(row["id"])


def insert_trade_fill(conn: sqlite3.Connection, fill: TradeFillRecord) -> int:
    cursor = conn.execute(
        """
        INSERT INTO trade_fills (
          side, code, quantity, price, before_quantity, after_quantity, filled_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            fill.side,
            fill.code,
            fill.quantity,
            float(fill.price),
            fill.before_quantity,
            fill.after_quantity,
            fill.filled_at.isoformat(sep=" ", timespec="seconds"),
        ),
    )
    return int(cursor.lastrowid)


def load_trade_fills(conn: sqlite3.Connection, *, limit: int | None = None) -> list[TradeFillRecord]:
    sql = """
        SELECT side, code, quantity, price, before_quantity, after_quantity, filled_at
        FROM trade_fills
        ORDER BY filled_at DESC, id DESC
    """
    params: tuple[object, ...] = ()
    if limit is not None:
        sql += " LIMIT ?"
        params = (limit,)
    rows = conn.execute(sql, params).fetchall()
    return [
        TradeFillRecord(
            side=str(row["side"]),
            code=str(row["code"]),
            quantity=int(row["quantity"]),
            price=_decimal(row["price"]),
            before_quantity=int(row["before_quantity"]),
            after_quantity=int(row["after_quantity"]),
            filled_at=datetime.fromisoformat(str(row["filled_at"])),
        )
        for row in rows
    ]


def insert_signal(conn: sqlite3.Connection, quote_id: int, quote: StockQuote, result: ObservationResult) -> int:
    cursor = conn.execute(
        """
        INSERT INTO signals (
          quote_id, symbol, code, signal_time, signal_level, should_notify, observations_json, message
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            quote_id,
            quote.symbol,
            quote.code,
            quote.quote_time.isoformat(sep=" "),
            result.signal_level,
            1 if result.should_notify else 0,
            json.dumps(result.observations, ensure_ascii=False),
            result.message,
        ),
    )
    signal_id = int(cursor.lastrowid)
    _insert_signal_metrics(conn, signal_id, result.metrics)
    _insert_decision_signal(conn, signal_id, quote, result.decision)
    return signal_id


def persist_observation(conn: sqlite3.Connection, quote: StockQuote, result: ObservationResult) -> tuple[int, int]:
    with conn:
        quote_id = insert_quote(conn, quote)
        signal_id = insert_signal(conn, quote_id, quote, result)
    return quote_id, signal_id


def _insert_signal_metrics(conn: sqlite3.Connection, signal_id: int, metrics: ObservationMetrics) -> None:
    conn.execute(
        """
        INSERT INTO signal_metrics (
          signal_id, ma5, ma15, ma60, ma240, rsi14, bias_to_ma15, bias_to_ma60, step_change_pct, recent_range_pct,
          intraday_amplitude_pct, minute_volume_shares, avg5_minute_volume_shares, avg30_minute_volume_shares,
          volume_ratio, volume_ratio_30, volume_trend_ratio, breakout_above_prev30_high_pct, breakdown_below_prev30_low_pct,
          benchmark_change_pct, relative_strength_pct, macd_line, macd_signal, macd_histogram, macd_prev_histogram,
          market_advance_ratio, hot_stock_rank
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            signal_id,
            float(metrics.ma5),
            float(metrics.ma15),
            float(metrics.ma60),
            float(metrics.ma240),
            float(metrics.rsi14),
            float(metrics.bias_to_ma15),
            float(metrics.bias_to_ma60),
            float(metrics.step_change_pct),
            float(metrics.recent_range_pct),
            float(metrics.intraday_amplitude_pct),
            float(metrics.minute_volume_shares),
            float(metrics.avg5_minute_volume_shares),
            float(metrics.avg30_minute_volume_shares),
            float(metrics.volume_ratio),
            float(metrics.volume_ratio_30),
            float(metrics.volume_trend_ratio),
            float(metrics.breakout_above_prev30_high_pct),
            float(metrics.breakdown_below_prev30_low_pct),
            float(metrics.benchmark_change_pct),
            float(metrics.relative_strength_pct),
            float(metrics.macd_line),
            float(metrics.macd_signal),
            float(metrics.macd_histogram),
            float(metrics.macd_prev_histogram),
            float(metrics.market_advance_ratio),
            int(metrics.hot_stock_rank),
        ),
    )


def _insert_decision_signal(conn: sqlite3.Connection, signal_id: int, quote: StockQuote, decision: DecisionSignal) -> None:
    conn.execute(
        """
        INSERT INTO decision_signals (
          signal_id, symbol, code, action, score, confidence, regime, rationale_json, risk_flags_json,
          trade_advice, trade_size_hint, entry_note
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            signal_id,
            quote.symbol,
            quote.code,
            decision.action,
            float(decision.score),
            decision.confidence,
            decision.regime,
            json.dumps(decision.rationale, ensure_ascii=False),
            json.dumps(decision.risk_flags, ensure_ascii=False),
            decision.trade_advice,
            decision.trade_size_hint,
            decision.entry_note,
        ),
    )


def replay_signal_stats(
    conn: sqlite3.Connection,
    *,
    symbol: str | None = None,
    signal_level: str | None = None,
    action: str | None = None,
    horizons: tuple[int, ...] = (1, 3, 5),
) -> dict:
    clauses: list[str] = []
    params: list[object] = []
    if symbol:
        clauses.append("s.symbol = ?")
        params.append(symbol)
    if signal_level:
        clauses.append("s.signal_level = ?")
        params.append(signal_level)
    if action:
        clauses.append("d.action = ?")
        params.append(action)

    where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    rows = conn.execute(
        f"""
        SELECT s.id AS signal_id, s.symbol, s.code, s.signal_time, s.signal_level, q.current_price,
               d.action, d.score, d.confidence, d.regime
        FROM signals s
        JOIN quotes q ON q.id = s.quote_id
        LEFT JOIN decision_signals d ON d.signal_id = s.id
        {where_sql}
        ORDER BY s.symbol, s.signal_time
        """,
        params,
    ).fetchall()

    by_horizon: dict[int, list[float]] = {h: [] for h in horizons}
    signal_count = 0
    actions: dict[str, int] = {}
    scores: list[float] = []

    for row in rows:
        signal_count += 1
        action_name = row["action"] or "unknown"
        actions[action_name] = actions.get(action_name, 0) + 1
        if row["score"] is not None:
            scores.append(float(row["score"]))
        future_quotes = conn.execute(
            """
            SELECT current_price
            FROM quotes
            WHERE symbol = ? AND quote_time > ?
            ORDER BY quote_time ASC
            LIMIT ?
            """,
            (row["symbol"], row["signal_time"], max(horizons)),
        ).fetchall()
        base_price = float(row["current_price"])
        if base_price <= 0:
            continue
        for horizon in horizons:
            if len(future_quotes) < horizon:
                continue
            future_price = float(future_quotes[horizon - 1]["current_price"])
            ret = ((future_price - base_price) / base_price) * 100
            by_horizon[horizon].append(ret)

    return {
        "generated_at": datetime.now().isoformat(sep=" ", timespec="seconds"),
        "signal_count": signal_count,
        "action_breakdown": actions,
        "avg_score": round(sum(scores) / len(scores), 4) if scores else None,
        "horizons": {str(h): _summarize_returns(values) for h, values in by_horizon.items()},
    }


def load_recent_quotes(conn: sqlite3.Connection, symbol: str, limit: int) -> list[StockQuote]:
    rows = conn.execute(
        """
        SELECT provider, symbol, code, name, current_price, open_price, previous_close, high_price, low_price,
               change_amount, change_percent, volume_shares, turnover_yuan, quote_time, raw_payload
        FROM quotes
        WHERE symbol = ?
        ORDER BY quote_time DESC
        LIMIT ?
        """,
        (symbol, limit),
    ).fetchall()
    return [_row_to_quote(row) for row in reversed(rows)]


def load_recent_quotes_before(conn: sqlite3.Connection, symbol: str, as_of: datetime, limit: int) -> list[StockQuote]:
    rows = conn.execute(
        """
        SELECT provider, symbol, code, name, current_price, open_price, previous_close, high_price, low_price,
               change_amount, change_percent, volume_shares, turnover_yuan, quote_time, raw_payload
        FROM quotes
        WHERE symbol = ? AND quote_time <= ?
        ORDER BY quote_time DESC
        LIMIT ?
        """,
        (symbol, as_of.isoformat(sep=" "), limit),
    ).fetchall()
    return [_row_to_quote(row) for row in reversed(rows)]


def cache_quotes(conn: sqlite3.Connection, quotes: list[StockQuote]) -> int:
    inserted = 0
    with conn:
        for quote in quotes:
            before = conn.total_changes
            insert_quote(conn, quote)
            if conn.total_changes > before:
                inserted += 1
    return inserted


def fetch_latest_briefing(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        """
        SELECT q.symbol, q.code, q.name, q.quote_time, q.current_price, q.change_percent,
               s.signal_level, d.action, d.score, d.confidence, d.regime, d.rationale_json, d.risk_flags_json
        FROM quotes q
        JOIN (
          SELECT symbol, MAX(quote_time) AS latest_time
          FROM quotes
          GROUP BY symbol
        ) latest ON latest.symbol = q.symbol AND latest.latest_time = q.quote_time
        LEFT JOIN (
          SELECT quote_id, MAX(id) AS latest_signal_id
          FROM signals
          GROUP BY quote_id
        ) latest_signal ON latest_signal.quote_id = q.id
        LEFT JOIN signals s ON s.id = latest_signal.latest_signal_id
        LEFT JOIN decision_signals d ON d.signal_id = s.id
        ORDER BY COALESCE(d.score, 0) DESC, q.symbol ASC
        """
    ).fetchall()
    return [
        {
            "symbol": row["symbol"],
            "code": row["code"],
            "name": row["name"],
            "quote_time": row["quote_time"],
            "current_price": round(float(row["current_price"]), 3),
            "change_percent": round(float(row["change_percent"]), 2),
            "signal_level": row["signal_level"] or "UNKNOWN",
            "action": row["action"] or "unknown",
            "score": round(float(row["score"]), 2) if row["score"] is not None else None,
            "confidence": row["confidence"] or "unknown",
            "regime": row["regime"] or "unknown",
            "rationale": json.loads(row["rationale_json"]) if row["rationale_json"] else [],
            "risk_flags": json.loads(row["risk_flags_json"]) if row["risk_flags_json"] else [],
        }
        for row in rows
    ]


def fetch_daily_review_snapshot(conn: sqlite3.Connection, trade_date: str) -> list[dict]:
    rows = conn.execute(
        """
        WITH daily_quotes AS (
          SELECT *,
                 ROW_NUMBER() OVER (PARTITION BY symbol ORDER BY quote_time DESC, id DESC) AS rn_desc,
                 ROW_NUMBER() OVER (PARTITION BY symbol ORDER BY quote_time ASC, id ASC) AS rn_asc
          FROM quotes
          WHERE substr(quote_time, 1, 10) = ?
        ),
        latest_quotes AS (
          SELECT *
          FROM daily_quotes
          WHERE rn_desc = 1
        ),
        opening_quotes AS (
          SELECT symbol, current_price AS first_price, quote_time AS first_quote_time
          FROM daily_quotes
          WHERE rn_asc = 1
        ),
        latest_signals AS (
          SELECT s.*, ROW_NUMBER() OVER (PARTITION BY s.symbol ORDER BY s.signal_time DESC, s.id DESC) AS rn_desc
          FROM signals s
          WHERE substr(s.signal_time, 1, 10) = ?
        )
        SELECT q.symbol, q.code, q.name, q.quote_time, q.current_price, q.open_price, q.previous_close,
               q.high_price, q.low_price, q.change_percent, q.turnover_yuan,
               o.first_price, o.first_quote_time,
               s.signal_level, d.action, d.score, d.confidence, d.regime, d.rationale_json, d.risk_flags_json,
               d.trade_advice, d.trade_size_hint, d.entry_note
        FROM latest_quotes q
        LEFT JOIN opening_quotes o ON o.symbol = q.symbol
        LEFT JOIN latest_signals s ON s.symbol = q.symbol AND s.rn_desc = 1
        LEFT JOIN decision_signals d ON d.signal_id = s.id
        ORDER BY COALESCE(d.score, 0) DESC, q.symbol ASC
        """,
        (trade_date, trade_date),
    ).fetchall()
    return [
        {
            "symbol": row["symbol"],
            "code": row["code"],
            "name": row["name"],
            "quote_time": row["quote_time"],
            "current_price": round(float(row["current_price"]), 3),
            "open_price": round(float(row["open_price"]), 3),
            "previous_close": round(float(row["previous_close"]), 3),
            "high_price": round(float(row["high_price"]), 3),
            "low_price": round(float(row["low_price"]), 3),
            "change_percent": round(float(row["change_percent"]), 2),
            "turnover_yuan": round(float(row["turnover_yuan"]), 2),
            "first_price": round(float(row["first_price"]), 3) if row["first_price"] is not None else None,
            "first_quote_time": row["first_quote_time"],
            "signal_level": row["signal_level"] or "UNKNOWN",
            "action": row["action"] or "unknown",
            "score": round(float(row["score"]), 2) if row["score"] is not None else None,
            "confidence": row["confidence"] or "unknown",
            "regime": row["regime"] or "unknown",
            "rationale": json.loads(row["rationale_json"]) if row["rationale_json"] else [],
            "risk_flags": json.loads(row["risk_flags_json"]) if row["risk_flags_json"] else [],
            "trade_advice": row["trade_advice"] or "",
            "trade_size_hint": row["trade_size_hint"] or "",
            "entry_note": row["entry_note"] or "",
        }
        for row in rows
    ]


def fetch_latest_trade_date(conn: sqlite3.Connection) -> str | None:
    row = conn.execute(
        """
        SELECT substr(MAX(quote_time), 1, 10) AS trade_date
        FROM quotes
        """
    ).fetchone()
    if row is None:
        return None
    return row["trade_date"]


def _row_to_quote(row: sqlite3.Row) -> StockQuote:
    return StockQuote(
        provider=row["provider"],
        symbol=row["symbol"],
        code=row["code"],
        name=row["name"],
        current_price=_decimal(row["current_price"]),
        open_price=_decimal(row["open_price"]),
        previous_close=_decimal(row["previous_close"]),
        high_price=_decimal(row["high_price"]),
        low_price=_decimal(row["low_price"]),
        change_amount=_decimal(row["change_amount"]),
        change_percent=_decimal(row["change_percent"]),
        volume_shares=_decimal(row["volume_shares"]),
        turnover_yuan=_decimal(row["turnover_yuan"]),
        quote_time=datetime.fromisoformat(row["quote_time"]),
        raw_payload=row["raw_payload"] or "",
    )


def _decimal(value: object) -> Decimal:
    return Decimal(str(value))


def _ensure_column(conn: sqlite3.Connection, table_name: str, column_name: str, ddl: str) -> None:
    rows = conn.execute(f"PRAGMA table_info({table_name})").fetchall()
    columns = {row["name"] for row in rows}
    if column_name in columns:
        return
    conn.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {ddl}")


def _summarize_returns(values: list[float]) -> dict:
    if not values:
        return {"samples": 0, "avg": None, "median": None, "win_rate": None, "min": None, "max": None}
    wins = sum(1 for v in values if v > 0)
    return {
        "samples": len(values),
        "avg": round(sum(values) / len(values), 4),
        "median": round(median(values), 4),
        "win_rate": round(wins / len(values) * 100, 2),
        "min": round(min(values), 4),
        "max": round(max(values), 4),
    }
