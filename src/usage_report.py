from __future__ import annotations

import argparse
import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path

from src.config import Settings
from src.services.usage import usage_store


def timestamp(value: str) -> str:
    try:
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            raise ValueError
        return parsed.astimezone(UTC).isoformat()
    except ValueError:
        raise argparse.ArgumentTypeError("Use a timezone-aware ISO timestamp") from None


def main() -> None:
    parser = argparse.ArgumentParser(description="按产品查询持久化生图统计，输出标价估算而非官方实扣")
    parser.add_argument("--app-id", required=True)
    parser.add_argument("--db")
    parser.add_argument("--since", type=timestamp)
    parser.add_argument("--until", type=timestamp)
    args = parser.parse_args()
    settings = Settings()
    path = args.db or settings.usage_db_path or str(Path(settings.storage_dir).parent / "usage.sqlite3")
    if not Path(path).is_file():
        parser.error("Usage database does not exist")
    if args.since and args.until and args.since >= args.until:
        parser.error("since must be earlier than until")
    result = asyncio.run(
        usage_store(path, settings.usage_retention_days).summary(args.app_id, since=args.since, until=args.until)
    )
    print(json.dumps(result, ensure_ascii=True, indent=2))


if __name__ == "__main__":
    main()
