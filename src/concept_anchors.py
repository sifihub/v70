from __future__ import annotations

from .entropy_source import QuantumEntropy
from .memory_system import MemorySystem


CONCEPT_ANCHORS = [
    "Mycelium Networks",
    "Quantum Entanglement",
    "Fractal Geometry",
    "Black Hole Event Horizons",
    "Magnetic Field Reversal",
    "Obsidian Formation",
    "Orbital Resonance",
    "Radioactive Decay",
    "Core Rope Memory",
    "Antikythera Mechanism",
    "Root Grafting",
    "River Delta Formation",
    "Zero-Knowledge Proofs",
    "Merkle Tree Roots",
    "Hash Avalanche Effects",
    "Byzantine Fault Tolerance",
    "Elliptic Curve Cryptography",
    "Proof of Work Entropy",
    "The Genesis Block",
    "Mempool Congestion",
]


class ConceptSelector:
    def __init__(self, memory: MemorySystem, entropy: QuantumEntropy, max_history: int = 10):
        self.memory = memory
        self.entropy = entropy
        self.max_history = max_history

    def get_concept(self) -> str:
        recent = self.memory.recall_relevant_memories("concept anchor", limit=self.max_history)
        used = {row["content"] for row in recent if row.get("type") == "concept_anchor"}
        available = [anchor for anchor in CONCEPT_ANCHORS if anchor not in used] or CONCEPT_ANCHORS
        index = min(int(self.entropy.get_entropy_float() * len(available)), len(available) - 1)
        chosen = available[index]
        self.memory.add_memory(
            content=chosen,
            memory_type="concept_anchor",
            importance=0.25,
            metadata={"concept": chosen},
        )
        return chosen
