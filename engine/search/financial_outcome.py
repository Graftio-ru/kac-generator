"""Server-owned deterministic rejection; never an admissible requested quote."""
from dataclasses import dataclass

@dataclass(frozen=True)
class RawFinancialRejection:
    fields: dict
    reason: str
