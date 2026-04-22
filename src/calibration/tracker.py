import sqlite3
import logging
from dataclasses import dataclass, field
from datetime import datetime

logger = logging.getLogger("calibration.tracker")


@dataclass
class CalibrationStats:
    total_predictions: int
    settled_predictions: int
    brier_score: float
    random_brier: float = 0.25
    accuracy_by_bucket: dict = field(default_factory=dict)


class CalibrationTracker:
    def __init__(self, db_path: str):
        self._db_path = db_path

    def record_settlement(self, market_ticker: str, resolved_yes: bool) -> int:
        outcome = 1 if resolved_yes else 0
        now = datetime.now().isoformat()

        with sqlite3.connect(self._db_path) as conn:
            rows = conn.execute(
                "SELECT id, predicted_probability FROM calibration "
                "WHERE market_ticker = ? AND resolved_yes IS NULL",
                (market_ticker,),
            ).fetchall()

            for row_id, predicted_p in rows:
                brier = (predicted_p - outcome) ** 2
                conn.execute(
                    "UPDATE calibration SET resolved_yes=?, resolution_date=?, brier_contribution=? WHERE id=?",
                    (outcome, now, brier, row_id),
                )
            conn.commit()

        logger.info("Settled calibration for %s: %d rows (resolved_yes=%s)", market_ticker, len(rows), resolved_yes)
        return len(rows)

    def get_calibration_stats(self) -> CalibrationStats:
        with sqlite3.connect(self._db_path) as conn:
            conn.row_factory = sqlite3.Row
            total = conn.execute("SELECT COUNT(*) FROM calibration").fetchone()[0]
            settled = conn.execute(
                "SELECT COUNT(*) FROM calibration WHERE resolved_yes IS NOT NULL"
            ).fetchone()[0]

            if settled == 0:
                return CalibrationStats(total_predictions=total, settled_predictions=0, brier_score=0.25)

            brier_row = conn.execute(
                "SELECT AVG(brier_contribution) FROM calibration WHERE brier_contribution IS NOT NULL"
            ).fetchone()
            brier_score = float(brier_row[0]) if brier_row[0] is not None else 0.25

            buckets = {
                "0-20%":   {"min": 0.0,  "max": 0.2,  "ps": 0.0, "as": 0.0, "n": 0},
                "20-40%":  {"min": 0.2,  "max": 0.4,  "ps": 0.0, "as": 0.0, "n": 0},
                "40-60%":  {"min": 0.4,  "max": 0.6,  "ps": 0.0, "as": 0.0, "n": 0},
                "60-80%":  {"min": 0.6,  "max": 0.8,  "ps": 0.0, "as": 0.0, "n": 0},
                "80-100%": {"min": 0.8,  "max": 1.01, "ps": 0.0, "as": 0.0, "n": 0},
            }

            for row in conn.execute(
                "SELECT predicted_probability, resolved_yes FROM calibration WHERE resolved_yes IS NOT NULL"
            ).fetchall():
                p, o = float(row[0]), int(row[1])
                for b in buckets.values():
                    if b["min"] <= p < b["max"]:
                        b["ps"] += p
                        b["as"] += o
                        b["n"] += 1
                        break

        accuracy_by_bucket = {
            label: {"predicted_avg": b["ps"] / b["n"], "actual_rate": b["as"] / b["n"], "count": b["n"]}
            for label, b in buckets.items() if b["n"] > 0
        }

        return CalibrationStats(
            total_predictions=total,
            settled_predictions=settled,
            brier_score=brier_score,
            accuracy_by_bucket=accuracy_by_bucket,
        )

    def get_correction_factor(self, raw_probability: float) -> float:
        stats = self.get_calibration_stats()
        if stats.settled_predictions < 10 or not stats.accuracy_by_bucket:
            return raw_probability

        if raw_probability < 0.2:
            label = "0-20%"
        elif raw_probability < 0.4:
            label = "20-40%"
        elif raw_probability < 0.6:
            label = "40-60%"
        elif raw_probability < 0.8:
            label = "60-80%"
        else:
            label = "80-100%"

        bucket = stats.accuracy_by_bucket.get(label)
        if bucket is None or bucket["count"] < 3:
            return raw_probability

        return 0.7 * raw_probability + 0.3 * bucket["actual_rate"]
