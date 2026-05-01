# Sentinel test fixture — clean baseline.
# Must produce zero findings on a Quick / Standard scan.  If a
# regression introduces FPs, this file lights the test up.

from __future__ import annotations

import hashlib
from typing import Iterable


def safe_hash(password: str, salt: bytes) -> str:
    """SHA-256 hashing with a separate salt is fine; argon2 / bcrypt
    are stronger but using SHA-256 with a salt is not a CWE-327."""
    digest = hashlib.sha256(salt + password.encode("utf-8")).hexdigest()
    return digest


def filter_positive(values: Iterable[float]) -> list[float]:
    return [v for v in values if v > 0]


def add_one(items: list[int]) -> list[int]:
    return [i + 1 for i in items]
