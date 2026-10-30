from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
import html
import re
from typing import Any, Dict, List
from urllib.parse import urlparse

import requests

from morning_newspaper.common import USER_AGENT, compact_text


MAX_BODY_CHARS = 12000
MIN_BODY_CHARS = 160


def enrich_items_with_content(items: List[Dict[str, Any]], *, max_workers: int = 4) -> List[Dict[str, Any]]:
    rows = [dict(item) for item in items]
    if not rows:
        return []

    results: Dict[int, Dict[str, Any]] = {}
    workers = max(1, min(max_workers, len(rows)))
    with ThreadPoolExecutor(max_workers=workers) as executor:
        future_map = {executor.submit(enrich_one_item, row): idx for idx, row in enumerate(rows)}
        for future in as_completed(future_map):
            idx = future_map[future]
            try:
                results[idx] = future.result()
            except Exception as exc:
                row = rows[idx]
                results[idx] = _with_fetch_result(
                    row,
                    fetch_status="failed",
                    extract_method="error",
                    body_text="",
                    note=f"{type(exc).__name__}: {exc}",
                )
    return [results[idx] for idx in range(len(rows))]


def enrich_one_item(item: Dict[str, Any]) -> Dict[str, Any]:
    url = compact_text(item.get("url"))
    if not url:
        return _with_fetch_result(item, fetch_status="failed", extract_method="no_url", body_text="", note="missing url")

    host = urlparse(url).netloc.lower()
    source_type = compact_text(item.get("source_type"))

    if source_type == "github_high_stars" and host.endswith("github.com"):
        return _fetch_github_repo(item, url)

    return _fetch_web_page(item, url)


def _fetch_github_repo(item: Dict[str, Any], url: str) -> Dict[str, Any]:
    owner_repo = _parse_github_owner_repo(url)
    if not owner_repo:
        return _fetch_web_page(item, url)

    owner, repo = owner_repo
    api_url = f"https://api.github.com/repos/{owner}/{repo}/readme"
    try:
        text = _http_text(api_url, headers={"Accept": "application/vnd.github.raw"})
        clean = _clean_markdown_text(text)
        if len(clean) >= MIN_BODY_CHARS:
            return _with_fetch_result(
                item,
                fetch_status="ok",
                extract_method="github_readme_api",
                body_text=clean,
                note="readme fetched from GitHub API",
            )
    except Exception:
        pass

    return _fetch_web_page(item, url, preferred_method="github_html")


def _fetch_web_page(item: Dict[str, Any], url: str, *, preferred_method: str = "html_text") -> Dict[str, Any]:
    try:
        raw = _http_text(url)
    except Exception as exc:
        fallback_published_at = _resolve_published_at(item, raw_html="")
        return _with_fetch_result(
            item,
            fetch_status="failed",
            extract_method=preferred_method,
            body_text="",
            note=f"fetch failed: {type(exc).__name__}",
            published_at=fallback_published_at,
        )

    extracted = _extract_main_text(raw, url=url)
    clean = _clean_text(extracted)
    published_at = _resolve_published_at(item, raw_html=raw)
    if len(clean) < MIN_BODY_CHARS:
        return _with_fetch_result(
            item,
            fetch_status="too_short",
            extract_method=preferred_method,
            body_text=clean,
            note=f"extracted text too short: {len(clean)} chars",
            published_at=published_at,
        )
    return _with_fetch_result(
        item,
        fetch_status="ok",
        extract_method=preferred_method,
        body_text=clean,
        note="",
        published_at=published_at,
    )


def _http_text(url: str, *, headers: Dict[str, str] | None = None, timeout: int = 20) -> str:
    request_headers = {
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,text/plain;q=0.8,*/*;q=0.5",
    }
    if headers:
        request_headers.update(headers)
    response = requests.get(url, headers=request_headers, timeout=timeout)
    response.raise_for_status()
    if response.content.startswith(b"\xef\xbb\xbf"):
        response.encoding = "utf-8-sig"
    if not response.encoding:
        response.encoding = response.apparent_encoding or "utf-8"
    return response.text


def _extract_main_text(raw_html: str, *, url: str) -> str:
    host = urlparse(url).netloc.lower()
    html_part = _best_html_region(raw_html, host=host)
    return _html_to_text(html_part)


def _best_html_region(raw_html: str, *, host: str) -> str:
    if "github.com" in host:
        match = re.search(r'<article[^>]*class="[^"]*markdown-body[^"]*"[^>]*>([\s\S]*?)</article>', raw_html, re.I)
        if match:
            return match.group(1)

    patterns = [
        r'<div[^>]+class="[^"]*(col-xs-12|col-sm-8|col-md-8|article__content|post-body)[^"]*"[^>]*>([\s\S]*?)</div>',
        r"<article[^>]*>([\s\S]*?)</article>",
        r"<main[^>]*>([\s\S]*?)</main>",
        r'<div[^>]+class="[^"]*(post-content|entry-content|article-content|markdown-body|content)[^"]*"[^>]*>([\s\S]*?)</div>',
    ]
    for pattern in patterns:
        match = re.search(pattern, raw_html, re.I)
        if not match:
            continue
        text = match.group(match.lastindex or 1)
        if len(text) >= 400:
            return text
    meta_description = _meta_description(raw_html)
    if meta_description:
        return meta_description
    return raw_html


def _meta_description(raw_html: str) -> str:
    match = re.search(r'<meta[^>]+name=["\']description["\'][^>]+content=["\']([^"\']+)["\']', raw_html, re.I)
    if not match:
        match = re.search(r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+name=["\']description["\']', raw_html, re.I)
    return html.unescape(match.group(1)).strip() if match else ""


def _resolve_published_at(item: Dict[str, Any], *, raw_html: str) -> str:
    existing = compact_text(item.get("published_at"))
    if existing:
        return existing

    source_type = compact_text(item.get("source_type"))
    if source_type == "tavily_api":
        # 只信任页面里真实的发布时间标签。抓不到就留空，
        # 绝不能用 fetched_at 冒充 —— 那会把旧闻伪装成今天的新闻。
        return _extract_published_at_from_html(raw_html)
    return existing


def _extract_published_at_from_html(raw_html: str) -> str:
    patterns = [
        r'<meta[^>]+property=["\']article:published_time["\'][^>]+content=["\']([^"\']+)["\']',
        r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']article:published_time["\']',
        r'<meta[^>]+name=["\']pubdate["\'][^>]+content=["\']([^"\']+)["\']',
        r'<meta[^>]+name=["\']publish-date["\'][^>]+content=["\']([^"\']+)["\']',
        r'<meta[^>]+name=["\']date["\'][^>]+content=["\']([^"\']+)["\']',
        r'<time[^>]+datetime=["\']([^"\']+)["\']',
    ]
    for pattern in patterns:
        match = re.search(pattern, raw_html, re.I)
        if not match:
            continue
        value = html.unescape(match.group(1)).strip()
        normalized = _normalize_datetime(value)
        if normalized:
            return normalized
    return ""


def _normalize_datetime(value: str) -> str:
    text = compact_text(value)
    if not text:
        return ""
    text = text.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return ""
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.replace(microsecond=0).isoformat()


def _html_to_text(raw_html: str) -> str:
    text = raw_html or ""
    for tag in ["script", "style", "noscript", "svg", "nav", "header", "footer", "form"]:
        text = re.sub(rf"<{tag}\b[\s\S]*?</{tag}>", " ", text, flags=re.I)
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.I)
    text = re.sub(r"</(p|div|li|h[1-6]|section|article|tr)>", "\n", text, flags=re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    return html.unescape(text)


def _clean_text(text: str) -> str:
    lines = []
    seen: set[str] = set()
    for raw_line in (text or "").splitlines():
        line = re.sub(r"\s+", " ", raw_line).strip()
        if not line or _looks_noise(line):
            continue
        key = line.lower()
        if key in seen:
            continue
        seen.add(key)
        lines.append(line)
    return "\n".join(lines)[:MAX_BODY_CHARS].strip()


def _clean_markdown_text(text: str) -> str:
    value = text or ""
    value = re.sub(r"!\[[^\]]*\]\([^)]+\)", " ", value)
    value = re.sub(r"\[!\[[^\]]*\]\([^)]+\)\]\([^)]+\)", " ", value)
    value = re.sub(r"<img\b[^>]*>", " ", value, flags=re.I)
    value = re.sub(r"</?(div|span|p|h[1-6]|a|br|strong|em|sub|sup|center)\b[^>]*>", " ", value, flags=re.I)
    value = re.sub(r"&nbsp;", " ", value)
    value = re.sub(r"\[[^\]]*\]\([^)]+\)", lambda m: m.group(0).split("](", 1)[0].lstrip("["), value)
    value = re.sub(r"`{1,3}", "", value)
    value = re.sub(r"^\s{0,3}#{1,6}\s*", "", value, flags=re.M)
    return _clean_text(value)


def _looks_noise(line: str) -> bool:
    lower = line.lower()
    if len(line) < 8:
        return True
    if re.fullmatch(r"[\W\d_\-:/|]+", line):
        return True
    bad_markers = [
        "skip to content",
        "sign in",
        "navigation menu",
        "privacy policy",
        "terms of service",
        "cookie",
        "advertisement",
        "subscribe to",
        "share this article",
        "use saved searches",
        "you signed in with another tab",
        "you signed out in another tab",
        "reload to refresh your session",
        "github copilot",
        "search code, repositories",
        "all rights reserved",
        "an official website of the united states government",
        "official websites use .gov",
        "secure .gov websites use https",
        "here's how you know",
        "toggle dropdown menu",
        "search submit search button",
        "main menu toggle button",
        "federal reserve board -",
        "accessibility",
        "stay connected",
        "crates.io",
        "dependencies",
        "dependents",
        "versions",
        "owners",
        "repository",
        "documentation",
    ]
    return any(marker in lower for marker in bad_markers)


def _parse_github_owner_repo(url: str) -> tuple[str, str] | None:
    parsed = urlparse(url)
    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) < 2:
        return None
    return parts[0], parts[1]


def _with_fetch_result(
    item: Dict[str, Any],
    *,
    fetch_status: str,
    extract_method: str,
    body_text: str,
    note: str,
    published_at: str | None = None,
) -> Dict[str, Any]:
    out = dict(item)
    clean = body_text[:MAX_BODY_CHARS].strip()
    out.update({
        "fetch_status": fetch_status,
        "extract_method": extract_method,
        "body_text": clean,
        "body_length": len(clean),
        "note": note,
    })
    if published_at:
        out["published_at"] = published_at
    return out
