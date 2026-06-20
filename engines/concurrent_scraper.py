"""
engines/concurrent_scraper.py — Concurrent Scraping Orchestrator v2.0
=====================================================================
Manages parallel scraping across multiple competitor stores.

v2.0 changes:
  - asyncio.gather with return_exceptions=True for fault isolation
  - Staggered start to avoid burst detection by WAFs
  - Fixed sync runner to use asyncio.run() (no deprecated get_event_loop)
  - Raised default concurrency to 5 stores in parallel
"""

import asyncio
import logging
import json
import time
from datetime import datetime
from typing import Dict, List, Optional, Tuple, Any
from dataclasses import dataclass, asdict
from enum import Enum
import pandas as pd
import traceback

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger("ConcurrentScraper")


class CompetitorStatus(Enum):
    """Competitor scraping state machine"""
    ACTIVE = "🟢 نشط"
    DISABLED = "⚫ معطل"
    RUNNING = "⏳ جاري الكشط"
    SUCCESS = "✅ نجح"
    ERROR = "❌ خطأ"
    TIMEOUT = "⏱️ انتهت المهلة الزمنية"
    SKIPPED = "⏭️ تم التخطي"


@dataclass
class CompetitorConfig:
    """Per-competitor configuration"""
    id: str
    name: str
    url: str
    enabled: bool = True
    timeout: int = 30
    retries: int = 2
    priority: int = 0
    custom_headers: Dict = None

    def __post_init__(self):
        if self.custom_headers is None:
            self.custom_headers = {}


@dataclass
class ScrapingResult:
    """Result container for a single competitor scrape"""
    competitor_id: str
    competitor_name: str
    status: CompetitorStatus
    data: Optional[pd.DataFrame] = None
    error_message: Optional[str] = None
    timestamp: str = None
    duration_seconds: float = 0.0
    items_count: int = 0
    retry_count: int = 0

    def __post_init__(self):
        if self.timestamp is None:
            self.timestamp = datetime.now().isoformat()

    def to_dict(self):
        return {
            "competitor_id": self.competitor_id,
            "competitor_name": self.competitor_name,
            "status": self.status.value,
            "items_count": self.items_count,
            "duration_seconds": self.duration_seconds,
            "timestamp": self.timestamp,
            "error_message": self.error_message,
            "retry_count": self.retry_count,
        }


class ConcurrentScraperEngine:
    """
    Parallel competitor scraping orchestrator.

    Uses asyncio.Semaphore to cap the number of stores scraped simultaneously,
    asyncio.gather(return_exceptions=True) so one store failure never kills
    the rest, and a staggered start delay to avoid burst-triggering WAFs.
    """

    # Default: up to 5 stores scraped in parallel.
    # Each store itself may open many connections (controlled by its own semaphore),
    # so keeping this moderate prevents overwhelming the network / memory.
    DEFAULT_MAX_CONCURRENT = 5

    # Seconds between launching each store task to avoid request bursts.
    STAGGER_DELAY = 1.5

    def __init__(self, max_concurrent_tasks: int = DEFAULT_MAX_CONCURRENT, log_file: str = None):
        """
        Args:
            max_concurrent_tasks: max stores scraped in parallel (Semaphore limit)
            log_file: optional path for JSON scrape log
        """
        self.max_concurrent_tasks = max_concurrent_tasks
        self.competitors: Dict[str, CompetitorConfig] = {}
        self.results: Dict[str, ScrapingResult] = {}
        self.log_file = log_file or "scraping_log.json"
        self.is_running = False

    def register_competitor(self, config: CompetitorConfig) -> None:
        self.competitors[config.id] = config
        logger.info(f"Registered competitor: {config.name} (ID: {config.id})")

    def register_competitors(self, configs: List[CompetitorConfig]) -> None:
        for config in configs:
            self.register_competitor(config)

    def toggle_competitor(self, competitor_id: str, enabled: bool) -> bool:
        if competitor_id not in self.competitors:
            logger.warning(f"Competitor not found: {competitor_id}")
            return False
        self.competitors[competitor_id].enabled = enabled
        status = "enabled" if enabled else "disabled"
        logger.info(f"{self.competitors[competitor_id].name} → {status}")
        return True

    def get_competitor_status(self, competitor_id: str) -> Optional[Dict]:
        if competitor_id not in self.results:
            return None
        return self.results[competitor_id].to_dict()

    def get_all_statuses(self) -> List[Dict]:
        return [result.to_dict() for result in self.results.values()]

    async def _scrape_single_competitor(
        self,
        config: CompetitorConfig,
        scraper_func: callable,
        semaphore: asyncio.Semaphore,
    ) -> ScrapingResult:
        """
        Scrape one competitor behind a shared Semaphore.

        The semaphore is created inside run_all_scrapers() so it belongs
        to the current event loop — avoids cross-loop issues.
        """
        # Gate: wait for a free slot
        async with semaphore:
            start_time = time.time()
            retry_count = 0

            if not config.enabled:
                logger.info(f"Skipping disabled competitor: {config.name}")
                return ScrapingResult(
                    competitor_id=config.id,
                    competitor_name=config.name,
                    status=CompetitorStatus.DISABLED,
                    duration_seconds=0.0,
                )

            # Retry loop with exponential backoff
            while retry_count <= config.retries:
                try:
                    logger.info(
                        f"🔄 Scraping {config.name} "
                        f"(attempt {retry_count + 1}/{config.retries + 1})"
                    )

                    try:
                        data = await asyncio.wait_for(
                            scraper_func(config),
                            timeout=config.timeout,
                        )
                    except asyncio.TimeoutError:
                        logger.warning(f"⏱️ Timeout for {config.name}")
                        if retry_count < config.retries:
                            retry_count += 1
                            await asyncio.sleep(2 ** retry_count)
                            continue
                        duration = time.time() - start_time
                        return ScrapingResult(
                            competitor_id=config.id,
                            competitor_name=config.name,
                            status=CompetitorStatus.TIMEOUT,
                            error_message=f"Timeout after {config.timeout}s",
                            duration_seconds=duration,
                            retry_count=retry_count,
                        )

                    if data is None or (isinstance(data, pd.DataFrame) and data.empty):
                        logger.warning(f"⚠️ No data from {config.name}")
                        retry_count += 1
                        if retry_count <= config.retries:
                            await asyncio.sleep(2 ** retry_count)
                            continue
                        duration = time.time() - start_time
                        return ScrapingResult(
                            competitor_id=config.id,
                            competitor_name=config.name,
                            status=CompetitorStatus.ERROR,
                            error_message="No valid data returned",
                            duration_seconds=duration,
                            retry_count=retry_count,
                        )

                    # Success
                    duration = time.time() - start_time
                    items_count = len(data) if isinstance(data, pd.DataFrame) else 0
                    logger.info(
                        f"✅ {config.name} done — "
                        f"{items_count} products in {duration:.1f}s"
                    )
                    return ScrapingResult(
                        competitor_id=config.id,
                        competitor_name=config.name,
                        status=CompetitorStatus.SUCCESS,
                        data=data,
                        duration_seconds=duration,
                        items_count=items_count,
                        retry_count=retry_count,
                    )

                except Exception as e:
                    error_msg = str(e)
                    logger.error(f"❌ Error scraping {config.name}: {error_msg}")
                    logger.debug(traceback.format_exc())
                    retry_count += 1
                    if retry_count <= config.retries:
                        await asyncio.sleep(2 ** retry_count)
                    else:
                        duration = time.time() - start_time
                        return ScrapingResult(
                            competitor_id=config.id,
                            competitor_name=config.name,
                            status=CompetitorStatus.ERROR,
                            error_message=error_msg,
                            duration_seconds=duration,
                            retry_count=retry_count,
                        )

            # All retries exhausted
            duration = time.time() - start_time
            return ScrapingResult(
                competitor_id=config.id,
                competitor_name=config.name,
                status=CompetitorStatus.ERROR,
                error_message="All retry attempts failed",
                duration_seconds=duration,
                retry_count=retry_count,
            )

    async def run_all_scrapers(
        self,
        scraper_func: callable,
        sort_by_priority: bool = True,
    ) -> Dict[str, ScrapingResult]:
        """
        Launch all registered competitors concurrently behind a Semaphore.

        - Semaphore(max_concurrent_tasks) caps parallel store count.
        - return_exceptions=True ensures one store crash never kills others.
        - Staggered start prevents burst-triggering WAFs.
        """
        self.is_running = True
        logger.info(
            f"🚀 Launching {len(self.competitors)} competitors "
            f"(max {self.max_concurrent_tasks} parallel)…"
        )

        competitors_list = list(self.competitors.values())
        if sort_by_priority:
            competitors_list.sort(key=lambda c: c.priority, reverse=True)

        # Create semaphore inside the running event loop
        semaphore = asyncio.Semaphore(self.max_concurrent_tasks)

        # Staggered launch: wrap each task with an incremental delay
        # so stores don't all hit the network at the exact same instant.
        async def _staggered_task(idx: int, config: CompetitorConfig) -> ScrapingResult:
            if idx > 0:
                await asyncio.sleep(idx * self.STAGGER_DELAY)
            return await self._scrape_single_competitor(config, scraper_func, semaphore)

        tasks = [
            _staggered_task(i, config)
            for i, config in enumerate(competitors_list)
        ]

        # return_exceptions=True: failed tasks return the Exception object
        # instead of propagating it and cancelling siblings.
        raw_results = await asyncio.gather(*tasks, return_exceptions=True)

        # Separate real results from exceptions
        for config, result in zip(competitors_list, raw_results):
            if isinstance(result, BaseException):
                logger.error(
                    f"💥 Unhandled exception for {config.name}: {result}"
                )
                self.results[config.id] = ScrapingResult(
                    competitor_id=config.id,
                    competitor_name=config.name,
                    status=CompetitorStatus.ERROR,
                    error_message=str(result)[:300],
                )
            else:
                self.results[config.id] = result

        self.is_running = False
        logger.info("✅ All competitors finished")
        self._save_log()
        return self.results

    def run_scrapers_sync(
        self,
        scraper_func: callable,
        sort_by_priority: bool = True,
    ) -> Dict[str, ScrapingResult]:
        """
        Sync wrapper — safe to call from Streamlit callbacks or plain scripts.
        Uses asyncio.run() which creates a fresh event loop.
        """
        return asyncio.run(
            self.run_all_scrapers(scraper_func, sort_by_priority)
        )

    def get_successful_data(self) -> pd.DataFrame:
        """Merge all successful DataFrames with source tracking columns."""
        all_data = []
        for result in self.results.values():
            if result.status == CompetitorStatus.SUCCESS and result.data is not None:
                df = result.data.copy()
                df['_competitor_source'] = result.competitor_id
                df['_competitor_name'] = result.competitor_name
                df['_scrape_timestamp'] = result.timestamp
                all_data.append(df)

        if all_data:
            combined = pd.concat(all_data, ignore_index=True)
            logger.info(f"✅ Merged {len(all_data)} sources → {len(combined)} products")
            return combined
        logger.warning("⚠️ No successful data to merge")
        return pd.DataFrame()

    def get_error_summary(self) -> Dict[str, Any]:
        summary = {
            "total_competitors": len(self.competitors),
            "successful": 0,
            "failed": 0,
            "disabled": 0,
            "timeout": 0,
            "details": [],
        }
        for result in self.results.values():
            if result.status == CompetitorStatus.SUCCESS:
                summary["successful"] += 1
            elif result.status == CompetitorStatus.DISABLED:
                summary["disabled"] += 1
            elif result.status == CompetitorStatus.TIMEOUT:
                summary["timeout"] += 1
            else:
                summary["failed"] += 1
            summary["details"].append(result.to_dict())
        return summary

    def _save_log(self) -> None:
        try:
            log_data = {
                "timestamp": datetime.now().isoformat(),
                "summary": self.get_error_summary(),
                "results": [r.to_dict() for r in self.results.values()],
            }
            with open(self.log_file, 'w', encoding='utf-8') as f:
                json.dump(log_data, f, ensure_ascii=False, indent=2)
            logger.info(f"📝 Log saved to {self.log_file}")
        except Exception as e:
            logger.error(f"❌ Failed to save log: {e}")

    def export_results_to_excel(self, output_path: str) -> bool:
        try:
            with pd.ExcelWriter(output_path, engine='openpyxl') as writer:
                combined_data = self.get_successful_data()
                if not combined_data.empty:
                    combined_data.to_excel(writer, sheet_name='البيانات المدمجة', index=False)
                summary_df = pd.DataFrame([r.to_dict() for r in self.results.values()])
                summary_df.to_excel(writer, sheet_name='ملخص الحالات', index=False)
            logger.info(f"✅ Exported results to {output_path}")
            return True
        except Exception as e:
            logger.error(f"❌ Export failed: {e}")
            return False


def create_default_competitors() -> List[CompetitorConfig]:
    """Create a default competitor list (example / testing)."""
    return [
        CompetitorConfig(
            id="competitor_1",
            name="المنافس الأول",
            url="https://example1.com",
            enabled=True,
            priority=1,
        ),
        CompetitorConfig(
            id="competitor_2",
            name="المنافس الثاني",
            url="https://example2.com",
            enabled=True,
            priority=2,
        ),
    ]
