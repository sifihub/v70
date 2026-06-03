from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from typing import List

from .llm_client import LocalLLM


log = logging.getLogger("zara.brain")


@dataclass
class Step:
    action: str
    target: str = ""
    value: str = ""


class BrainPlanner:
    def __init__(self, model: str = "qwen2.5:1.5b"):
        self.llm = LocalLLM(model=model)

    def ensure_brain(self) -> bool:
        return True

    def _heuristic_step(self, text: str) -> str:
        lowered = (text or "").lower()
        if any(word in lowered for word in ("stuck", "selector", "button", "textbox", "field", "element", "click")):
            return "Capture the page, inspect selectors, and try the most likely actionable element next."
        if any(word in lowered for word in ("login", "sign in", "logged in")):
            return "Check whether the session is already authenticated first, then run the matching login flow only if needed."
        if any(word in lowered for word in ("post", "tweet", "publish", "x.com")):
            return "Find the highest-confidence fresh source post, rephrase it cleanly, attach the source image if available, and publish only once."
        if any(word in lowered for word in ("research", "trend", "analyze")):
            return "Collect fresh trend signals first, score them, and only continue once there is a real source worth using."
        return "Take the next smallest concrete browser or research action that moves the task forward."

    def think(self, situation: str, context: str = "") -> str:
        if not os.environ.get("ZARA_BRAIN_USE_LLM_FOR_TASKS", "").strip():
            return self._heuristic_step(f"{situation}\n{context}")
        prompt = (
            "You are Zara's planner. Break the situation into the next concrete step.\n"
            f"Situation: {situation}\n"
            f"Context: {context}\n"
            "Reply in 1-3 sentences with the exact next action."
        )
        return self.llm.ask(prompt, timeout=30, role="director") or self._heuristic_step(f"{situation}\n{context}")

    def plan(self, goal: str, context: str = "") -> List[Step]:
        if not os.environ.get("ZARA_BRAIN_USE_LLM_FOR_TASKS", "").strip():
            return [Step(action="inspect", target=goal[:120], value=context[:120])]
        prompt = (
            "Plan a short Zara execution sequence.\n"
            f"Goal: {goal}\n"
            f"Context: {context}\n"
            'Return JSON array of objects with keys action, target, value.'
        )
        raw = self.llm.ask(prompt, timeout=35, role="director")
        try:
            parsed = json.loads(raw)
            steps = []
            for item in parsed:
                if not isinstance(item, dict):
                    continue
                steps.append(
                    Step(
                        action=str(item.get("action", "wait")),
                        target=str(item.get("target", "")),
                        value=str(item.get("value", "")),
                    )
                )
            if steps:
                return steps
        except Exception:
            log.info("Falling back to a single wait step")
        return [Step(action="inspect", target=goal[:120], value=context[:120])]
