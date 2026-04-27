"""Logger for auto_fill — concise per-strategy success/failure output."""
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
            fmt="%(asctime)s [%(levelname)s] %(message)s",
            datefmt="%H:%M:%S",
        )
    )
    logger.addHandler(handler)
    logger.propagate = False
    return logger
