from __future__ import annotations

import hashlib
from pathlib import Path
import shutil
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

    def put_file(self, source: str, suffix: str = ".json") -> tuple[str, str, int]:
        source_path = Path(source)
        digest = hashlib.sha256()
        size = 0
        with source_path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
                size += len(chunk)
        value = digest.hexdigest()
        target = self.root / value[:2] / f"{value}{suffix}"
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists():
            shutil.copyfile(source_path, target)
        return str(target), value, size
