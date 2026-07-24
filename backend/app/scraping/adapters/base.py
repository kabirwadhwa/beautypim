from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Optional

from app.scraping.schemas import ScrapedProduct


class ProductAdapter(ABC):
    name = "base"
    version = "1.0.0"

    @abstractmethod
    def parse(self, html: str, url: str, *, country: Optional[str] = None, locale: Optional[str] = None) -> Optional[ScrapedProduct]:
        raise NotImplementedError
