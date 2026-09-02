"""Minimal logging setup. Swap for structlog later if needed."""

import logging

#: DEBUG on the root logger turns every HTTP call into a dozen lines of
#: httpcore frame chatter, which buries our own ingest/retrieval logs. These
#: stay at INFO (or WARNING) regardless, so debug=True means *our* debug.
NOISY_LOGGERS = {
    "httpcore": logging.WARNING,
    "httpx": logging.WARNING,
    "urllib3": logging.WARNING,
    "chromadb": logging.INFO,
    "chromadb.config": logging.WARNING,
    "chromadb.telemetry": logging.WARNING,
    "posthog": logging.WARNING,
    "watchfiles": logging.WARNING,
}


def configure_logging(debug: bool = False) -> None:
    logging.basicConfig(
        level=logging.DEBUG if debug else logging.INFO,
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
    )
    for name, level in NOISY_LOGGERS.items():
        logging.getLogger(name).setLevel(level)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
