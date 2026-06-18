"""حزمة core — نماذج النطاق وتعداداته واستثناءاته (لا تبعية على Streamlit أو DB)."""
from core.enums import (
    ActionType,
    ConfidenceLevel,
    Gender,
    ItemType,
    SectionType,
)
from core.exceptions import (
    AIServiceError,
    ClassificationError,
    ConfigError,
    DataLossError,
    ExportError,
    MatchingError,
    MissingDetectionError,
    PricingError,
    RepositoryError,
)
from core.models import (
    AnalysisResult,
    CompetitorDetail,
    CompetitorProduct,
    MatchResult,
    MissingProduct,
    PriceDiff,
    Product,
    ReconciliationReport,
)

__all__ = [
    "ActionType", "ConfidenceLevel", "Gender", "ItemType", "SectionType",
    "PricingError", "ConfigError", "RepositoryError", "MatchingError",
    "ClassificationError", "MissingDetectionError", "AIServiceError",
    "ExportError", "DataLossError",
    "Product", "CompetitorProduct", "CompetitorDetail", "MatchResult",
    "PriceDiff", "MissingProduct", "ReconciliationReport", "AnalysisResult",
]
