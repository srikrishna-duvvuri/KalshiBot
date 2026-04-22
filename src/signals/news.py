import feedparser
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from time import mktime

from config.settings import settings

logger = logging.getLogger("signals.news")

RSS_FEEDS = [
    ("Reuters Top News",   "https://feeds.reuters.com/reuters/topNews"),
    ("Reuters Business",   "https://feeds.reuters.com/reuters/businessNews"),
    ("BBC News",           "http://feeds.bbci.co.uk/news/rss.xml"),
    ("NYT Home",           "https://rss.nytimes.com/services/xml/rss/nyt/HomePage.xml"),
    ("NPR News",           "https://feeds.npr.org/1001/rss.xml"),
    ("Politico",           "https://www.politico.com/rss/politics08.xml"),
    ("CNBC",               "https://www.cnbc.com/id/100003114/device/rss/rss.html"),
    ("ESPN",               "https://www.espn.com/espn/rss/news"),
    ("Yahoo Finance",      "https://finance.yahoo.com/news/rssindex"),
]

STOP_WORDS = {
    "a", "an", "the", "is", "are", "was", "were", "be", "been", "being",
    "have", "has", "had", "do", "does", "did", "will", "would", "could",
    "should", "may", "might", "shall", "can", "need", "must",
    "in", "on", "at", "by", "for", "of", "to", "from", "with", "into",
    "and", "or", "but", "not", "no", "nor", "so", "yet",
    "if", "when", "as", "than", "that", "this", "these", "those",
    "it", "its", "he", "she", "they", "we", "you", "i", "me",
    "who", "which", "what", "how", "where", "why",
    "also", "just", "about", "up", "out", "after", "before",
}


@dataclass
class NewsItem:
    title: str
    summary: str
    published: datetime
    source: str
    url: str


class NewsFetcher:
    def __init__(self):
        self._cache: list[NewsItem] = []
        self._last_refresh: datetime = datetime.min

    def get_relevant_news(self, market_title: str, max_items: int = 5) -> list[NewsItem]:
        self._maybe_refresh()
        keywords = self._extract_keywords(market_title)
        cutoff = datetime.now(timezone.utc) - timedelta(hours=48)

        def score(item: NewsItem) -> int:
            pub = item.published
            if pub.tzinfo is None:
                pub = pub.replace(tzinfo=timezone.utc)
            if pub < cutoff:
                return -1
            text = (item.title + " " + item.summary).lower()
            return sum(1 for kw in keywords if kw in text)

        scored = [(score(item), item) for item in self._cache]
        scored = [(s, item) for s, item in scored if s >= 0]
        scored.sort(key=lambda x: x[0], reverse=True)
        return [item for _, item in scored[:max_items]]

    def _maybe_refresh(self) -> None:
        elapsed = (datetime.now() - self._last_refresh).total_seconds()
        if elapsed >= settings.news_poll_interval_seconds:
            self.refresh_cache()

    def refresh_cache(self) -> None:
        items = self._fetch_rss()
        if settings.news_api_key:
            items.extend(self._fetch_newsapi())

        seen_urls: set[str] = set()
        unique: list[NewsItem] = []
        for item in items:
            if item.url not in seen_urls:
                seen_urls.add(item.url)
                unique.append(item)

        unique.sort(key=lambda x: x.published, reverse=True)
        self._cache = unique
        self._last_refresh = datetime.now()
        logger.info("News cache refreshed: %d items", len(unique))

    def _fetch_rss(self) -> list[NewsItem]:
        items: list[NewsItem] = []
        for source_name, url in RSS_FEEDS:
            try:
                feed = feedparser.parse(url)
                for entry in feed.entries[:20]:
                    published = self._parse_time(getattr(entry, "published_parsed", None))
                    items.append(NewsItem(
                        title=getattr(entry, "title", ""),
                        summary=getattr(entry, "summary", getattr(entry, "description", ""))[:500],
                        published=published,
                        source=source_name,
                        url=getattr(entry, "link", ""),
                    ))
            except Exception as e:
                logger.warning("RSS fetch failed for %s: %s", source_name, e)
        return items

    def _fetch_newsapi(self) -> list[NewsItem]:
        try:
            from newsapi import NewsApiClient
            client = NewsApiClient(api_key=settings.news_api_key)
            result = client.get_top_headlines(language="en", page_size=50)
            items: list[NewsItem] = []
            for article in result.get("articles", []):
                pub_str = article.get("publishedAt", "")
                try:
                    pub = datetime.fromisoformat(pub_str.replace("Z", "+00:00"))
                except Exception:
                    pub = datetime.now(timezone.utc)
                items.append(NewsItem(
                    title=article.get("title", ""),
                    summary=article.get("description", "") or "",
                    published=pub,
                    source=article.get("source", {}).get("name", "NewsAPI"),
                    url=article.get("url", ""),
                ))
            return items
        except Exception as e:
            logger.warning("NewsAPI fetch failed: %s", e)
            return []

    def _parse_time(self, t) -> datetime:
        if t is None:
            return datetime.now(timezone.utc)
        try:
            return datetime.fromtimestamp(mktime(t), tz=timezone.utc)
        except Exception:
            return datetime.now(timezone.utc)

    def _extract_keywords(self, market_title: str) -> list[str]:
        words = market_title.lower().replace("?", "").replace(",", "").split()
        return [w for w in words if w not in STOP_WORDS and len(w) >= 3]
