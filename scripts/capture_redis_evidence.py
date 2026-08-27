from __future__ import annotations

import argparse
import json
from pathlib import Path

from reliability_lab.cache import SharedRedisCache


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--redis-url", default="redis://localhost:6379/0")
    parser.add_argument("--out", default="reports/redis_evidence.json")
    args = parser.parse_args()

    prefix = "rl:evidence:"
    writer = SharedRedisCache(args.redis_url, 300, 0.92, prefix=prefix)
    reader = SharedRedisCache(args.redis_url, 300, 0.92, prefix=prefix)
    writer.flush()
    query = "what are the operating hours for customer service"
    response = "[evidence] customer service is available during published hours"
    writer.set(query, response)
    cached, score = reader.get(query)
    key = f"{prefix}{writer._query_hash(query)}"
    evidence = {
        "ping": writer.ping() and reader.ping(),
        "cross_instance_read": cached == response and score == 1.0,
        "key": key,
        "ttl_seconds": writer._redis.ttl(key),
        "fields": writer._redis.hgetall(key),
    }
    target = Path(args.out)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(evidence, indent=2, ensure_ascii=False), encoding="utf-8")
    writer.flush()
    writer.close()
    reader.close()
    print(f"wrote {target}")


if __name__ == "__main__":
    main()
