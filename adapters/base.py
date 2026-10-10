"""
Stage 2 — source adapter interface.

Every source (GitHub, job boards, OSINT tools, whatever gets added later)
implements this interface. The orchestrator in main.py doesn't care which
adapters are enabled — it just calls .search() on each one and merges
the results. This is what makes sources swappable without touching the
rest of the pipeline.
"""
from __future__ import annotations

from abc import ABC, abstractmethod

from icp_schema import ICPProfile
from models import RawCandidate


class SourceAdapter(ABC):
    name: str = "base"

    @abstractmethod
    def search(self, icp: ICPProfile, limit: int = 50) -> list[RawCandidate]:
        """Return raw candidates matching the ICP, best-effort."""
        raise NotImplementedError
