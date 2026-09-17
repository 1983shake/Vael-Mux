import logging
import os
import sys

_LEVEL = os.environ.get("VAEL_LOG_LEVEL", "INFO").upper()


def setup_logger(name: str = "vael-mux") -> logging.Logger:
    logger = logging.getLogger(name)
    if logger.handlers:
        return logger
    logger.setLevel(_LEVEL)
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        logging.Formatter(
            fmt="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
    )
    logger.addHandler(handler)
    logger.propagate = False
    return logger


logger = setup_logger()
