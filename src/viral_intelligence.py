from __future__ import annotations

import re
from collections import Counter

from .trend_hunter import MISSION_PHRASES, MISSION_TERMS, SHOPPING_PHRASES, SHOPPING_TERMS


class ViralIntelligence:
    def _metric_value(self, item: dict, key: str) -> int:
        metrics = item.get("metrics") or {}
        value = metrics.get(key, 0)
        try:
            return int(float(value or 0))
        except Exception:
            return 0

    def _emotion(self, text: str) -> str:
        lowered = text.lower()
        if any(word in lowered for word in ("shock", "wild", "insane", "unbelievable", "crazy")):
            return "shock"
        if any(word in lowered for word in ("future", "next", "coming", "soon")):
            return "curiosity"
        if any(word in lowered for word in ("fear", "risk", "collapse", "warning")):
            return "fear"
        if any(word in lowered for word in ("beautiful", "amazing", "incredible", "remarkable")):
            return "awe"
        if any(word in lowered for word in ("funny", "joke", "hilarious", "laugh")):
            return "humor"
        return "curiosity"

    def _hook(self, text: str) -> str:
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        if lines:
            return lines[0][:120]
        sentences = re.split(r"(?<=[.!?])\s+", text.strip())
        return sentences[0][:120] if sentences and sentences[0] else text[:120]

    def _topic(self, query: str, text: str, hinted_topic: str = "") -> str:
        if hinted_topic:
            return hinted_topic[:40]
        base = re.sub(r"\b(min_\w+:\d+|lang:\w+|since:\d{4}-\d{2}-\d{2})\b", "", query or "")
        blob = f"{base} {text[:220]}".lower()
        if any(phrase in blob for phrase in SHOPPING_PHRASES):
            return "off-topic"
        tokens = set(re.findall(r"[a-z]{3,}", blob))
        if (tokens & SHOPPING_TERMS) and not any(phrase in blob for phrase in MISSION_PHRASES):
            return "off-topic"
        if "war update" in blob:
            return "war"
        if any(phrase in blob for phrase in ("world order", "foreign policy", "global conflict")):
            return "geopolitics"
        category_map = (
            ("entertainment", ("entertainment", "celebrity", "movie", "music", "gaming", "streaming", "box office")),
            ("memes", ("meme", "memes", "humor", "funny", "viral")),
            ("crypto", ("crypto", "bitcoin", "ethereum", "solana", "blockchain", "defi", "web3")),
            ("war", ("war", "conflict", "ceasefire", "military", "nato", "ukraine", "russia", "israel", "gaza")),
            ("geopolitics", ("geopolitics", "foreign policy", "world order", "global conflict")),
            ("politics", ("politics", "political", "election", "government", "policy", "congress", "senate")),
        )
        for category, needles in category_map:
            if any(needle in blob for needle in needles):
                return category
        mission_words = [word for word in re.findall(r"[A-Za-z]{4,}", blob) if word.lower() in MISSION_TERMS]
        if mission_words:
            return Counter(word.lower() for word in mission_words).most_common(1)[0][0]
        return "off-topic"

    def _score(self, item: dict) -> float:
        text = str(item.get("text", ""))
        hook = self._hook(text)
        metrics = float((item.get("metrics") or {}).get("engagement_hint", 0) or 0)
        likes = self._metric_value(item, "likes")
        reposts = self._metric_value(item, "reposts")
        replies = self._metric_value(item, "replies")
        views = self._metric_value(item, "views")
        score = min(len(hook), 120) / 8.0
        score += 8.0 if "?" in hook else 0.0
        score += 6.0 if any(word in text.lower() for word in ("nobody", "why", "future", "just", "this")) else 0.0
        score += min(metrics / 700.0, 16.0)
        score += min(likes / 250.0, 26.0)
        score += min(reposts / 40.0, 22.0)
        score += min(replies / 28.0, 14.0)
        score += min(views / 18000.0, 32.0)
        if likes >= 5000:
            score += 10.0
        if views >= 200000:
            score += 14.0
        if replies >= 60:
            score += 12.0
        if item.get("video_url") or item.get("media_type") == "video":
            score += 16.0
        elif item.get("image_url"):
            score += 12.0
        return round(score, 2)

    def build_cards(self, posts: list[dict], limit: int = 8) -> list[dict]:
        cards = []
        for item in posts:
            text = str(item.get("text", "")).strip()
            if not text:
                continue
            cards.append(
                {
                    "topic": self._topic(str(item.get("query", "")), text, str(item.get("topic", "")).strip()),
                    "hook": self._hook(text),
                    "emotion": self._emotion(text),
                    "reason": "strong opening plus visible engagement signal",
                    "format": (
                        "video-post"
                        if item.get("video_url") or item.get("media_type") == "video"
                        else ("media-post" if item.get("image_url") else ("thread-like" if text.count("\n") >= 2 else "short-post"))
                    ),
                    "source_query": item.get("query", ""),
                    "source_url": item.get("url", ""),
                    "author_handle": item.get("user", ""),
                    "source_text": text,
                    "image_url": item.get("image_url", ""),
                    "video_url": item.get("video_url", ""),
                    "thumbnail_url": item.get("thumbnail_url", ""),
                    "media_type": item.get("media_type", ""),
                    "metrics": item.get("metrics", {}) or {},
                    "simulated": bool(item.get("simulated", False)),
                    "score": self._score(item),
                }
            )
        cards.sort(key=lambda item: item["score"], reverse=True)
        return cards[:limit]

    def topic_clusters(self, cards: list[dict], limit: int = 6) -> list[str]:
        topics = [str(item.get("topic", "")).strip() for item in cards if item.get("topic")]
        return [topic for topic, _ in Counter(topics).most_common(limit)]
