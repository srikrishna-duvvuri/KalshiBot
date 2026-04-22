import json
import logging
import subprocess
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from config.settings import settings
from src.claude.prompts import SYSTEM_PROMPT, MarketContext, build_analysis_prompt
from src.sizing.kelly import KellyResult, calculate_kelly

logger = logging.getLogger("claude.analyzer")

# Pricing per token in USD (for cost tracking when using direct API key fallback)
CLAUDE_PRICING = {
    "claude-sonnet-4-6": {
        "input":       3.00 / 1_000_000,
        "output":      15.00 / 1_000_000,
        "cache_write": 3.75 / 1_000_000,
        "cache_read":  0.30 / 1_000_000,
    },
    "claude-opus-4-6": {
        "input":       15.00 / 1_000_000,
        "output":      75.00 / 1_000_000,
        "cache_write": 18.75 / 1_000_000,
        "cache_read":   1.50 / 1_000_000,
    },
}

# Path to the auth helper script — reads Claude Code's OAuth token from macOS Keychain
_AUTH_HELPER = Path(__file__).parents[2] / "scripts" / "claude_auth_helper.sh"

# JSON Schema enforced on every CLI call — guarantees structured output, no code fences
_RESPONSE_SCHEMA = {
    "type": "array",
    "items": {
        "type": "object",
        "required": ["ticker", "probability", "confidence", "direction", "reasoning", "key_factors", "data_gaps"],
        "additionalProperties": False,
        "properties": {
            "ticker":      {"type": "string"},
            "probability": {"type": "number", "minimum": 0.0, "maximum": 1.0},
            "confidence":  {"type": "string", "enum": ["high", "medium", "low"]},
            "direction":   {"type": "string", "enum": ["yes", "no"]},
            "reasoning":   {"type": "string"},
            "key_factors": {"type": "array", "items": {"type": "string"}},
            "data_gaps":   {"type": "array", "items": {"type": "string"}},
        },
    },
}


@dataclass
class Opportunity:
    ticker: str
    title: str
    market_price: float
    claude_probability: float
    confidence: str
    direction: str
    edge: float
    kelly_result: KellyResult
    reasoning: str
    key_factors: list
    data_gaps: list
    claude_call_id: int = 0


class ClaudeAnalyzer:
    def __init__(self, bankroll: float):
        self._bankroll = bankroll
        self._daily_cost: float = 0.0
        self._daily_cost_date: date = date.today()

    def update_bankroll(self, bankroll: float) -> None:
        self._bankroll = bankroll

    @property
    def daily_cost(self) -> float:
        if date.today() != self._daily_cost_date:
            self._daily_cost = 0.0
            self._daily_cost_date = date.today()
        return self._daily_cost

    def analyze_markets(self, markets: list[MarketContext]) -> tuple[list[Opportunity], dict]:
        if not markets:
            return [], {}

        if self.daily_cost >= settings.claude_daily_budget_usd:
            logger.warning(
                "Daily Claude budget $%.2f reached. Skipping analysis.",
                settings.claude_daily_budget_usd,
            )
            return [], {}

        all_opportunities: list[Opportunity] = []
        aggregate_usage = {
            "input_tokens": 0, "output_tokens": 0,
            "cache_write_tokens": 0, "cache_read_tokens": 0,
            "cost_usd": 0.0, "markets_analyzed": 0,
        }

        batch_size = settings.max_markets_per_claude_call
        for i in range(0, len(markets), batch_size):
            batch = markets[i:i + batch_size]
            try:
                analyses, usage = self._call_claude(batch)
                for key in ("input_tokens", "output_tokens", "cache_write_tokens", "cache_read_tokens"):
                    aggregate_usage[key] += usage.get(key, 0)
                aggregate_usage["cost_usd"] += usage.get("cost_usd", 0.0)
                aggregate_usage["markets_analyzed"] += len(batch)
                self._daily_cost += usage.get("cost_usd", 0.0)

                # Option A: escalate low-confidence / high-edge markets to Opus
                for j, (analysis, market) in enumerate(zip(analyses, batch)):
                    if analysis.get("confidence") == "low":
                        raw_edge = abs(float(analysis.get("probability", 0.5)) - market.market_price)
                        if raw_edge >= settings.min_edge_to_escalate:
                            logger.info(
                                "Escalating %s to Opus (low confidence, edge=%.2f)",
                                market.ticker, raw_edge,
                            )
                            try:
                                [escalated], esc_usage = self._call_claude([market], model_override="claude-opus-4-6")
                                analyses[j] = escalated
                                for key in ("input_tokens", "output_tokens", "cache_write_tokens", "cache_read_tokens"):
                                    aggregate_usage[key] += esc_usage.get(key, 0)
                                aggregate_usage["cost_usd"] += esc_usage.get("cost_usd", 0.0)
                                self._daily_cost += esc_usage.get("cost_usd", 0.0)
                            except Exception as e:
                                logger.error("Opus escalation failed for %s: %s", market.ticker, e)

                for analysis, market in zip(analyses, batch):
                    opp = self._build_opportunity(analysis, market)
                    if opp is not None:
                        all_opportunities.append(opp)

            except Exception as e:
                logger.error("Claude batch %d-%d failed: %s", i, i + batch_size, e)

        logger.info(
            "Claude: %d markets → %d opportunities, $%.4f (daily $%.4f)",
            aggregate_usage["markets_analyzed"], len(all_opportunities),
            aggregate_usage["cost_usd"], self.daily_cost,
        )
        return all_opportunities, aggregate_usage

    def _call_claude(self, batch: list[MarketContext], model_override: str = None) -> tuple[list[dict], dict]:
        prompt = build_analysis_prompt(batch)

        if settings.use_claude_code_cli:
            return self._call_via_cli(prompt, model_override=model_override)
        else:
            return self._call_via_sdk(prompt, model_override=model_override)

    def _call_via_cli(self, prompt: str, model_override: str = None) -> tuple[list[dict], dict]:
        """Call Claude via the local `claude` CLI subprocess (uses existing Claude Code auth)."""
        import os
        model = model_override or settings.claude_model
        cmd = [
            "claude", "--print",
            "--bare",
            "--no-session-persistence",
            "--output-format", "json",
            "--model", model,
            "--system-prompt", SYSTEM_PROMPT,
            "--settings", json.dumps({"apiKeyHelper": str(_AUTH_HELPER)}),
        ]

        # Remove ANTHROPIC_API_KEY from the subprocess env — if dotenv loaded a placeholder
        # value, claude --bare would use it instead of apiKeyHelper and get a 401.
        env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}

        result = subprocess.run(
            cmd,
            input=prompt,
            capture_output=True,
            text=True,
            timeout=120,
            env=env,
        )

        if result.returncode != 0:
            # Parse the JSON error envelope if present (claude --bare always writes JSON to stdout)
            try:
                err_data = json.loads(result.stdout)
                msg = err_data.get("result", result.stderr or "unknown error")
            except Exception:
                msg = result.stderr or result.stdout or "unknown error"
            raise RuntimeError(f"claude CLI exited {result.returncode}: {msg[:300]}")

        data = json.loads(result.stdout)

        if data.get("is_error"):
            msg = data.get("result", "unknown")
            logger.error("claude CLI API error (status %s): %s", data.get("api_error_status"), msg)
            raise RuntimeError(f"claude CLI error (HTTP {data.get('api_error_status')}): {msg[:200]}")

        raw = data.get("result", "[]")
        parsed = self._parse_json_response(raw)

        # Extract usage from modelUsage (keyed by model name)
        model_usage = data.get("modelUsage", {}).get(model, {})
        usage_dict = {
            "input_tokens":       model_usage.get("inputTokens", 0),
            "output_tokens":      model_usage.get("outputTokens", 0),
            "cache_write_tokens": model_usage.get("cacheCreationInputTokens", 0),
            "cache_read_tokens":  model_usage.get("cacheReadInputTokens", 0),
            "cost_usd":           model_usage.get("costUSD", data.get("total_cost_usd", 0.0)),
        }

        return parsed, usage_dict

    def _call_via_sdk(self, prompt: str, model_override: str = None) -> tuple[list[dict], dict]:
        """Call Claude via the Anthropic SDK (requires ANTHROPIC_API_KEY in .env)."""
        import anthropic
        model = model_override or settings.claude_model
        client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
        response = client.messages.create(
            model=model,
            max_tokens=2000,
            system=[{
                "type": "text",
                "text": SYSTEM_PROMPT,
                "cache_control": {"type": "ephemeral"},
            }],
            messages=[{"role": "user", "content": prompt}],
        )

        usage = response.usage
        cost = self._calculate_sdk_cost(usage, model)
        usage_dict = {
            "input_tokens":       getattr(usage, "input_tokens", 0),
            "output_tokens":      getattr(usage, "output_tokens", 0),
            "cache_write_tokens": getattr(usage, "cache_creation_input_tokens", 0),
            "cache_read_tokens":  getattr(usage, "cache_read_input_tokens", 0),
            "cost_usd":           cost,
        }

        raw = response.content[0].text if response.content else "[]"
        return self._parse_json_response(raw), usage_dict

    def _parse_json_response(self, raw: str) -> list[dict]:
        if "```" in raw:
            raw = "\n".join(l for l in raw.split("\n") if not l.strip().startswith("```"))
        try:
            parsed = json.loads(raw.strip())
            return [parsed] if isinstance(parsed, dict) else parsed
        except json.JSONDecodeError as e:
            logger.error("Failed to parse Claude JSON: %s\nRaw: %s", e, raw[:300])
            return []

    def _calculate_sdk_cost(self, usage, model: str = None) -> float:
        p = CLAUDE_PRICING.get(model or settings.claude_model, CLAUDE_PRICING["claude-sonnet-4-6"])
        return (
            getattr(usage, "input_tokens", 0) * p["input"] +
            getattr(usage, "output_tokens", 0) * p["output"] +
            getattr(usage, "cache_creation_input_tokens", 0) * p["cache_write"] +
            getattr(usage, "cache_read_input_tokens", 0) * p["cache_read"]
        )

    def _build_opportunity(self, analysis: dict, market: MarketContext) -> "Opportunity | None":
        try:
            claude_p = float(analysis.get("probability", 0.5))
            confidence = analysis.get("confidence", "low")

            confidence_rank = {"low": 0, "medium": 1, "high": 2}
            if confidence_rank.get(confidence, 0) < confidence_rank.get(settings.min_confidence, 1):
                return None

            kelly_result = calculate_kelly(
                claude_p=claude_p,
                market_price=market.market_price,
                bankroll=self._bankroll,
                fee_rate=settings.kalshi_fee_rate,
                max_position_pct=settings.max_position_pct,
            )

            if not kelly_result.is_worthwhile:
                return None

            return Opportunity(
                ticker=market.ticker,
                title=market.title,
                market_price=market.market_price,
                claude_probability=claude_p,
                confidence=confidence,
                direction=analysis.get("direction", kelly_result.direction),
                edge=kelly_result.edge,
                kelly_result=kelly_result,
                reasoning=analysis.get("reasoning", ""),
                key_factors=analysis.get("key_factors", []),
                data_gaps=analysis.get("data_gaps", []),
            )
        except Exception as e:
            logger.error("Failed to build opportunity: %s", e)
            return None
