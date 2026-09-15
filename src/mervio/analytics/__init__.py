"""Analytics Engine de Mervio (deterministe, sans LLM)."""
from .anomaly import Anomaly, detect_anomalies
from .health import BusinessHealth, compute_business_health
from .insights import Insight, build_insights
from .kpi import Metric, campaign_performance, compute_kpis, product_performance
from .periods import Period, last_complete_period, make_period, previous_period
from .pipeline import SourcePaths, analyze_loaded_dataset, load_dataset, run_analysis
from .profitability import ProfitabilityResult, compute_profitability
from .root_cause import RootCauseAnalysis, analyse_revenue_change

__all__ = [
    "Anomaly", "BusinessHealth", "Insight", "Metric", "Period", "ProfitabilityResult",
    "RootCauseAnalysis", "SourcePaths", "analyse_revenue_change", "analyze_loaded_dataset", "build_insights",
    "campaign_performance", "compute_business_health", "compute_kpis",
    "compute_profitability", "detect_anomalies", "last_complete_period", "load_dataset",
    "make_period", "previous_period", "product_performance", "run_analysis",
]
