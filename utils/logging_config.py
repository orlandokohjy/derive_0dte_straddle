"""Structured logging setup using structlog."""
from __future__ import annotations

import logging
import os
import sys

import structlog

import config


class _Tee:
    """Fan writes out to several streams.

    structlog's ``PrintLoggerFactory`` writes straight to a stream and never
    touches the stdlib handlers configured below, so the ``FileHandler`` here
    was dead: ``logs/algo.log`` stayed empty and every structured event lived
    only in the container's stdout. A ``docker-compose up --force-recreate``
    then destroyed the whole trail — which is exactly how we lost the
    ``order_failed`` error strings for the 2026-07-30 no-fill sessions.
    Teeing keeps `docker logs` working AND persists to the mounted volume.
    """

    def __init__(self, *streams) -> None:
        self._streams = streams

    def write(self, data: str) -> None:
        for s in self._streams:
            try:
                s.write(data)
            except Exception:  # never let logging kill the algo
                pass

    def flush(self) -> None:
        for s in self._streams:
            try:
                s.flush()
            except Exception:
                pass


def setup_logging() -> None:
    log_dir = os.path.dirname(config.LOG_FILE)
    if log_dir:
        os.makedirs(log_dir, exist_ok=True)

    # line-buffered so a crash or `docker kill` cannot lose the tail
    log_fh = open(config.LOG_FILE, "a", buffering=1, encoding="utf-8")

    logging.basicConfig(
        level=getattr(logging, config.LOG_LEVEL, logging.INFO),
        format="%(message)s",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.StreamHandler(log_fh),
        ],
    )

    processors = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]

    if config.LOG_JSON:
        processors.append(structlog.processors.JSONRenderer())
    else:
        processors.append(structlog.dev.ConsoleRenderer())

    structlog.configure(
        processors=processors,
        wrapper_class=structlog.make_filtering_bound_logger(
            getattr(logging, config.LOG_LEVEL, logging.INFO)
        ),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(
            file=_Tee(sys.stdout, log_fh)),
        cache_logger_on_first_use=True,
    )
