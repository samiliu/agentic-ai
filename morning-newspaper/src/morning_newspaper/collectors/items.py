from __future__ import annotations

import re
from typing import Any, Dict, List

from morning_newspaper.common import compact_text
from morning_newspaper.models import RawItem, raw_item_from_fields


def make_raw_item(
    source: Dict[str, Any],
    *,
    title: str,
    url: str,
    raw_snippet: str = "",
    published_at: str = "",
    raw_metadata: Dict[str, Any] | None = None,
    fetched_at: str = "",
) -> RawItem:
    return raw_item_from_fields(
        source_id=compact_text(source.get("id") or source.get("source_id")),
        source_name=compact_text(source.get("source_name") or source.get("name")),
        source_group=compact_text(source.get("source_group")) or "primary",
        source_type=compact_text(source.get("source_type") or source.get("type")),
        title=title,
        url=url,
        published_at=published_at,
        raw_snippet=raw_snippet,
        raw_metadata=raw_metadata or {},
        fetched_at=fetched_at,
    )


_SITE_SUFFIX = re.compile(
    r"\s*[-|\u2013\u2014]\s*(wsj|wall street journal|hacker news|techcrunch|the verge|"
    r"bloomberg|reuters|cnbc|financial times|ft|arxiv)\s*$",
    re.I,
)


def normalize_title(title: str) -> str:
    """小写、去掉站点后缀与标点，让近似重复标题收敛到同一个 key。"""
    text = compact_text(title).lower()
    if not text:
        return ""
    text = _SITE_SUFFIX.sub("", text)
    return re.sub(r"[^\w\u4e00-\u9fff]+", "", text)


def dedup_exact(items: List[RawItem]) -> List[RawItem]:
    out: List[RawItem] = []
    seen_urls: set[str] = set()
    seen_titles: set[str] = set()
    for item in items:
        url_key = compact_text(item.url)
        if url_key and url_key in seen_urls:
            continue
        title_key = normalize_title(item.title)
        if title_key and title_key in seen_titles:
            continue
        if url_key:
            seen_urls.add(url_key)
        if title_key:
            seen_titles.add(title_key)
        out.append(item)
    return out
