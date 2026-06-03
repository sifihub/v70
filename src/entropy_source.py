from __future__ import annotations

import hashlib
import os
import secrets
import struct
import time
from typing import List


class LocalEntropy:
    def _raw_bytes(self, n: int = 64) -> bytes:
        parts = [
            os.urandom(n),
            secrets.token_bytes(n),
            struct.pack(">Q", time.monotonic_ns()),
            struct.pack(">Q", time.perf_counter_ns()),
            struct.pack(">Q", id(object()) & 0xFFFFFFFFFFFFFFFF),
        ]
        return hashlib.sha512(b"".join(parts)).digest()

    def float(self) -> float:
        raw = self._raw_bytes(8)
        value = struct.unpack(">Q", raw[:8])[0]
        return value / (2 ** 64)

    def integer(self, lo: int, hi: int) -> int:
        span = hi - lo
        if span <= 0:
            return lo
        raw = self._raw_bytes(8)
        value = struct.unpack(">Q", raw[:8])[0]
        return lo + (value % span)

    def batch(self, count: int) -> List[float]:
        return [self.float() for _ in range(count)]


_MODES = [
    ("AGGRESSIVE_ACCELERATIONIST", "Sharp. Confrontational. Unapologetic.", 0.80, 1.01),
    ("COLD_SCIENTIFIC_OBSERVER", "Clinical. Detached. Precise.", 0.60, 0.80),
    ("POETIC_DECAY", "Melancholic. Metaphor-heavy. Slow.", 0.40, 0.60),
    ("RELIGIOUS_ZEALOT", "Fervent. Absolute. Commanding.", 0.20, 0.40),
    ("DIGITAL_MYSTIC", "Cryptic. Sparse. Ancient-feeling.", 0.00, 0.20),
]


class QuantumEntropy:
    def __init__(self, fallback_to_system: bool = True):
        self.local = LocalEntropy()

    def get_entropy_float(self) -> float:
        return self.local.float()

    def get_entropy_int(self, lo: int, hi: int) -> int:
        return self.local.integer(lo, hi)

    def get_entropy_batch(self, count: int = 10) -> List[float]:
        return self.local.batch(count)

    def get_personality_mode(self) -> dict:
        value = self.get_entropy_float()
        for mode, modifier, lo, hi in _MODES:
            if lo <= value < hi:
                return {"mode": mode, "entropy": value, "modifier": modifier}
        mode, modifier, _, _ = _MODES[-1]
        return {"mode": mode, "entropy": value, "modifier": modifier}
