from __future__ import annotations

import argparse
from pathlib import Path
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC = PROJECT_ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from morning_newspaper.collectors import collect_all
from morning_newspaper.common import load_env_file, load_yaml, write_json, write_text
from morning_newspaper.models import utc_now_iso


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Collect raw morning newspaper cards.")
    parser.add_argument("--config", default=str(PROJECT_ROOT / "config" / "sources.yaml"))
    parser.add_argument("--dry-run", action="store_true", help="Only print source plan, do not fetch network sources.")
    parser.add_argument("--skip-tavily", action="store_true", help="Disable Tavily plan/results for local collector tests.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    load_env_file(PROJECT_ROOT / ".env")
    config_path = Path(args.config)
    config = load_yaml(config_path)
    if args.skip_tavily and isinstance(config.get("openclaw_tavily"), dict):
        config["openclaw_tavily"]["enabled"] = False
    runtime_dir = PROJECT_ROOT / str(config.get("runtime", {}).get("output_dir", "runtime"))

    if args.dry_run:
        fixed = [s for s in config.get("fixed_sources", []) or [] if isinstance(s, dict) and s.get("enabled", False)]
        payload = {
            "generated_at": utc_now_iso(),
            "config": str(config_path),
            "fixed_sources": [
                {
                    "id": item.get("id"),
                    "source_type": item.get("source_type"),
                    "source_group": item.get("source_group"),
                    "max_items": item.get("max_items"),
                }
                for item in fixed
            ],
            "openclaw_tavily_enabled": bool(config.get("openclaw_tavily", {}).get("enabled", False)),
            "paused_sources": config.get("paused_sources", []),
        }
        write_json(runtime_dir / "source_plan.json", payload)
        print(f"wrote {runtime_dir / 'source_plan.json'}")
        return

    items, reports = collect_all(config, root=PROJECT_ROOT)
    payload = {
        "generated_at": utc_now_iso(),
        "count": len(items),
        "items": [item.to_dict() for item in items],
    }
    write_json(runtime_dir / "collected_raw.json", payload)
    date_unknown = [item for item in items if item.raw_metadata.get("date_unknown")]
    write_json(runtime_dir / "collect_report.json", {
        "generated_at": utc_now_iso(),
        "item_count": len(items),
        "date_unknown_count": len(date_unknown),
        "date_unknown_ratio": round(len(date_unknown) / len(items), 3) if items else 0.0,
        "sources": reports,
    })
    write_text(
        runtime_dir / "collect_report.md",
        _render_report(len(items), reports, date_unknown_count=len(date_unknown)),
    )
    print(f"collected items={len(items)}")
    print(f"wrote {runtime_dir / 'collected_raw.json'}")


def _render_report(item_count: int, reports: list[dict], *, date_unknown_count: int = 0) -> str:
    lines = [
        "# Raw Collection Report",
        "",
        f"- total_items: {item_count}",
        f"- date_unknown: {date_unknown_count}",
        "",
        "| source_id | source_type | status | item_count | note |",
        "| --- | --- | --- | ---: | --- |",
    ]
    for report in reports:
        note = report.get("error") or report.get("output") or report.get("input") or ""
        lines.append(
            f"| {report.get('source_id', '')} | {report.get('source_type', '')} | {report.get('status', '')} | {report.get('item_count', 0)} | {note} |"
        )
    lines.append("")
    return "\n".join(lines)


if __name__ == "__main__":
    main()
