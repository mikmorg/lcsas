"""LCSAS logging configuration."""

from __future__ import annotations

import logging
import os
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from typing import TextIO


def stdout_closed() -> bool:
    """True when nothing reads stdout any more (#968): a pipe whose reader
    exited, as ``lcsas ... | head`` leaves it."""
    import select

    try:
        poller = select.poll()
        poller.register(sys.stdout.fileno(), select.POLLOUT)
        events = poller.poll(0)
    except (AttributeError, OSError, ValueError):
        return False
    return any(ev & select.POLLERR for _fd, ev in events)


def silence_stdout() -> None:
    """Send the rest of stdout to the null device (#968).

    Called once, when the reader of stdout has gone away: the command
    goes on (a stage or burn must not stop half way because ``head``
    exited) and nothing more is said about the closed pipe, neither per
    log line nor at exit.
    """
    try:
        fd = sys.stdout.fileno()
    except (AttributeError, OSError, ValueError):
        sys.stdout = open(os.devnull, "w")  # noqa: SIM115 - lives to exit
        return
    null = os.open(os.devnull, os.O_WRONLY)
    try:
        os.dup2(null, fd)
    finally:
        os.close(null)


class _StdoutHandler(logging.StreamHandler[TextIO]):
    """Handler that always writes to the *current* ``sys.stdout``.

    Unlike ``StreamHandler(sys.stdout)`` which captures the stream
    reference at construction time, this handler resolves ``sys.stdout``
    on every emit — allowing pytest's ``capsys`` fixture (and any other
    monkey-patching) to capture log output transparently.
    """

    def __init__(self) -> None:  # noqa: D107
        super().__init__()

    @property
    def stream(self) -> TextIO:
        return sys.stdout

    @stream.setter
    def stream(self, _value: TextIO) -> None:
        pass  # ignore attempts to set the stream

    def handleError(self, record: logging.LogRecord) -> None:  # noqa: N802
        """A closed stdout is silenced once, not reported per line (#968)."""
        if isinstance(sys.exc_info()[1], BrokenPipeError):
            silence_stdout()
            return
        super().handleError(record)


class _StderrHandler(logging.StreamHandler[TextIO]):
    """Handler that always writes to the *current* ``sys.stderr``.

    The stderr twin of :class:`_StdoutHandler` (resolved on every emit, so
    ``capsys`` captures it).
    """

    def __init__(self) -> None:  # noqa: D107
        super().__init__()

    @property
    def stream(self) -> TextIO:
        return sys.stderr

    @stream.setter
    def stream(self, _value: TextIO) -> None:
        pass  # ignore attempts to set the stream


@contextmanager
def log_to_stderr() -> Iterator[None]:
    """Send the ``lcsas`` log to stderr while the block runs.

    The log normally goes to stdout.  A command whose stdout is data that
    a script or a file captures (a rebuilt password, a JSON document) must
    keep everything else off stdout, so it runs inside this block.  Level
    and format are kept from the current handler.
    """
    lg = logging.getLogger("lcsas")
    saved = lg.handlers[:]
    handler = _StderrHandler()
    if saved:
        handler.setLevel(saved[0].level)
        handler.setFormatter(saved[0].formatter)
    lg.handlers = [handler]
    try:
        yield
    finally:
        lg.handlers = saved


def setup_logging(verbose: bool = False) -> logging.Logger:
    """Configure and return the root ``lcsas`` logger.

    Parameters
    ----------
    verbose:
        If *True* the log level is set to ``DEBUG``; otherwise ``INFO``.

    The format intentionally omits timestamps — LCSAS is a CLI tool, not
    a daemon.  To get timestamps, set the ``LCSAS_LOG_FORMAT``
    environment variable.

    The handler writes to **stdout** so that ``capsys`` in tests captures
    log output the same way it captured the old ``print()`` calls.
    """
    logger = logging.getLogger("lcsas")

    # Avoid adding duplicate handlers if called more than once
    if logger.handlers:
        # Still update the level in case verbose changed between calls
        level = logging.DEBUG if verbose else logging.INFO
        logger.setLevel(level)
        for h in logger.handlers:
            h.setLevel(level)
        return logger

    level = logging.DEBUG if verbose else logging.INFO
    logger.setLevel(level)

    fmt = os.environ.get("LCSAS_LOG_FORMAT", "%(message)s")
    handler = _StdoutHandler()
    handler.setLevel(level)
    handler.setFormatter(logging.Formatter(fmt))
    logger.addHandler(handler)

    return logger


def get_logger() -> logging.Logger:
    """Return the ``lcsas`` logger (creates it unconfigured if needed)."""
    return logging.getLogger("lcsas")


_PASSWORD_SUFFIXES = (".key", ".pem", ".pass", ".password", ".secret")


def mask_password_path(path: str) -> str:
    """Mask a path if it looks like a password file.

    Returns ``***`` if the path ends with a known password-file suffix,
    otherwise returns the path unchanged.
    """
    lower = path.lower()
    for suffix in _PASSWORD_SUFFIXES:
        if lower.endswith(suffix):
            return "***"
    return path
