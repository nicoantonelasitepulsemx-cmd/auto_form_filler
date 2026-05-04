"""Logger for auto_fill — concise per-strategy success/failure output.

C5 fix: include the thread name in the format string so concurrent
worker output is no longer interleaved without attribution. The
WorkerPool spins up multiple threads (per account) and the recorder
uses a separate screenshot writer thread; without the thread name,
debugging an interleaved race was painful. The new format is:

    HH:MM:SS [LEVEL] [Thread-Name] message

Existing callers don't have to change anything — the format swap is
transparent unless you were grepping for ``[INFO]`` adjacency.
"""
from __future__ import annotations

import logging
import sys


def get_logger(debug: bool = False) -> logging.Logger:
    logger = logging.getLogger("auto_fill")
    if logger.handlers:
        # Already configured
        logger.setLevel(logging.DEBUG if debug else logging.INFO)
        return logger

    logger.setLevel(logging.DEBUG if debug else logging.INFO)
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        logging.Formatter(
            fmt="%(asctime)s [%(levelname)s] [%(threadName)s] %(message)s",
            datefmt="%H:%M:%S",
        )
    )
    logger.addHandler(handler)
    logger.propagate = False
    return logger
