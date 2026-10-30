from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Dict, List

import requests

from morning_newspaper.common import compact_text, positive_int, write_json
from morning_newspaper.models import RawItem, raw_item_from_fields, utc_now_iso

logger = logging.getLogger(__name__)


def write_tavily_plan(config: Dict[str, Any], path: Path) -> None:
    max_items = positive_int(config.get("max_items_per_topic"), 5)
    recency_days = positive_int(config.get("recency_days"), 3)
    fallback_days = positive_int(config.get("fallback_recency_days"), 3)
    now = datetime.now(timezone.utc).replace(microsecond=0)

    plan_items: List[Dict[str, Any]] = []
    topics = config.get("topics", [])
    if isinstance(topics, list):
        for topic in topics:
            if not isinstance(topic, dict):
                continue
            topic_id = compact_text(topic.get("id"))
            query = compact_text(topic.get("query"))
            if not topic_id or not query:
                continue
            plan_items.append({
                "topic_id": topic_id,
                "topic_name": compact_text(topic.get("name")) or topic_id,
                "query": query,
                "domains": topic.get("domains", []) if isinstance(topic.get("domains"), list) else [],
                "max_items": max_items,
                "recency_days": recency_days,
                "since_date": (now - timedelta(days=recency_days)).date().isoformat(),
                "fallback_recency_days": fallback_days,
                "fallback_since_date": (now - timedelta(days=fallback_days)).date().isoformat(),
                "filter_by_published_date": bool(config.get("filter_by_published_date", True)),
            })

    write_json(path, {
        "enabled": True,
        "generated_at": utc_now_iso(),
        "skill_name": compact_text(config.get("skill_name")) or "tavily-search",
        "instructions": "由 OpenClaw 调用 tavily-search skill 执行每日早报搜索，结果写入 tavily_search_results.json。",
        "items": plan_items,
    })


TAVILY_API_URL = "https://api.tavily.com/search"


def _normalize_published_date(value: str) -> str:
    """Tavily 返回的是 RFC 2822（Wed, 28 Oct 2026 08:00:00 GMT），统一转成 ISO 8601。"""
    text = compact_text(value)
    if not text:
        return ""
    try:
        parsed = parsedate_to_datetime(text)
    except (TypeError, ValueError):
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return ""
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).replace(microsecond=0).isoformat()


def _query_end_date() -> str:
    """上界取明天（UTC），避免因时区差把当天的结果切掉。"""
    return (datetime.now(timezone.utc) + timedelta(days=1)).date().isoformat()


def execute_tavily_plan(plan_path: Path, results_path: Path) -> int:
    api_key = os.environ.get("TAVILY_API_KEY", "").strip()
    if not api_key:
        return 0

    if not plan_path.exists():
        return 0

    try:
        payload = json.loads(plan_path.read_text(encoding="utf-8"))
    except Exception:
        return 0

    plan_items = payload.get("items", []) if isinstance(payload, dict) else []
    if not isinstance(plan_items, list):
        return 0

    out_items: list[Dict[str, Any]] = []
    seen_urls: set[str] = set()

    for item in plan_items:
        if not isinstance(item, dict):
            continue
        query = compact_text(item.get("query"))
        if not query:
            continue
        max_items = int(item.get("max_items") or 5)
        topic_id = compact_text(item.get("topic_id"))
        topic_name = compact_text(item.get("topic_name")) or topic_id
        domains = item.get("domains", []) if isinstance(item.get("domains"), list) else []

        body: Dict[str, Any] = {
            "api_key": api_key,
            "query": query,
            "max_results": max_items,
            "search_depth": "basic",
            # published_date 只在显式要求时返回（topic=news 才会自动带上）。
            "include_published_date": True,
        }
        if domains:
            body["include_domains"] = domains
        # 旧代码用的是 Tavily 已废弃的 `days` 参数：它只在 topic=news 时生效，
        # 且当前 API 已不接受该字段，会被静默忽略 —— 这就是旧闻混入的根因。
        # 改用 start_date / end_date 做发布时间窗过滤（格式 YYYY-MM-DD）。
        start_date = compact_text(item.get("since_date"))
        if start_date:
            body["start_date"] = start_date
        end_date = _query_end_date()
        if end_date:
            body["end_date"] = end_date
        if bool(item.get("filter_by_published_date", True)):
            body["filter_by_published_date"] = True

        try:
            resp = requests.post(TAVILY_API_URL, json=body, timeout=30)
            resp.raise_for_status()
            results = resp.json().get("results", [])
        except Exception as exc:
            logger.warning("tavily query failed topic=%s: %s", topic_id, exc)
            continue

        for r in results:
            if not isinstance(r, dict):
                continue
            title = compact_text(r.get("title"))
            url = compact_text(r.get("url"))
            if not title or not url or url in seen_urls:
                continue
            seen_urls.add(url)
            out_items.append({
                "topic_id": topic_id,
                "topic_name": topic_name,
                "source_name": "Tavily Search",
                "source": "tavily-api",
                "title": title,
                "url": url,
                "summary": compact_text(r.get("content")),
                "published_at": _normalize_published_date(r.get("published_date")),
                "fetched_at": utc_now_iso(),
            })

    write_json(results_path, {
        "generated_at": utc_now_iso(),
        "input": str(plan_path),
        "count": len(out_items),
        "items": out_items,
    })
    return len(out_items)


def read_tavily_results(path: Path) -> List[RawItem]:
    if not path.exists():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return []

    raw_items = payload.get("items", []) if isinstance(payload, dict) else []
    if not isinstance(raw_items, list):
        return []

    out: List[RawItem] = []
    for raw in raw_items:
        if not isinstance(raw, dict):
            continue
        title = compact_text(raw.get("title"))
        url = compact_text(raw.get("url"))
        if not title or not url:
            continue
        out.append(raw_item_from_fields(
            source_id="tavily_search",
            source_name=compact_text(raw.get("source_name") or raw.get("source")) or "Tavily Search",
            source_group="primary",
            source_type="tavily_api",
            title=title,
            url=url,
            published_at=compact_text(raw.get("published_at") or raw.get("published_date")),
            raw_snippet=compact_text(raw.get("summary") or raw.get("content") or raw.get("snippet")),
            raw_metadata={
                "topic_id": compact_text(raw.get("topic_id")),
                "source": compact_text(raw.get("source") or raw.get("source_name")),
                "published_date_raw": compact_text(raw.get("published_date")),
            },
            fetched_at=compact_text(raw.get("fetched_at")) or utc_now_iso(),
        ))
    return out
