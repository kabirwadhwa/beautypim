from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Optional

from app.config import settings


class LocalRawPageStorage:
    def __init__(self, root: Optional[str] = None):
        self.root = Path(root or settings.CRAWL_RAW_STORAGE_PATH)

    def put(self, content: bytes, suffix: str = ".html") -> tuple[str, str]:
        digest = hashlib.sha256(content).hexdigest()
        target = self.root / digest[:2] / f"{digest}{suffix}"
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists():
            target.write_bytes(content)
        return str(target), digest

    def get(self, reference: str) -> bytes:
        path = Path(reference)
        if self.root.resolve() not in path.resolve().parents:
            raise ValueError("Raw page reference is outside the configured storage root")
        return path.read_bytes()
