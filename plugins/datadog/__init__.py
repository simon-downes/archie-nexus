"""Exec-only Datadog tools: bounded reads and ownership-guarded monitor writes."""

from .catalog import list_catalog_entities, list_teams
from .dashboards import get_dashboard, list_dashboards
from .discovery import search
from .events import list_events
from .log_config import (
    get_log_pipeline,
    list_log_indexes,
    list_log_metrics,
    list_log_pipelines,
    list_scanning_groups,
)
from .logs import aggregate_logs, search_logs
from .metrics import describe_metric, query_metrics, search_metrics
from .monitors import (
    create_monitor,
    get_monitor,
    search_monitors,
    update_monitor,
    validate_monitor,
)
from .traces import search_spans

__all__ = [
    "aggregate_logs",
    "create_monitor",
    "describe_metric",
    "get_dashboard",
    "get_log_pipeline",
    "get_monitor",
    "list_catalog_entities",
    "list_dashboards",
    "list_events",
    "list_log_indexes",
    "list_log_metrics",
    "list_log_pipelines",
    "list_scanning_groups",
    "list_teams",
    "query_metrics",
    "search",
    "search_logs",
    "search_metrics",
    "search_monitors",
    "search_spans",
    "update_monitor",
    "validate_monitor",
]
