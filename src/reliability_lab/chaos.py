from __future__ import annotations

import json
import random
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from reliability_lab.cache import ResponseCache, SharedRedisCache
from reliability_lab.circuit_breaker import CircuitBreaker
from reliability_lab.config import LabConfig, ScenarioConfig
from reliability_lab.gateway import ReliabilityGateway
from reliability_lab.metrics import RunMetrics
from reliability_lab.providers import FakeLLMProvider


def load_queries(path: str | Path = "data/sample_queries.jsonl") -> list[str]:
    queries: list[str] = []
    for line in Path(path).read_text().splitlines():
        if not line.strip():
            continue
        queries.append(json.loads(line)["query"])
    return queries


def build_gateway(config: LabConfig, provider_overrides: dict[str, float] | None = None) -> ReliabilityGateway:
    providers = []
    for p in config.providers:
        fail_rate = provider_overrides.get(p.name, p.fail_rate) if provider_overrides else p.fail_rate
        providers.append(FakeLLMProvider(p.name, fail_rate, p.base_latency_ms, p.cost_per_1k_tokens))
    breakers = {
        p.name: CircuitBreaker(
            name=p.name,
            failure_threshold=config.circuit_breaker.failure_threshold,
            reset_timeout_seconds=config.circuit_breaker.reset_timeout_seconds,
            success_threshold=config.circuit_breaker.success_threshold,
        )
        for p in config.providers
    }
    cache: ResponseCache | SharedRedisCache | None = None
    if config.cache.enabled:
        if config.cache.backend == "redis":
            cache = SharedRedisCache(
                config.cache.redis_url,
                config.cache.ttl_seconds,
                config.cache.similarity_threshold,
            )
        else:
            cache = ResponseCache(config.cache.ttl_seconds, config.cache.similarity_threshold)
    return ReliabilityGateway(providers, breakers, cache)


def calculate_recovery_time_ms(gateway: ReliabilityGateway) -> float | None:
    """Derive recovery time from circuit breaker transition logs."""
    recovery_times: list[float] = []
    for breaker in gateway.breakers.values():
        open_ts: float | None = None
        for entry in breaker.transition_log:
            to_state = entry.get("to")
            ts = float(entry.get("ts", 0.0))
            if to_state == "open":
                if open_ts is None:
                    open_ts = ts
            elif to_state == "closed" and open_ts is not None:
                recovery_times.append((ts - open_ts) * 1000.0)
                open_ts = None
    if recovery_times:
        return sum(recovery_times) / len(recovery_times)
    return None


def run_scenario(
    config: LabConfig,
    queries: list[str],
    scenario: ScenarioConfig,
    *,
    seed: int | None = None,
    concurrent_workers: int | None = None,
) -> RunMetrics:
    """Run a single named chaos scenario."""
    if not queries:
        raise ValueError("At least one query is required")
    scenario_seed = config.load_test.random_seed if seed is None else seed
    random.seed(scenario_seed)
    query_rng = random.Random(scenario_seed)
    gateway = build_gateway(config, scenario.provider_overrides or None)
    metrics = RunMetrics()
    prompts = [query_rng.choice(queries) for _ in range(config.load_test.requests)]
    workers = concurrent_workers or config.load_test.concurrent_workers
    started = time.perf_counter()
    if workers == 1:
        results = [gateway.complete(prompt) for prompt in prompts]
    else:
        with ThreadPoolExecutor(max_workers=workers) as executor:
            results = list(executor.map(gateway.complete, prompts))

    metrics.duration_ms = (time.perf_counter() - started) * 1000.0
    for result in results:
        metrics.total_requests += 1
        metrics.estimated_cost += result.estimated_cost
        if result.cache_hit:
            metrics.cache_hits += 1
            metrics.estimated_cost_saved += 0.001
        if result.route == "fallback":
            metrics.fallback_successes += 1
            metrics.successful_requests += 1
        elif result.route == "static_fallback":
            metrics.static_fallbacks += 1
            metrics.failed_requests += 1
        else:
            metrics.successful_requests += 1
        if result.latency_ms > 0:
            metrics.latencies_ms.append(result.latency_ms)

    metrics.circuit_open_count = sum(
        sum(1 for t in b.transition_log if t.get("to") == "open")
        for b in gateway.breakers.values()
    )
    metrics.recovery_time_ms = calculate_recovery_time_ms(gateway)
    return metrics


def _merge_metrics(combined: RunMetrics, result: RunMetrics) -> None:
    """Merge counters and samples without discarding scenario-level evidence."""
    combined.total_requests += result.total_requests
    combined.successful_requests += result.successful_requests
    combined.failed_requests += result.failed_requests
    combined.fallback_successes += result.fallback_successes
    combined.static_fallbacks += result.static_fallbacks
    combined.cache_hits += result.cache_hits
    combined.circuit_open_count += result.circuit_open_count
    combined.estimated_cost += result.estimated_cost
    combined.estimated_cost_saved += result.estimated_cost_saved
    combined.duration_ms += result.duration_ms
    combined.latencies_ms.extend(result.latencies_ms)


def run_cache_benchmark(config: LabConfig, queries: list[str]) -> dict[str, dict[str, object]]:
    """Compare cache enabled/disabled under the same healthy workload and seed."""
    scenario = ScenarioConfig(
        name="cache_benchmark",
        description="Healthy providers; identical query sequence for both cache modes",
        provider_overrides={provider.name: 0.0 for provider in config.providers},
    )
    benchmark: dict[str, dict[str, object]] = {}
    for enabled, label in ((False, "without_cache"), (True, "with_cache")):
        benchmark_config = config.model_copy(
            update={"cache": config.cache.model_copy(update={"enabled": enabled})}
        )
        result = run_scenario(
            benchmark_config,
            queries,
            scenario,
            seed=config.load_test.random_seed,
            concurrent_workers=config.load_test.concurrent_workers,
        )
        benchmark[label] = result.to_report_dict()
    return benchmark


def run_simulation(config: LabConfig, queries: list[str]) -> RunMetrics:
    """Run all named scenarios from config, or a default run if none defined."""
    if not config.scenarios:
        default_scenario = ScenarioConfig(name="default", description="baseline run")
        metrics = run_scenario(config, queries, default_scenario)
        metrics.scenarios = {"default": "pass" if metrics.successful_requests > 0 else "fail"}
        return metrics

    combined = RunMetrics()
    recovery_times: list[float] = []
    for scenario in config.scenarios:
        result = run_scenario(
            config,
            queries,
            scenario,
            seed=config.load_test.random_seed + len(combined.scenarios),
        )

        if scenario.name == "primary_timeout_100":
            passed = result.fallback_success_rate > 0.8 and result.availability > 0.8
        elif scenario.name == "primary_flaky_50":
            passed = result.availability > 0.8
        elif scenario.name == "all_healthy":
            passed = result.availability >= 0.95
        else:
            passed = result.successful_requests > 0

        combined.scenarios[scenario.name] = "pass" if passed else "fail"
        combined.scenario_metrics[scenario.name] = result.to_report_dict()
        _merge_metrics(combined, result)
        if result.recovery_time_ms is not None:
            recovery_times.append(result.recovery_time_ms)

    if recovery_times:
        combined.recovery_time_ms = sum(recovery_times) / len(recovery_times)
    combined.benchmarks = run_cache_benchmark(config, queries)
    return combined
