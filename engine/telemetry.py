"""Per-position timings and API usage; never store prompts, responses or credentials."""
from __future__ import annotations

import threading
import time
from collections import defaultdict
from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps
from .research_publication import IncrementalMetrics


class Metrics(IncrementalMetrics):
    def __init__(self, sink=None, policy=None):
        super().__init__(sink=sink, policy=policy, retain_events=True)


_METRICS: ContextVar[Metrics | None] = ContextVar("research_metrics", default=None)
_METRIC_STREAM: ContextVar[tuple | None] = ContextVar("research_metric_stream", default=None)


@contextmanager
def stream_metrics(sink, policy):
    """Enclosing row sink survives nested operation capture; thread-local scope."""
    token = _METRIC_STREAM.set((sink, policy))
    try:
        yield
    finally:
        _METRIC_STREAM.reset(token)


@contextmanager
def capture_metrics():
    stream = _METRIC_STREAM.get()
    metrics = Metrics(*stream) if stream else Metrics()
    token = _METRICS.set(metrics)
    try:
        yield metrics
    finally:
        _METRICS.reset(token)


def record_stage(stage: str, seconds: float, *, ok: bool = True, label: str = "") -> None:
    metrics = _METRICS.get()
    if metrics is not None:
        metrics.add({"stage": stage, "seconds": round(seconds, 3), "ok": ok,
                     "label": label})


@contextmanager
def timed_stage(stage: str, label: str = ""):
    started = time.monotonic()
    ok = False
    try:
        yield
        ok = True
    finally:
        record_stage(stage, time.monotonic() - started, ok=ok, label=label)


def timed_call(stage: str):
    def decorate(function):
        @wraps(function)
        def call(*args, **kwargs):
            with timed_stage(stage):
                return function(*args, **kwargs)
        return call
    return decorate


def record_llm_usage(provider: str, model: str, usage: dict) -> None:
    metrics = _METRICS.get()
    if metrics is not None and usage:
        metrics.add({"stage": "llm_usage", "provider": provider, "model": model,
                     "input_tokens": int(usage.get("input_tokens", usage.get("prompt_tokens", 0)) or 0),
                     "output_tokens": int(usage.get("output_tokens", usage.get("completion_tokens", 0)) or 0),
                     "cache_hit_tokens": int(usage.get("prompt_cache_hit_tokens", usage.get("cache_read_input_tokens", 0)) or 0)})
