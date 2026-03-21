import logging
import os
import sys


_COLORS = {
    "DEBUG": "\033[36m",
    "INFO": "\033[32m",
    "WARNING": "\033[33m",
    "ERROR": "\033[31m",
    "CRITICAL": "\033[35m",
}
_RESET = "\033[0m"


def _use_color() -> bool:
    if os.environ.get("NO_COLOR"):
        return False
    if os.environ.get("TERM") == "dumb":
        return False
    return hasattr(sys.stderr, "isatty") and sys.stderr.isatty()


class _ColorFormatter(logging.Formatter):
    def __init__(self) -> None:
        super().__init__("[%(name)s] %(levelname)s %(message)s")
        self._use_color = _use_color()

    def format(self, record: logging.LogRecord) -> str:
        message = super().format(record)
        if not self._use_color:
            return message
        color = _COLORS.get(record.levelname)
        if not color:
            return message
        return f"{color}{message}{_RESET}"


def get_logger(name: str = "cypack") -> logging.Logger:
    logger = logging.getLogger(name)
    if logger.handlers:
        return logger

    handler = logging.StreamHandler()
    handler.setFormatter(_ColorFormatter())
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG if os.environ.get("CYPACK_DEBUG") else logging.INFO)
    logger.propagate = False
    return logger
