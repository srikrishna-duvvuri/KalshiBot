import json
import logging
import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path

logger = logging.getLogger("accounting.ledger")


class Ledger:
    def __init__(self, db_path: str):
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._db_path = db_path
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def _init_schema(self):
        with self._connect() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS opportunities (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at TEXT NOT NULL,
                    market_ticker TEXT NOT NULL,
                    market_title TEXT NOT NULL,
                    market_price REAL NOT NULL,
                    claude_probability REAL NOT NULL,
                    edge REAL NOT NULL,
                    kelly_fraction REAL NOT NULL,
                    bet_size_dollars REAL NOT NULL,
                    confidence TEXT NOT NULL,
                    direction TEXT NOT NULL,
                    reasoning TEXT,
                    key_factors TEXT,
                    data_gaps TEXT,
                    acted_on INTEGER DEFAULT 0,
                    claude_call_id INTEGER,
                    FOREIGN KEY (claude_call_id) REFERENCES claude_calls(id)
                );
                CREATE TABLE IF NOT EXISTS trades (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    opportunity_id INTEGER,
                    opened_at TEXT NOT NULL,
                    closed_at TEXT,
                    market_ticker TEXT NOT NULL,
                    direction TEXT NOT NULL,
                    contracts REAL NOT NULL,
                    entry_price REAL NOT NULL,
                    exit_price REAL,
                    gross_pnl REAL,
                    kalshi_fee REAL,
                    net_pnl REAL,
                    status TEXT DEFAULT 'open',
                    resolved_yes INTEGER,
                    FOREIGN KEY (opportunity_id) REFERENCES opportunities(id)
                );
                CREATE TABLE IF NOT EXISTS claude_calls (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    called_at TEXT NOT NULL,
                    model TEXT NOT NULL,
                    markets_analyzed INTEGER NOT NULL,
                    input_tokens INTEGER DEFAULT 0,
                    output_tokens INTEGER DEFAULT 0,
                    cache_write_tokens INTEGER DEFAULT 0,
                    cache_read_tokens INTEGER DEFAULT 0,
                    cost_usd REAL NOT NULL,
                    running_daily_cost REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS calibration (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    opportunity_id INTEGER NOT NULL,
                    market_ticker TEXT NOT NULL,
                    predicted_probability REAL NOT NULL,
                    market_price_at_analysis REAL NOT NULL,
                    resolved_yes INTEGER,
                    resolution_date TEXT,
                    brier_contribution REAL,
                    FOREIGN KEY (opportunity_id) REFERENCES opportunities(id)
                );
                CREATE INDEX IF NOT EXISTS idx_opp_ticker   ON opportunities(market_ticker);
                CREATE INDEX IF NOT EXISTS idx_opp_acted    ON opportunities(acted_on);
                CREATE INDEX IF NOT EXISTS idx_trade_ticker ON trades(market_ticker);
                CREATE INDEX IF NOT EXISTS idx_trade_status ON trades(status);
                CREATE INDEX IF NOT EXISTS idx_cal_ticker   ON calibration(market_ticker);
                CREATE INDEX IF NOT EXISTS idx_claude_date  ON claude_calls(called_at);
            """)
            conn.commit()

    def record_opportunity(
        self, market_ticker, market_title, market_price, claude_probability,
        edge, kelly_fraction, bet_size_dollars, confidence, direction,
        reasoning, key_factors, data_gaps, claude_call_id,
    ) -> int:
        now = datetime.now().isoformat()
        with self._connect() as conn:
            cur = conn.execute(
                """INSERT INTO opportunities
                   (created_at,market_ticker,market_title,market_price,claude_probability,
                    edge,kelly_fraction,bet_size_dollars,confidence,direction,
                    reasoning,key_factors,data_gaps,claude_call_id)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (now, market_ticker, market_title, market_price, claude_probability,
                 edge, kelly_fraction, bet_size_dollars, confidence, direction,
                 reasoning, json.dumps(key_factors), json.dumps(data_gaps), claude_call_id),
            )
            opp_id = cur.lastrowid
            conn.execute(
                "INSERT INTO calibration (opportunity_id,market_ticker,predicted_probability,market_price_at_analysis) VALUES (?,?,?,?)",
                (opp_id, market_ticker, claude_probability, market_price),
            )
            conn.commit()
        return opp_id

    def record_trade(self, opportunity_id, direction, contracts, entry_price) -> int:
        now = datetime.now().isoformat()
        with self._connect() as conn:
            cur = conn.execute(
                """INSERT INTO trades (opportunity_id,opened_at,market_ticker,direction,contracts,entry_price)
                   SELECT ?,?,market_ticker,?,?,? FROM opportunities WHERE id=?""",
                (opportunity_id, now, direction, contracts, entry_price, opportunity_id),
            )
            trade_id = cur.lastrowid
            conn.execute("UPDATE opportunities SET acted_on=1 WHERE id=?", (opportunity_id,))
            conn.commit()
        return trade_id

    def settle_trade(self, trade_id, resolved_yes, kalshi_fee=0.0):
        now = datetime.now().isoformat()
        with self._connect() as conn:
            row = conn.execute(
                "SELECT direction,contracts,entry_price,opportunity_id FROM trades WHERE id=?", (trade_id,)
            ).fetchone()
            if not row:
                logger.error("Trade %d not found", trade_id)
                return

            direction, contracts, entry_price, opp_id = row
            outcome = 1 if resolved_yes else 0

            if direction == "yes":
                gross_pnl = contracts * (1.0 - entry_price) if resolved_yes else -contracts * entry_price
            else:
                gross_pnl = contracts * (1.0 - entry_price) if not resolved_yes else -contracts * entry_price

            net_pnl = gross_pnl - kalshi_fee

            conn.execute(
                """UPDATE trades SET status='settled',resolved_yes=?,closed_at=?,
                   gross_pnl=?,kalshi_fee=?,net_pnl=?,exit_price=? WHERE id=?""",
                (outcome, now, gross_pnl, kalshi_fee, net_pnl, float(resolved_yes), trade_id),
            )

            conn.commit()

        logger.info("Trade %d settled: resolved_yes=%s gross=%.2f net=%.2f", trade_id, resolved_yes, gross_pnl, net_pnl)

    def record_claude_call(self, model, markets_analyzed, input_tokens, output_tokens,
                           cache_write_tokens, cache_read_tokens, cost_usd) -> int:
        now = datetime.now().isoformat()
        running = self.get_daily_claude_cost() + cost_usd
        with self._connect() as conn:
            cur = conn.execute(
                """INSERT INTO claude_calls
                   (called_at,model,markets_analyzed,input_tokens,output_tokens,
                    cache_write_tokens,cache_read_tokens,cost_usd,running_daily_cost)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (now, model, markets_analyzed, input_tokens, output_tokens,
                 cache_write_tokens, cache_read_tokens, cost_usd, running),
            )
            call_id = cur.lastrowid
            conn.commit()
        logger.info("Claude call recorded: %d markets $%.4f (daily $%.4f)", markets_analyzed, cost_usd, running)
        return call_id

    def get_daily_claude_cost(self, for_date=None) -> float:
        target = (for_date or date.today()).isoformat()
        with self._connect() as conn:
            row = conn.execute(
                "SELECT SUM(cost_usd) FROM claude_calls WHERE DATE(called_at)=?", (target,)
            ).fetchone()
            return float(row[0] or 0.0)

    def get_pnl_summary(self, days=None) -> dict:
        where, params = "", []
        if days is not None:
            cutoff = (datetime.now() - timedelta(days=days)).isoformat()
            where, params = "WHERE opened_at >= ?", [cutoff]

        with self._connect() as conn:
            total = conn.execute(f"SELECT COUNT(*) FROM trades {where}", params).fetchone()[0]
            settled_sql = (
                f"SELECT net_pnl,gross_pnl,kalshi_fee,resolved_yes FROM trades {where} AND status='settled'"
                if where else
                "SELECT net_pnl,gross_pnl,kalshi_fee,resolved_yes FROM trades WHERE status='settled'"
            )
            settled_rows = conn.execute(settled_sql, params).fetchall()
            open_sql = (
                f"SELECT COUNT(*) FROM trades {where} AND status='open'"
                if where else
                "SELECT COUNT(*) FROM trades WHERE status='open'"
            )
            open_count = conn.execute(open_sql, params).fetchone()[0]
            avg_edge = conn.execute(
                "SELECT AVG(edge) FROM opportunities WHERE acted_on=1" +
                (" AND created_at >= ?" if days else ""),
                params if days else [],
            ).fetchone()[0]
            avg_bet = conn.execute(
                f"SELECT AVG(contracts * entry_price) FROM trades {where}", params
            ).fetchone()[0]

        gross = sum(float(r[1] or 0) for r in settled_rows)
        fees = sum(float(r[2] or 0) for r in settled_rows)
        net = sum(float(r[0] or 0) for r in settled_rows)
        winners = sum(1 for r in settled_rows if float(r[1] or 0) > 0)
        settled_count = len(settled_rows)
        claude_cost = self.get_claude_cost_summary(days).get("total_cost", 0.0)

        return {
            "total_trades": total, "settled_trades": settled_count,
            "winning_trades": winners, "open_trades": open_count,
            "win_rate": winners / settled_count if settled_count > 0 else 0.0,
            "gross_pnl": gross, "total_kalshi_fees": fees,
            "total_claude_costs": claude_cost, "net_pnl": net - claude_cost,
            "avg_edge": float(avg_edge or 0), "avg_bet_size": float(avg_bet or 0),
        }

    def get_claude_cost_summary(self, days=None) -> dict:
        where, params = "", []
        if days is not None:
            cutoff = (datetime.now() - timedelta(days=days)).isoformat()
            where, params = "WHERE called_at >= ?", [cutoff]

        with self._connect() as conn:
            row = conn.execute(
                f"SELECT COUNT(*),SUM(markets_analyzed),SUM(input_tokens),SUM(output_tokens),"
                f"SUM(cache_write_tokens),SUM(cache_read_tokens),SUM(cost_usd) FROM claude_calls {where}",
                params,
            ).fetchone()

        calls, markets = int(row[0] or 0), int(row[1] or 0)
        it, ot = int(row[2] or 0), int(row[3] or 0)
        cwt, crt = int(row[4] or 0), int(row[5] or 0)
        total_cost = float(row[6] or 0.0)
        total_input = it + cwt + crt
        p = {"input": 3e-6, "output": 15e-6, "cache_write": 3.75e-6, "cache_read": 0.3e-6}

        return {
            "total_calls": calls, "total_markets_analyzed": markets,
            "total_tokens": total_input + ot,
            "cache_hit_rate": crt / total_input if total_input > 0 else 0.0,
            "input_cost": it * p["input"], "output_cost": ot * p["output"],
            "cache_write_cost": cwt * p["cache_write"], "cache_read_cost": crt * p["cache_read"],
            "total_cost": total_cost,
        }

    def get_open_opportunities(self) -> list[dict]:
        with self._connect() as conn:
            return [dict(r) for r in conn.execute(
                "SELECT * FROM opportunities WHERE acted_on=0 ORDER BY edge DESC"
            ).fetchall()]

    def get_open_trades(self) -> list[dict]:
        with self._connect() as conn:
            return [dict(r) for r in conn.execute("SELECT * FROM trades WHERE status='open'").fetchall()]

    def mark_opportunity_acted_on(self, opportunity_id) -> None:
        with self._connect() as conn:
            conn.execute("UPDATE opportunities SET acted_on=1 WHERE id=?", (opportunity_id,))
            conn.commit()

    def get_net_profit(self, days=None) -> float:
        return self.get_pnl_summary(days)["net_pnl"]
