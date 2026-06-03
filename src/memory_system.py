from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path
from typing import Dict, List, Optional
from urllib.parse import urlsplit


class MemorySystem:
    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn: Optional[sqlite3.Connection] = sqlite3.connect(self.db_path, check_same_thread=False)
        self._init_database()

    def _init_database(self) -> None:
        cur = self.conn.cursor()
        cur.executescript(
            """
            CREATE TABLE IF NOT EXISTS beliefs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                belief_text TEXT NOT NULL UNIQUE,
                strength REAL DEFAULT 0.5,
                category TEXT DEFAULT 'philosophy',
                accession_count INTEGER DEFAULT 0,
                iteration_born INTEGER DEFAULT 1,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                last_accessed TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS memories (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                memory_type TEXT,
                content TEXT,
                summary TEXT,
                importance REAL DEFAULT 0.5,
                iteration INTEGER DEFAULT 1,
                metadata TEXT,
                timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS interactions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_handle TEXT,
                user_comment TEXT,
                my_reply TEXT,
                sentiment REAL,
                topics TEXT,
                timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS lineage (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                iteration_number INTEGER,
                repo_name TEXT,
                repo_url TEXT,
                memory_snapshot_path TEXT,
                notes TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                deleted_at TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS performance (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                iteration INTEGER,
                post_id TEXT,
                content TEXT,
                likes INTEGER DEFAULT 0,
                retweets INTEGER DEFAULT 0,
                replies INTEGER DEFAULT 0,
                virality_score REAL,
                posted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS reflections (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                week_start DATE,
                reflection_text TEXT,
                new_beliefs TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS working_memory (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                slot TEXT NOT NULL UNIQUE,
                content TEXT,
                metadata TEXT,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS selector_strategies (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                site TEXT,
                goal TEXT,
                selector TEXT,
                action TEXT,
                confidence REAL DEFAULT 0.5,
                notes TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                last_used TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS source_assets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                topic TEXT,
                source_url TEXT,
                author_handle TEXT,
                source_text TEXT,
                image_url TEXT,
                local_image_path TEXT,
                score REAL DEFAULT 0.0,
                metadata TEXT,
                captured_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS posted_sources (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                canonical_key TEXT NOT NULL UNIQUE,
                source_url TEXT,
                image_url TEXT,
                source_text TEXT,
                posted_content TEXT,
                metadata TEXT,
                posted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS engaged_sources (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                canonical_key TEXT NOT NULL UNIQUE,
                source_url TEXT,
                image_url TEXT,
                source_text TEXT,
                engagement_text TEXT,
                metadata TEXT,
                engaged_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            """
        )
        self.conn.commit()

    def add_belief(self, belief_text: str, category: str = "philosophy", strength: float = 0.5, iteration: int = 1) -> int:
        cur = self.conn.cursor()
        try:
            cur.execute(
                """
                INSERT INTO beliefs (belief_text, strength, category, iteration_born)
                VALUES (?, ?, ?, ?)
                """,
                (belief_text, strength, category, iteration),
            )
            self.conn.commit()
            return cur.lastrowid
        except sqlite3.IntegrityError:
            self.strengthen_belief(belief_text, 0.05)
            return -1

    def get_beliefs(self, limit: int = 10, min_strength: float = 0.3) -> List[Dict]:
        cur = self.conn.cursor()
        cur.execute(
            """
            SELECT belief_text, strength, category, accession_count, iteration_born
            FROM beliefs
            WHERE strength >= ?
            ORDER BY strength DESC, last_accessed DESC
            LIMIT ?
            """,
            (min_strength, limit),
        )
        return [
            {
                "text": row[0],
                "strength": row[1],
                "category": row[2],
                "access_count": row[3],
                "born": row[4],
            }
            for row in cur.fetchall()
        ]

    def strengthen_belief(self, belief_text: str, delta: float = 0.1) -> None:
        self.conn.execute(
            """
            UPDATE beliefs
            SET strength = MIN(1.0, strength + ?),
                accession_count = accession_count + 1,
                last_accessed = CURRENT_TIMESTAMP
            WHERE belief_text = ?
            """,
            (delta, belief_text),
        )
        self.conn.commit()

    def weaken_beliefs(self, decay_rate: float = 0.95, threshold: float = 0.1) -> None:
        self.conn.execute(
            """
            UPDATE beliefs
            SET strength = strength * ?
            WHERE last_accessed < datetime('now', '-7 days')
            """,
            (decay_rate,),
        )
        self.conn.execute(
            """
            DELETE FROM beliefs
            WHERE strength < ? AND accession_count < 3
            """,
            (threshold,),
        )
        self.conn.commit()

    def add_memory(
        self,
        content: str,
        memory_type: str,
        summary: str | None = None,
        importance: float = 0.5,
        iteration: int = 1,
        metadata: dict | None = None,
    ) -> None:
        summary = summary or (content[:100] + "..." if len(content) > 100 else content)
        self.conn.execute(
            """
            INSERT INTO memories (memory_type, content, summary, importance, iteration, metadata)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (memory_type, content, summary, importance, iteration, json.dumps(metadata) if metadata else None),
        )
        self.conn.commit()

    def recall_relevant_memories(self, query: str, limit: int = 5) -> List[Dict]:
        keywords = query.lower().split()
        if not keywords:
            return []
        conditions = " OR ".join(["LOWER(content) LIKE ?"] * len(keywords))
        params = [f"%{keyword}%" for keyword in keywords] + [limit]
        cur = self.conn.cursor()
        cur.execute(
            f"""
            SELECT content, summary, memory_type, timestamp, importance
            FROM memories
            WHERE {conditions}
            ORDER BY importance DESC, timestamp DESC
            LIMIT ?
            """,
            params,
        )
        return [
            {
                "content": row[0],
                "summary": row[1],
                "type": row[2],
                "timestamp": row[3],
                "importance": row[4],
            }
            for row in cur.fetchall()
        ]

    def get_recent_posts(self, limit: int = 5) -> List[Dict]:
        cur = self.conn.cursor()
        cur.execute(
            """
            SELECT content, summary, timestamp
            FROM memories
            WHERE memory_type = 'post'
            ORDER BY timestamp DESC
            LIMIT ?
            """,
            (limit,),
        )
        return [{"content": row[0], "summary": row[1], "timestamp": row[2]} for row in cur.fetchall()]

    def add_interaction(
        self,
        user_handle: str,
        user_comment: str,
        my_reply: str,
        sentiment: float = 0.5,
        topics: List[str] | None = None,
    ) -> None:
        self.conn.execute(
            """
            INSERT INTO interactions (user_handle, user_comment, my_reply, sentiment, topics)
            VALUES (?, ?, ?, ?, ?)
            """,
            (user_handle, user_comment, my_reply, sentiment, json.dumps(topics or [])),
        )
        self.conn.commit()

    def get_user_history(self, handle: str, limit: int = 3) -> List[Dict]:
        cur = self.conn.cursor()
        cur.execute(
            """
            SELECT user_comment, my_reply, timestamp
            FROM interactions
            WHERE user_handle = ?
            ORDER BY timestamp DESC
            LIMIT ?
            """,
            (handle, limit),
        )
        return [{"comment": row[0], "reply": row[1], "ts": row[2]} for row in cur.fetchall()]

    def record_lineage(self, iteration: int, repo_name: str, repo_url: str, snapshot_path: str | None = None, notes: str | None = None) -> None:
        self.conn.execute(
            """
            INSERT INTO lineage (iteration_number, repo_name, repo_url, memory_snapshot_path, notes)
            VALUES (?, ?, ?, ?, ?)
            """,
            (iteration, repo_name, repo_url, snapshot_path, notes),
        )
        self.conn.commit()

    def get_latest_lineage(self) -> Dict:
        cur = self.conn.cursor()
        cur.execute(
            """
            SELECT iteration_number, repo_name, repo_url, created_at, deleted_at
            FROM lineage
            ORDER BY iteration_number DESC
            LIMIT 1
            """
        )
        row = cur.fetchone()
        if row:
            return {
                "iteration": row[0],
                "repo_name": row[1],
                "repo_url": row[2],
                "created": row[3],
                "deleted": row[4],
            }
        return {"iteration": 0, "repo_name": "unknown", "repo_url": ""}

    def add_performance(self, iteration: int, post_id: str, content: str, likes: int = 0, retweets: int = 0, replies_count: int = 0) -> None:
        virality = (likes * 0.5) + (retweets * 0.3) + (replies_count * 0.2)
        self.conn.execute(
            """
            INSERT INTO performance (iteration, post_id, content, likes, retweets, replies, virality_score)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (iteration, post_id, content, likes, retweets, replies_count, virality),
        )
        self.conn.commit()

    def get_top_performers(self, days: int = 7, limit: int = 5) -> List[Dict]:
        cur = self.conn.cursor()
        cur.execute(
            """
            SELECT content, virality_score, likes, retweets, replies
            FROM performance
            WHERE posted_at > datetime('now', ?)
            ORDER BY virality_score DESC
            LIMIT ?
            """,
            (f"-{days} days", limit),
        )
        return [
            {"content": row[0], "virality": row[1], "likes": row[2], "retweets": row[3], "replies": row[4]}
            for row in cur.fetchall()
        ]

    def add_reflection(self, week_start: str, reflection_text: str, new_beliefs: List[str]) -> None:
        self.conn.execute(
            """
            INSERT INTO reflections (week_start, reflection_text, new_beliefs)
            VALUES (?, ?, ?)
            """,
            (week_start, reflection_text, json.dumps(new_beliefs)),
        )
        self.conn.commit()

    def set_working_memory(self, slot: str, content: str, metadata: dict | None = None) -> None:
        self.conn.execute(
            """
            INSERT INTO working_memory (slot, content, metadata, updated_at)
            VALUES (?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(slot) DO UPDATE SET
                content=excluded.content,
                metadata=excluded.metadata,
                updated_at=CURRENT_TIMESTAMP
            """,
            (slot, content, json.dumps(metadata) if metadata else None),
        )
        self.conn.commit()

    def get_working_memory(self, slot: str) -> Dict:
        cur = self.conn.cursor()
        cur.execute(
            """
            SELECT slot, content, metadata, updated_at
            FROM working_memory
            WHERE slot = ?
            ORDER BY updated_at DESC
            LIMIT 1
            """,
            (slot,),
        )
        row = cur.fetchone()
        if not row:
            return {}
        return {
            "slot": row[0],
            "content": row[1],
            "metadata": json.loads(row[2]) if row[2] else {},
            "updated_at": row[3],
        }

    def remember_selector(self, site: str, goal: str, selector: str, action: str, confidence: float = 0.5, notes: str = "") -> None:
        self.conn.execute(
            """
            INSERT INTO selector_strategies (site, goal, selector, action, confidence, notes)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (site, goal, selector, action, confidence, notes),
        )
        self.conn.commit()

    def get_selector_candidates(self, site: str, goal: str, limit: int = 5) -> List[Dict]:
        cur = self.conn.cursor()
        cur.execute(
            """
            SELECT site, goal, selector, action, confidence, notes, last_used
            FROM selector_strategies
            WHERE site = ? OR goal = ?
            ORDER BY confidence DESC, last_used DESC
            LIMIT ?
            """,
            (site, goal, limit),
        )
        return [
            {
                "site": row[0],
                "goal": row[1],
                "selector": row[2],
                "action": row[3],
                "confidence": row[4],
                "notes": row[5],
                "last_used": row[6],
            }
            for row in cur.fetchall()
        ]

    def add_source_asset(
        self,
        topic: str,
        source_url: str,
        author_handle: str,
        source_text: str,
        image_url: str = "",
        local_image_path: str = "",
        score: float = 0.0,
        metadata: dict | None = None,
    ) -> None:
        self.conn.execute(
            """
            INSERT INTO source_assets (topic, source_url, author_handle, source_text, image_url, local_image_path, score, metadata)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                topic,
                source_url,
                author_handle,
                source_text,
                image_url,
                local_image_path,
                score,
                json.dumps(metadata) if metadata else None,
            ),
        )
        self.conn.commit()

    def get_recent_source_assets(self, limit: int = 10) -> List[Dict]:
        cur = self.conn.cursor()
        cur.execute(
            """
            SELECT topic, source_url, author_handle, source_text, image_url, local_image_path, score, metadata, captured_at
            FROM source_assets
            ORDER BY captured_at DESC
            LIMIT ?
            """,
            (limit,),
        )
        return [
            {
                "topic": row[0],
                "source_url": row[1],
                "author_handle": row[2],
                "source_text": row[3],
                "image_url": row[4],
                "local_image_path": row[5],
                "score": row[6],
                "metadata": json.loads(row[7]) if row[7] else {},
                "captured_at": row[8],
            }
            for row in cur.fetchall()
        ]

    def _normalize_source_identity(self, value: str) -> str:
        raw = (value or "").strip()
        if not raw:
            return ""
        try:
            parts = urlsplit(raw)
        except Exception:
            parts = None
        if parts and parts.scheme and parts.netloc:
            return f"{parts.scheme.lower()}://{parts.netloc.lower()}{parts.path}".rstrip("/")
        return raw.rstrip("/")

    def posted_source_keys(self, source_url: str, image_url: str, source_text: str = "") -> List[str]:
        keys: List[str] = []
        normalized_source = self._normalize_source_identity(source_url)
        normalized_image = self._normalize_source_identity(image_url)
        normalized_text = re.sub(r"\s+", " ", (source_text or "").strip().lower())
        for prefix, value in (("source", normalized_source), ("image", normalized_image), ("text", normalized_text[:180])):
            if not value:
                continue
            key = f"{prefix}:{value}"
            if key not in keys:
                keys.append(key)
        return keys

    def record_posted_source(
        self,
        source_url: str,
        image_url: str,
        source_text: str = "",
        posted_content: str = "",
        metadata: dict | None = None,
    ) -> None:
        payload = json.dumps(metadata) if metadata else None
        for canonical_key in self.posted_source_keys(source_url, image_url, source_text):
            self.conn.execute(
                """
                INSERT INTO posted_sources (canonical_key, source_url, image_url, source_text, posted_content, metadata)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(canonical_key) DO UPDATE SET
                    source_url=excluded.source_url,
                    image_url=excluded.image_url,
                    source_text=excluded.source_text,
                    posted_content=excluded.posted_content,
                    metadata=excluded.metadata,
                    posted_at=CURRENT_TIMESTAMP
                """,
                (canonical_key, source_url, image_url, source_text, posted_content, payload),
            )
        self.conn.commit()

    def was_source_posted(self, source_url: str, image_url: str, source_text: str = "") -> bool:
        keys = self.posted_source_keys(source_url, image_url, source_text)
        if not keys:
            return False
        placeholders = ",".join("?" for _ in keys)
        cur = self.conn.cursor()
        cur.execute(
            f"""
            SELECT canonical_key
            FROM posted_sources
            WHERE canonical_key IN ({placeholders})
            LIMIT 1
            """,
            keys,
        )
        return cur.fetchone() is not None

    def get_recent_posted_sources(self, limit: int = 20) -> List[Dict]:
        cur = self.conn.cursor()
        cur.execute(
            """
            SELECT canonical_key, source_url, image_url, source_text, posted_content, metadata, posted_at
            FROM posted_sources
            ORDER BY posted_at DESC
            LIMIT ?
            """,
            (limit,),
        )
        return [
            {
                "canonical_key": row[0],
                "source_url": row[1],
                "image_url": row[2],
                "source_text": row[3],
                "posted_content": row[4],
                "metadata": json.loads(row[5]) if row[5] else {},
                "posted_at": row[6],
            }
            for row in cur.fetchall()
        ]

    def record_source_engagement(
        self,
        source_url: str,
        image_url: str,
        source_text: str = "",
        engagement_text: str = "",
        metadata: dict | None = None,
    ) -> None:
        payload = json.dumps(metadata) if metadata else None
        for canonical_key in self.posted_source_keys(source_url, image_url, source_text):
            key = f"engage:{canonical_key}"
            self.conn.execute(
                """
                INSERT INTO engaged_sources (canonical_key, source_url, image_url, source_text, engagement_text, metadata)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(canonical_key) DO UPDATE SET
                    source_url=excluded.source_url,
                    image_url=excluded.image_url,
                    source_text=excluded.source_text,
                    engagement_text=excluded.engagement_text,
                    metadata=excluded.metadata,
                    engaged_at=CURRENT_TIMESTAMP
                """,
                (key, source_url, image_url, source_text, engagement_text, payload),
            )
        self.conn.commit()

    def was_source_engaged(self, source_url: str, image_url: str, source_text: str = "") -> bool:
        base_keys = self.posted_source_keys(source_url, image_url, source_text)
        if not base_keys:
            return False
        keys = [f"engage:{key}" for key in base_keys]
        placeholders = ",".join("?" for _ in keys)
        cur = self.conn.cursor()
        cur.execute(
            f"""
            SELECT canonical_key
            FROM engaged_sources
            WHERE canonical_key IN ({placeholders})
            LIMIT 1
            """,
            keys,
        )
        return cur.fetchone() is not None

    def get_recent_engaged_sources(self, limit: int = 20) -> List[Dict]:
        cur = self.conn.cursor()
        cur.execute(
            """
            SELECT canonical_key, source_url, image_url, source_text, engagement_text, metadata, engaged_at
            FROM engaged_sources
            ORDER BY engaged_at DESC
            LIMIT ?
            """,
            (limit,),
        )
        return [
            {
                "canonical_key": row[0],
                "source_url": row[1],
                "image_url": row[2],
                "source_text": row[3],
                "engagement_text": row[4],
                "metadata": json.loads(row[5]) if row[5] else {},
                "engaged_at": row[6],
            }
            for row in cur.fetchall()
        ]

    def get_stats(self) -> Dict:
        cur = self.conn.cursor()
        stats = {}
        for table in ("beliefs", "memories", "interactions", "lineage", "performance", "reflections", "working_memory", "selector_strategies", "source_assets", "posted_sources", "engaged_sources"):
            cur.execute(f"SELECT COUNT(*) FROM {table}")
            stats[table] = cur.fetchone()[0]
        return stats

    def close(self) -> None:
        if self.conn is not None:
            self.conn.close()
