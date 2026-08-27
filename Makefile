.PHONY: test lint typecheck run-chaos report clean docker-up docker-down

test:
	pytest -q

lint:
	ruff check src tests scripts

typecheck:
	mypy src

run-chaos:
	python scripts/run_chaos.py --config configs/default.yaml --out reports/metrics.json
	python scripts/capture_redis_evidence.py --out reports/redis_evidence.json

report:
	python scripts/generate_report.py --metrics reports/metrics.json --config configs/default.yaml --redis-evidence reports/redis_evidence.json --out reports/final_report.md

docker-up:
	docker compose up -d

docker-down:
	docker compose down

clean:
	rm -rf .pytest_cache .ruff_cache .mypy_cache reports/metrics.json reports/final_report.md
