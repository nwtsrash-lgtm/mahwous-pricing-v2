"""services/scraper_service.py — إدارة روابط المنافسين + الكشط.

يدير ملف `competitors_list_v30.json` (سرد/إضافة عبر رابط محلي/حذف) ويلتفّ على
`engines.mahally_scraper.MahallyScraper` للكشط والتحقّق (لا يُعاد كتابة المحرّك).

الجزء النقي (استخراج المعرّف + إدارة JSON) قابل للاختبار دون شبكة؛ النداءات
الشبكية (تحقّق/كشط) مفصولة في دوال تستورد المحرّك كسولاً.
"""
from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from conf.constants import COMPETITORS_FILE, COMPETITOR_DB_PATH, PROJECT_ROOT
from core.exceptions import PricingError, RepositoryError

_STORE_ID_RE = re.compile(r"/stores/(\d+)")


@dataclass(frozen=True)
class Competitor:
    """متجر منافس مُعرَّف محلياً."""

    name: str
    store_url: str
    mahally_store_id: int
    sitemap_url: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name, "store_url": self.store_url,
            "sitemap_url": self.sitemap_url,
            "mahally_store_id": self.mahally_store_id,
        }


def extract_store_id(url: str) -> Optional[int]:
    """يستخرج معرّف متجر محلي من رابط `mahally.com/stores/ID`. خالص."""
    match = _STORE_ID_RE.search(url or "")
    return int(match.group(1)) if match else None


class ScraperService:
    """خدمة كشط المنافسين وإدارة روابطهم."""

    def __init__(
        self,
        links_file: Path = COMPETITORS_FILE,
        competitor_db: Path = COMPETITOR_DB_PATH,
    ) -> None:
        self._file = Path(links_file)
        self._db = str(competitor_db)

    def _load_raw(self) -> list[dict[str, Any]]:
        if not self._file.exists():
            return []
        try:
            with open(self._file, "r", encoding="utf-8") as handle:
                data = json.load(handle)
            return data if isinstance(data, list) else []
        except Exception as exc:
            raise RepositoryError("تعذّرت قراءة ملف المنافسين", error=str(exc)) from exc

    def _save_raw(self, data: list[dict[str, Any]]) -> None:
        self._file.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._file.with_suffix(".json.tmp")
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
        tmp.replace(self._file)

    def list_competitors(self) -> list[Competitor]:
        """يسرد المتاجر المُعرَّفة."""
        out: list[Competitor] = []
        for entry in self._load_raw():
            sid = entry.get("mahally_store_id")
            if sid:
                out.append(Competitor(
                    name=entry.get("name", f"store_{sid}"),
                    store_url=entry.get("store_url", ""),
                    mahally_store_id=int(sid),
                    sitemap_url=entry.get("sitemap_url", ""),
                ))
        return out

    def add_competitor(self, name: str, url: str) -> Competitor:
        """يضيف متجراً (يستخرج المعرّف من الرابط، يمنع التكرار، يحفظ)."""
        store_id = extract_store_id(url)
        if not store_id:
            raise PricingError("رابط غير صالح — استخدم mahally.com/stores/ID")
        if not (name or "").strip():
            raise PricingError("اسم المتجر مطلوب")
        data = self._load_raw()
        if any(e.get("mahally_store_id") == store_id for e in data):
            raise PricingError(f"المتجر موجود مسبقاً (#{store_id})")
        competitor = Competitor(name.strip(), url.strip(), store_id)
        data.append(competitor.to_dict())
        self._save_raw(data)
        return competitor

    def remove_competitor(self, store_id: int) -> bool:
        """يحذف متجراً بمعرّفه. يعيد True إن حُذف."""
        data = self._load_raw()
        kept = [e for e in data if e.get("mahally_store_id") != store_id]
        if len(kept) == len(data):
            return False
        self._save_raw(kept)
        return True

    def _scraper(self) -> Any:
        """يحمّل MahallyScraper كسولاً (استيراد من جذر المشروع)."""
        if str(PROJECT_ROOT) not in sys.path:
            sys.path.insert(0, str(PROJECT_ROOT))
        try:
            from engines.mahally_scraper import MahallyScraper  # type: ignore

            return MahallyScraper(db_path=self._db)
        except Exception as exc:  # pragma: no cover - يحتاج المحرّك
            raise RepositoryError("تعذّر تحميل محرّك محلي", error=str(exc)) from exc

    def validate_store(self, store_id: int) -> dict[str, Any]:
        """يتحقّق من متجر عبر MahallyScraper.get_store_info (شبكي)."""
        return self._scraper().get_store_info(store_id)

    def scrape_and_save(self, store_id: int, name: str) -> int:
        """يكشط متجراً ويحفظه في قاعدة المنافسين (شبكي). يعيد عدد المنتجات."""
        scraper = self._scraper()
        products = scraper.scrape_store(store_id, name)
        if not products:
            return 0
        return scraper.save_to_db(products, name)
