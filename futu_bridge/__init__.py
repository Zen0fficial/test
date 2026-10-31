"""Read-only local HTTP bridge for Futu desktop market data."""

__version__ = "0.2.3"

from .client import FutuClient, MarketDataResponse, MarketDataStream
from .deepseek_extractor import DeepSeekExtractionError, DeepSeekExtractor, extract_jsonl
from .option_features import OptionFeatureError, option_snapshot_features
from .provider import MarketDataProvider, ProviderError, ProviderResult
from .research_features import (ResearchFeatureError, aggregate_daily_features,
                                earnings_transcript_events, futu_ai_summary_event,
                                make_event, news_events, pdf_page_events,
                                write_events_jsonl, write_feature_csv,
                                merge_features_into_qlib_source)
from .report_documents import announcement_documents, download_report_pdf, issuer_reports

__all__ = ["FutuClient", "MarketDataResponse", "MarketDataStream", "MarketDataProvider",
           "ProviderError", "ProviderResult", "announcement_documents", "download_report_pdf", "issuer_reports",
           "DeepSeekExtractionError", "DeepSeekExtractor",
           "extract_jsonl",
           "OptionFeatureError", "option_snapshot_features", "ResearchFeatureError", "aggregate_daily_features",
           "earnings_transcript_events", "futu_ai_summary_event", "make_event", "news_events", "pdf_page_events",
           "write_events_jsonl", "write_feature_csv", "merge_features_into_qlib_source", "__version__"]
