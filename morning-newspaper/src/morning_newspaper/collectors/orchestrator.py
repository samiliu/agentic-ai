from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List

from morning_newspaper.common import compact_text
from morning_newspaper.models import RawItem

from .github import fetch_github_high_stars
from .hackernews import fetch_hackernews_top
from .items import dedup_exact
from .rss import fetch_rss
from .tavily import execute_tavily_plan, read_tavily_results, write_tavily_plan


def collect_all(config: Dict[str, Any], *, root: Path) -> tuple[List[RawItem], List[Dict[str, Any]]]:
    reports: List[Dict[str, Any]] = []
    items: List[RawItem] = []
    lookback_days = _lookback_days(config)

    for source in config.get("fixed_sources", []) or []:
        if not isinstance(source, dict) or not source.get("enabled", False):
            continue
        try:
            fetched = _filter_recent_items(
                collect_fixed_source(source),
                lookback_days=lookback_days,
                source_id=compact_text(source.get("id")),
            )
            reports.append(_report(source, "ok", len(fetched)))
            items.extend(fetched)
        except Exception as exc:
            reports.append(_report(source, "failed", 0, str(exc)))

    tavily_cfg = config.get("tavily_search", {}) or config.get("openclaw_tavily", {})
    if isinstance(tavily_cfg, dict) and tavily_cfg.get("enabled", False):
        runtime_dir = root / str(config.get("runtime", {}).get("output_dir", "runtime"))
        plan_path = runtime_dir / "tavily_search_plan.json"
        write_tavily_plan(tavily_cfg, plan_path)
        results_path = runtime_dir / "tavily_search_results.json"
        executed = execute_tavily_plan(plan_path, results_path)
        reports.append({
            "source_id": "tavily_search",
            "source_type": "tavily_plan",
            "status": "plan_executed" if executed else "plan_written",
            "item_count": executed,
            "output": str(plan_path),
        })
        fetched = read_tavily_results(results_path)
        if fetched:
            fetched = _filter_recent_items(
                fetched, lookback_days=lookback_days, source_id="tavily_search"
            )
            reports.append({
                "source_id": "tavily_search",
                "source_type": "tavily_results",
                "status": "ok",
                "item_count": len(fetched),
                "input": str(results_path),
            })
            items.extend(fetched)

    return dedup_exact(items), reports


def collect_fixed_source(source: Dict[str, Any]) -> List[RawItem]:
    source_type = compact_text(source.get("source_type") or source.get("type"))
    if source_type == "rss":
        return fetch_rss(source)
    if source_type == "github_high_stars":
        return fetch_github_high_stars(source)
    if source_type == "hackernews_top":
        return fetch_hackernews_top(source)
    return []


def _report(source: Dict[str, Any], status: str, count: int, error: str = "") -> Dict[str, Any]:
    out = {
        "source_id": source.get("id", ""),
        "source_type": source.get("source_type") or source.get("type", ""),
        "source_group": source.get("source_group", ""),
        "status": status,
        "item_count": count,
    }
    if error:
        out["error"] = error
    return out


def _lookback_days(config: Dict[str, Any]) -> int:
    runtime = config.get("runtime", {})
    if not isinstance(runtime, dict):
        return 3
    try:
        value = int(runtime.get("lookback_days", 3))
    except (TypeError, ValueError):
        return 3
    return max(1, value)


def _filter_recent_items(
    items: List[RawItem],
    *,
    lookback_days: int,
    source_id: str = "",
) -> List[RawItem]:
    """三态处理：有日期且新 → 收；有日期且旧 → 丢；无日期 → 收但打 date_unknown 标记。

    打标记是为了让下游（精排 prompt、dashboard、健康指标）能识别
    "这条的日期不可信"，而不是把无日期条目和有日期新条目同等对待。
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=lookback_days)
    filtered: List[RawItem] = []
    for item in items:
        published_at = compact_text(item.published_at)
        if not published_at:
            item.raw_metadata["date_unknown"] = True
            filtered.append(item)
            continue
        try:
            parsed = datetime.fromisoformat(published_at)
        except ValueError:
            item.raw_metadata["date_unknown"] = True
            filtered.append(item)
            continue
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        if parsed >= cutoff:
            filtered.append(item)
    return filtered
