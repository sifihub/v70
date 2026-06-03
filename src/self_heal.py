from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path


log = logging.getLogger("zara.self_heal")


class SelfHealer:
    def __init__(self, log_path: Path | None = None):
        self.log_path = log_path or Path("data") / "self_heal_log.jsonl"
        self.log_path.parent.mkdir(parents=True, exist_ok=True)

    def record_failure(self, exc: Exception, traceback_text: str) -> None:
        event = {
            "timestamp": datetime.utcnow().isoformat() + "Z",
            "error": str(exc),
            "traceback": traceback_text,
        }
        with open(self.log_path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(event) + "\n")
        log.error("Failure recorded for later analysis")
