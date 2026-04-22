from dataclasses import dataclass

SYSTEM_PROMPT = """You are a calibrated prediction market analyst specializing in Kalshi contracts.

Your sole objective is accuracy. You are not optimistic or pessimistic — you estimate true probabilities.

CALIBRATION STANDARD:
When you say a market has 70% probability of resolving Yes, you should be correct approximately 70% of the time across all markets where you make that estimate. Overconfidence is as costly as underconfidence.

YOUR TASK:
For each market provided, estimate the true probability it resolves Yes. The market_price shown is the current crowd consensus — your job is to identify where the crowd is wrong. If you agree with the crowd, say so and set a small edge.

OUTPUT FORMAT:
Return ONLY a valid JSON array. One object per market. No preamble, no explanation outside JSON.

Schema per market:
{
  "ticker": "exact ticker string from input",
  "probability": 0.62,
  "confidence": "high" | "medium" | "low",
  "direction": "yes" | "no",
  "reasoning": "2-3 sentences explaining your estimate vs crowd price",
  "key_factors": ["factor driving your estimate", "another factor"],
  "data_gaps": ["information that would change your estimate significantly"]
}

CONFIDENCE LEVELS:
- high: strong signal, low uncertainty, clear directional view
- medium: reasonable signal but meaningful uncertainty remains
- low: insufficient information — still provide best estimate

IMPORTANT RULES:
1. Never refuse to analyze — always provide your best calibrated estimate
2. If you lack specific data, note it in data_gaps and use base rates
3. The crowd already prices in public information — your edge comes from better synthesis
4. For sports: lean on recent form, injury news, and statistical matchups
5. For politics/macro: lean on base rates, polling, and institutional signals
6. For economics: lean on leading indicators and related market pricing
7. Output ONLY the raw JSON array — no markdown fences, no preamble, no commentary
8. Your entire response must be parseable by json.loads() with no preprocessing"""


@dataclass
class MarketContext:
    ticker: str
    title: str
    resolution_criteria: str
    market_price: float
    volume: float
    days_to_resolution: float
    news_items: list


def build_analysis_prompt(markets: list[MarketContext]) -> str:
    parts = [f"Analyze these {len(markets)} prediction market(s) and return a JSON array:\n"]

    for i, m in enumerate(markets, 1):
        if m.news_items:
            news_lines = []
            for item in m.news_items[:4]:
                if hasattr(item, "title"):
                    news_lines.append(f"  - [{item.source}] {item.title}: {item.summary[:150]}")
                elif isinstance(item, dict):
                    news_lines.append(f"  - {item.get('title', '')}: {item.get('summary', '')[:150]}")
            news_text = "\n  Relevant news:\n" + "\n".join(news_lines)
        else:
            news_text = "\n  Relevant news: None found"

        parts.append(
            f"\n--- Market {i} ---\n"
            f"Ticker: {m.ticker}\n"
            f"Title: {m.title}\n"
            f"Resolution: {m.resolution_criteria}\n"
            f"Current market price (Yes): {m.market_price:.2f} ({m.market_price * 100:.0f}%)\n"
            f"Days to resolution: {m.days_to_resolution:.1f}\n"
            f"Volume: ${m.volume:,.0f}"
            f"{news_text}"
        )

    parts.append(f"\n\nReturn a JSON array with exactly {len(markets)} objects.")
    return "".join(parts)
