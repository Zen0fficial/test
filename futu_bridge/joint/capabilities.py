"""Explicit feature-family coverage; installed packages never imply integration.

This is a versioned implementation backlog, not a count of every upstream API.
Verification is reported separately and requires local evidence artifacts.
"""

from __future__ import annotations

import json
from pathlib import Path

from .evidence import lookup

# framework, family, state, public entry point, local evidence path
CATALOGUE = (
    ("qlib", "converted_order_lifecycle", "scoped", "simulate-lifecycle",
     "lifecycle_extended_final_20261109/report.json"),
    ("nautilus", "native_order_lifecycle", "scoped", "simulate-lifecycle",
     "lifecycle_extended_final_20261109/report.json"),
    ("lean", "native_order_lifecycle", "scoped", "simulate-lifecycle",
     "lifecycle_extended_final_20261109/report.json"),
    ("qlib", "storage_expressions", "scoped", "history", "shared_history_final_20261109/report.json"),
    ("qlib", "dataset_lightgbm", "scoped", "compare-models", "rl_supervised_compat_20261109/comparison/report.json"),
    ("qlib", "native_rl_ppo_dqn", "scoped", "train-qlib-rl", "qlib_native_final_20261109/report.json"),
    ("qlib", "native_rl_simple_execution", "scoped", "train-qlib-rl", "qlib_native_final_20261109/report.json"),
    ("qlib", "native_rl_nested_execution_hk", "scoped", "train-qlib-nested",
     "qlib_nested_final_20261109/report.json"),
    ("qlib", "alpha158_alpha360_processors", "pending", None, None),
    ("qlib", "full_model_catalogue", "pending", None, None),
    ("qlib", "portfolio_strategies", "pending", None, None),
    ("qlib", "nested_multi_frequency_backtests", "scoped", "simulate-native",
     "native_execution_final_20261109/report.json"),
    ("qlib", "online_rolling_workflows", "pending", None, None),
    ("qlib", "meta_learning_tuning", "partial", "qlib_meta_parquet", None),
    ("qlib", "task_distributed_workflows", "pending", None, None),
    ("qlib", "cache_data_services", "pending", None, None),
    ("qlib", "full_native_analysis", "pending", None, None),
    ("nautilus", "native_replay", "scoped", "history", "shared_history_final_20261109/report.json"),
    ("nautilus", "live_trade_bbo", "scoped", "live", "shared_live_20261109/nautilus/verification.json"),
    ("nautilus", "native_paper_execution", "scoped", "simulate-native",
     "native_execution_final_20261109/report.json"),
    ("nautilus", "portfolio_risk_accounting", "scoped", "simulate-native",
     "native_execution_final_20261109/report.json"),
    ("nautilus", "fill_latency_fee_models", "scoped", "simulate-native",
     "native_execution_final_20261109/report.json"),
    ("nautilus", "depth_order_books", "pending", None, None),
    ("nautilus", "parquet_catalogue", "pending", None, None),
    ("nautilus", "indicator_catalogue", "pending", None, None),
    ("nautilus", "strategy_actor_batch_composition", "pending", None, None),
    ("nautilus", "native_performance_reports", "pending", None, None),
    ("lean", "native_replay", "scoped", "history", "shared_history_final_20261109/report.json"),
    ("lean", "live_trade_bbo", "scoped", "live", "shared_live_20261109/lean/verification.json"),
    ("lean", "universe_selection", "pending", None, None),
    ("lean", "alpha_insight_lifecycle", "pending", None, None),
    ("lean", "portfolio_construction", "pending", None, None),
    ("lean", "risk_management", "pending", None, None),
    ("lean", "native_paper_execution", "scoped", "simulate-native",
     "native_execution_final_20261109/report.json"),
    ("lean", "security_fee_fill_models", "scoped", "simulate-native",
     "native_execution_final_20261109/report.json"),
    ("lean", "history_indicators_consolidators", "pending", None, None),
    ("lean", "research_python_algorithms", "pending", None, None),
    ("lean", "optimization_cli", "pending", None, None),
    ("mlflow", "tracking_artifact_reload", "scoped", "train-qlib-rl", "qlib_native_final_20261109/report.json"),
    ("mlflow", "model_registry_aliases", "pending", None, None),
    ("mlflow", "central_tracking_service", "pending", None, None),
    ("mlflow", "serving_promotion", "pending", None, None),
    ("joint", "sb3_allocation_rl", "scoped", "train-rl", "rl_final_20261109/report.json"),
    ("joint", "capability_inventory", "implemented", "capabilities", None),
    ("joint", "parquet_snapshot_storage", "partial", "storage.captures",
     "parquet_store_20261110/verification.json"),
    ("qlib", "parquet_native_data_rl", "scoped", "qlib_parquet_verify --train-rl", None),
    ("qlib", "mongo_free_local_tasks", "scoped", "workflows.acceptance", None),
    ("nautilus", "deterministic_simulation_recovery", "scoped", "nautilus_recovery", None),
    ("nautilus", "partial_brackets_extended_orders", "scoped", "nautilus_order_extensions", None),
    ("joint", "canonical_research_workflow", "scoped", "research-parquet", None),
    ("qlib", "durable_online_schedule", "scoped", "online_parquet", None),
    ("qlib", "additional_native_learners", "partial", "qlib_model_extensions_verify", None),
    ("lean", "deterministic_simulation_recovery", "scoped", "lean_recovery", None),
    ("nautilus", "activation_price_orders", "scoped", "nautilus_activation", None),
    ("lean", "native_contingent_groups", "scoped", "lean_contingent", None),
    ("lean", "native_combo_orders", "scoped", "lean_combos", None),
    ("lean", "adapted_immediate_intents", "scoped", "lean_immediate", None),
    ("qlib", "cross_sectional_signal_analysis", "scoped", "panel_analysis", None),
    ("joint", "general_native_api_configuration", "pending", None, None),
    ("joint", "multi_asset_corporate_actions_settlement", "pending", None, None),
    ("joint", "point_in_time_universes_fundamentals", "pending", None, None),
    ("joint", "full_provider_field_conversion", "partial", "storage provider-ingest", None),
    ("joint", "cross_framework_strategy_contracts", "scoped", "simulate-native",
     "native_execution_final_20261109/report.json"),
    ("joint", "implementation_benchmarks", "pending", None, None),
    ("joint", "real_brokerage_routing", "disabled_by_requirement", None, None),
)

# Scoped additions are separate from the historical catalogue to preserve its
# family identifiers. Evidence is per platform, never inferred from importability.
FEATURE_ADDITIONS = {
    "alpha158_alpha360_processors": "scoped", "full_model_catalogue": "partial",
    "portfolio_strategies": "scoped", "online_rolling_workflows": "scoped",
    "task_distributed_workflows": "partial", "cache_data_services": "partial", "full_native_analysis": "partial",
    "parquet_catalogue": "scoped", "indicator_catalogue": "scoped", "depth_order_books": "scoped",
    "strategy_actor_batch_composition": "scoped", "native_performance_reports": "scoped",
    "universe_selection": "scoped", "alpha_insight_lifecycle": "scoped", "portfolio_construction": "scoped",
    "risk_management": "scoped", "history_indicators_consolidators": "scoped", "optimization_cli": "partial",
    "research_python_algorithms": "partial",
    "model_registry_aliases": "scoped", "central_tracking_service": "scoped", "serving_promotion": "scoped",
}

HISTORICAL_FEATURES = {
    "qlib": "qlib_features_final_20261109/report.json",
    "nautilus": "nautilus_features_verified_20261109/report.json",
    "lean": "lean_features_final2_20261109/report.json",
    "mlflow": "mlflow_features_final_20261109/report.json",
}

REMAINING = {
    "full_model_catalogue": (
        "Original feature suite plus separate additional-learner evidence; optional/concept-model gaps remain"),
    "meta_learning_tuning": (
        "Native MetaModelDS verified on retrospective 20-stock panel; broad tuning and PIT universes remain"),
    "task_distributed_workflows": "Mongo-free local coordinator available; separate-host operation unverified",
    "cache_data_services": "Native local dataset cache covered; distributed/Redis expression services absent",
    "full_native_analysis": "Numerical native analysis covered; native interactive graph catalogue needs Plotly",
    "portfolio_strategies": (
        "Single-stock native strategies; broad risk-factor and enhanced-index universes unverified"),
    "alpha158_alpha360_processors": (
        "Raw VWAP missing because provider turnover scale is unverified; explicitly imputed"),
    "online_rolling_workflows": (
        "Historical rolling updates and model promotion; no continuously operated online service"),
    "depth_order_books": "Observed snapshots verified; lossless exchange deltas and queue reconstruction unavailable",
    "indicator_catalogue": (
        "37 pinned native classes across 48 routes; selected parameters and constant actual BBO spread only"),
    "strategy_actor_batch_composition": "Tested configured Actor/Strategy batches; not arbitrary execution algorithms",
    "native_order_lifecycle": "Backend-specific order support; restart persistence and network races remain scoped",
    "universe_selection": "Scheduled captured equity universe; no point-in-time fundamental universe dataset",
    "portfolio_construction": "Native single-stock target construction; broad multi-asset optimization unverified",
    "risk_management": "Native drawdown model tested; other risk-model families and margin scenarios unverified",
    "optimization_cli": "Local native grid only; no distributed or QuantConnect cloud/CLI deployment claim",
    "research_python_algorithms": "Shared Python and QuantBook History adapters; other research APIs unverified",
    "central_tracking_service": "Local loopback HTTP tested; no authenticated shared production deployment",
    "serving_promotion": "Two research-model versions and local native inference; no remote deployment/autotrading",
    "multi_asset_corporate_actions_settlement": (
        "No verified corporate-action, FX or general settlement input contract"),
    "point_in_time_universes_fundamentals": "No historical publication-time fundamental universe evidence",
    "full_provider_field_conversion": (
        "Ten additional typed families; all14 methods/98 routes inventoried; explicit raw-only fields remain"),
    "general_native_api_configuration": "Explicit native configurations exposed; no unrestricted all-API passthrough",
    "parquet_snapshot_storage": (
        "Small captures use full immutable tables; automatic deletion disabled; "
        "full-session/lunch capture unverified"),
    "parquet_native_data_rl": "Observed calendars only; historical PIT fundamentals and genuine VWAP unavailable",
    "mongo_free_local_tasks": "One-host SQLite workers; arbitrary PyMongo collection protocol unsupported",
    "deterministic_simulation_recovery": "Replay from immutable inputs; not a full native memory checkpoint",
    "partial_brackets_extended_orders": "Integral 100-share fill model; native auction TIF remains unsupported",
    "canonical_research_workflow": "Reused holdout; daily signals and minute conformance are separate simulations",
    "durable_online_schedule": "Local snapshot polling/restart; not a continuously operated multi-host service",
    "additional_native_learners": (
        "Explicit conformance configurations; optional dependencies and concepts remain scoped"),
    "activation_price_orders": "BID_ASK price-offset gates and funded sell market-to-limit; derived quote assumptions",
    "native_contingent_groups": (
        "Native complete-parent OTO, OCO and OUO; OCO partial fill cancels its winner remainder"),
    "native_combo_orders": (
        "Two real equities, complete native groups and date-end expiry; "
        "partial ratios and actual lot history unverified"),
    "adapted_immediate_intents": (
        "Joint IOC/FOK semantics over native child tickets; declared immediate100-share capacity only"),
    "cross_sectional_signal_analysis": (
        "Six saved learners on retrospective20-stock basket; descriptive diagnostics, not portfolio execution"),
    "cross_framework_strategy_contracts": (
        "Shared HK single-instrument simulation commands; general multi-asset strategy event protocol remains"),
}

PARQUET_EVIDENCE = {
    "parquet_snapshot_storage": "parquet_storage", "parquet_native_data_rl": "qlib_parquet",
    "mongo_free_local_tasks": "parquet_tasks", "deterministic_simulation_recovery": "nautilus_recovery",
    "partial_brackets_extended_orders": "nautilus_extensions", "canonical_research_workflow": "parquet_workflow",
    "implementation_benchmarks": "performance",
    "durable_online_schedule": "online_parquet", "additional_native_learners": "qlib_models_extended",
    "meta_learning_tuning": "qlib_meta", "indicator_catalogue": "nautilus_indicators",
    "full_provider_field_conversion": "provider_fields",
    "activation_price_orders": "nautilus_activation", "native_contingent_groups": "lean_contingents",
    "native_rl_ppo_dqn": "qlib_rl", "native_rl_simple_execution": "qlib_rl",
    "native_rl_nested_execution_hk": "qlib_nested",
    "converted_order_lifecycle": "lifecycle", "native_order_lifecycle": "lifecycle",
    "storage_expressions": "qlib_parquet", "dataset_lightgbm": "parquet_workflow",
    "task_distributed_workflows": "parquet_tasks", "online_rolling_workflows": "online_parquet",
    "full_model_catalogue": "qlib_model_catalogue",
    "native_combo_orders": "lean_combos", "adapted_immediate_intents": "lean_immediate",
    "cross_sectional_signal_analysis": "panel_analysis",
}


def report(repo: Path) -> dict[str, object]:
    """Show implementation and evidence separately; never mark absent artifacts verified."""
    entries = []
    for framework, family, state, command, evidence in CATALOGUE:
        binding = "historical_unbound"
        published = None
        if family in FEATURE_ADDITIONS:
            state, command = FEATURE_ADDITIONS[family], "verify-features"
            evidence = HISTORICAL_FEATURES[framework]
            published, binding = lookup(repo, framework)
        if family == "implementation_benchmarks":
            state, command, evidence = "partial", "benchmark-features", None
        if family in PARQUET_EVIDENCE:
            published, binding = lookup(repo, PARQUET_EVIDENCE[family])
        if family == "deterministic_simulation_recovery" and framework == "lean":
            published, binding = lookup(repo, "lean_recovery")
        path = published or (repo / "output/joint" / evidence if evidence else None)
        verification = "not_recorded"
        if path is not None and path.is_file():
            try:
                value = json.loads(path.read_text(encoding="utf-8"))
                passed = value.get("passed") is True or value.get("completed") is True
                if family.startswith("native_rl_"):
                    passed = passed and value.get("independent_verification", {}).get("passed") is True
                if family == "task_distributed_workflows" and published is None:
                    passed = passed and value.get("distributed_tasks", {}).get("passed") is True
                if family == "research_python_algorithms":
                    passed = passed and value.get("native_research", {}).get("passed") is True
                if family == "optimization_cli":
                    passed = passed and value.get("native_optimizer", {}).get("passed") is True
                verification = ("passed_current_source" if binding == "current_source_verified"
                                else "passed_historical_scope") if passed else "failed_or_incomplete"
                if binding not in ("current_source_verified", "historical_unbound", "not_recorded"):
                    verification = binding
            except (ValueError, OSError):
                verification = "unreadable_evidence"
        entries.append({"framework": framework, "family": family, "implementation": state,
                        "command": command, "verification": verification,
                        "source_binding": binding,
                        "evidence": str(path) if path else None,
                        "remaining_scope": REMAINING.get(family)})
    return {"schema_version": 1, "scope": "Pinned framework feature families; not every upstream API",
            "full_feature_coverage": False, "real_orders_enabled": False,
            "versions": {"qlib": "0.9.7", "nautilus": "1.231.0", "mlflow": "3.18.0",
                         "lean_commit": "49b4aa7022abd38e99e9c7d3ec67929fa7f6d7b8"},
            "capabilities": entries}
