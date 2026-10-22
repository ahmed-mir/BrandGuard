"""Logging helpers that keep exception values out of deployment logs."""
import logging
import traceback


class SafeTracebackFormatter(logging.Formatter):
    """Render stack frames and exception class, but redact exception text.

    Database drivers and validation libraries can include input values in an
    exception message. Trace frames are useful for diagnosis without logging
    marketing copy, file content, credentials, or other exception values.
    """

    def formatException(self, exc_info):
        exc_type, _exc_value, tb = exc_info
        frames = "".join(
            f'  File "{frame.filename}", line {frame.lineno}, in {frame.name}\n'
            for frame in traceback.extract_tb(tb)
        ) if tb else ""
        exception_name = exc_type.__name__ if exc_type else "Exception"
        return f"Traceback (most recent call last):\n{frames}{exception_name}: [exception message redacted]"
