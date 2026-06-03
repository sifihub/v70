from __future__ import annotations

import json
import re
from datetime import datetime, timedelta

MISSION_TERMS = {
    "geopolitics", "war", "conflict", "foreign", "policy", "world",
    "politics", "political", "election", "government", "military",
    "strategy", "intelligence", "diplomacy", "history", "macro", "defense"
}

MISSION_PHRASES = {
    "world order", "foreign policy", "war update", "global conflict",
    "military strategy", "political debate", "election strategy",
    "government reform", "diplomatic relations", "historical precedent"
}

SHOPPING_TERMS = {
    "shopping", "shop", "store", "stores", "clothing", "product", "products",
    "order", "orders", "checkout", "cart", "coupon", "discount", "sale",
    "delivery", "returns", "exchange", "sizes", "crypto", "bitcoin", "ethereum",
    "solana", "meme", "funny"
}

SHOPPING_PHRASES = {
    "product info", "order support", "customer support", "customer service",
    "crypto market", "bitcoin etf", "crypto regulation"
}

class TrendHunter:
    def __init__(self) -> None:
        self.topic_groups = {
            "geopolitics": ["geopolitics", "war update", "global conflict", "world order", "foreign policy"],
            "politics": ["politics", "policy debate", "election strategy", "government reform"],
            "military": ["military strategy", "defense policy", "intelligence update"],
        }
        self.seed_topics = [item for values in self.topic_groups.values() for item in values]

    def default_queries(self) -> list[str]:
        since = (datetime.utcnow() - timedelta(days=3)).strftime("%Y-%m-%d")
        return [
            f"politics OR geopolitics min_faves:60 min_replies:5 lang:en since:{since}",
            f"geopolitics filter:videos min_faves:210 min_retweets:24 min_replies:10 lang:en since:{since}",
            f"war update filter:videos min_faves:175 min_retweets:20 min_replies:9 lang:en since:{since}",
            f"foreign policy filter:images min_faves:170 min_retweets:21 min_replies:8 lang:en since:{since}",
            f"politics filter:images min_faves:190 min_retweets:21 min_replies:11 lang:en since:{since}",
            f"military OR defense filter:videos min_faves:130 min_retweets:14 min_replies:7 lang:en since:{since}",
        ]

    def parse_queries(self, raw: str) -> list[str]:
        try:
            parsed = json.loads(raw)
        except Exception:
            return []
        if not isinstance(parsed, list):
            return []
        queries: list[str] = []
        seen = set()
        for item in parsed:
            query = str(item or "").strip()
            if not query or query in seen:
                continue
            if not self._query_is_on_mission(query):
                continue
            seen.add(query)
            queries.append(query)
        return queries

    def _query_is_on_mission(self, query: str) -> bool:
        lowered = (query or "").lower()
        tokens = set(re.findall(r"[a-z]{3,}", lowered))
        has_mission_phrase = any(phrase in lowered for phrase in MISSION_PHRASES)
        if any(phrase in lowered for phrase in SHOPPING_PHRASES):
            return False
        if (tokens & SHOPPING_TERMS) and not has_mission_phrase:
            return False
        return bool((tokens & MISSION_TERMS) or has_mission_phrase)

    def compose_queries(self, memory_briefs: list[str], limit: int = 8) -> list[str]:
        since = (datetime.utcnow() - timedelta(days=3)).strftime("%Y-%m-%d")
        queries: list[str] = []
        seen = set()

        def add(query: str) -> None:
            query = " ".join((query or "").split()).strip()
            if not query or query in seen:
                return
            seen.add(query)
            queries.append(query)

        for query in self.default_queries():
            add(query)

        boosted_topics: list[str] = []
        for item in memory_briefs:
            text = (item or "").strip().lower()
            if not text:
                continue
            for match in re.findall(r"[a-z]{4,}", text):
                if match in {"signal", "source", "trend", "memory", "posts", "fresh", "strongest", "ignored"}:
                    continue
                if match in SHOPPING_TERMS or match not in MISSION_TERMS:
                    continue
                boosted_topics.append(match)

        for index, topic in enumerate(boosted_topics[:12]):
            media_filter = "filter:videos" if index % 2 == 0 else "filter:images"
            add(f"{topic} {media_filter} min_faves:150 min_retweets:14 min_replies:6 lang:en since:{since}")

        return queries[:limit]

    def fallback_results(self, queries: list[str]) -> list[dict]:
        results = []
        for query in queries[:6]:
            topic = query.split("min_", 1)[0].replace("lang:en", "").replace("since:", "").strip()
            results.append(
                {
                    "query": query,
                    "topic": topic,
                    "user": "trend-sim",
                    "text": f"{topic.title()} is shifting faster than most people realize. The geopolitical landscape is changing.",
                    "url": "",
                    "image_url": "",
                    "simulated": True,
                    "metrics": {"engagement_hint": 1200},
                }
            )
        return results
