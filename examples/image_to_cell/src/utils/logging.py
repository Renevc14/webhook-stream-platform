"""
Logging Configuration
=====================

Production-grade structured logging with:
- Consistent formatting across all services
- Context tracking (org_id, worksheet_id, etc.)
- Flow tracking for multi-step operations
- Timing/duration measurement
- Log levels following best practices
"""

import logging
import sys
import time
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime
from functools import wraps
from typing import Optional, Dict, Any, Callable

# ContextVar for async-safe log context propagation
_context_var: ContextVar[Optional["LogContext"]] = ContextVar("log_context", default=None)


# =============================================================================
# Log Format Constants
# =============================================================================

# Standard format: timestamp | level | service | message
STANDARD_FORMAT = "%(asctime)s | %(levelname)-5s | %(name)-20s | %(message)s"

# Detailed format for debugging
DEBUG_FORMAT = (
    "%(asctime)s | %(levelname)-5s | %(name)-20s | "
    "%(filename)s:%(lineno)d | %(message)s"
)

# JSON-like format for log aggregators
STRUCTURED_FORMAT = (
    '{"time":"%(asctime)s","level":"%(levelname)s",'
    '"service":"%(name)s","message":"%(message)s"}'
)

DATE_FORMAT = "%Y-%m-%d %H:%M:%S"


# =============================================================================
# Flow Tags - Consistent prefixes for different operations
# =============================================================================

class FlowTags:
    """Standard prefixes for log messages to enable easy filtering."""

    # Webhook lifecycle
    WEBHOOK = "[WEBHOOK]"
    WEBHOOK_REGISTER = "[WEBHOOK:REGISTER]"
    WEBHOOK_VERIFY = "[WEBHOOK:VERIFY]"
    WEBHOOK_HEALTH = "[WEBHOOK:HEALTH]"
    WEBHOOK_RECOVERY = "[WEBHOOK:RECOVERY]"
    WEBHOOK_KEEPALIVE = "[WEBHOOK:KEEPALIVE]"

    # Processing flows
    PROCESS = "[PROCESS]"
    PROCESS_ROW = "[PROCESS:ROW]"
    PROCESS_IMAGE = "[PROCESS:IMAGE]"
    PROCESS_UPLOAD = "[PROCESS:UPLOAD]"

    # External services
    SMARTSHEET = "[SMARTSHEET]"
    SUPABASE = "[SUPABASE]"

    # System events
    STARTUP = "[STARTUP]"
    SHUTDOWN = "[SHUTDOWN]"
    ERROR = "[ERROR]"
    RETRY = "[RETRY]"


# =============================================================================
# Logging Context
# =============================================================================

@dataclass
class LogContext:
    """
    Context information to include in log messages.

    Usage:
        with log_context(org_id="abc", worksheet_id=123):
            log.info("Processing sheet")  # Will include context
    """
    request_id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])
    org_id: Optional[str] = None
    worksheet_id: Optional[int] = None
    webhook_id: Optional[int] = None
    config_id: Optional[str] = None
    row_id: Optional[int] = None

    def to_prefix(self) -> str:
        """Convert context to a log prefix string."""
        parts = []
        if self.org_id:
            parts.append(f"org={self.org_id[:8]}")
        if self.worksheet_id:
            parts.append(f"sheet={self.worksheet_id}")
        if self.webhook_id:
            parts.append(f"wh={self.webhook_id}")
        if self.row_id:
            parts.append(f"row={self.row_id}")
        if self.config_id:
            parts.append(f"cfg={self.config_id[:8]}")

        if parts:
            return f"[{' '.join(parts)}] "
        return ""


def get_current_context() -> Optional[LogContext]:
    """Get the current logging context."""
    return _context_var.get()


@contextmanager
def log_context(**kwargs):
    """
    Context manager for adding context to log messages.

    Usage:
        with log_context(org_id="abc-123", worksheet_id=456):
            log.info("Processing")  # Logs: [org=abc-123 sheet=456] Processing
    """
    old_context = get_current_context()

    # Merge with existing context if present
    if old_context:
        new_kwargs = {
            'request_id': old_context.request_id,
            'org_id': kwargs.get('org_id', old_context.org_id),
            'worksheet_id': kwargs.get('worksheet_id', old_context.worksheet_id),
            'webhook_id': kwargs.get('webhook_id', old_context.webhook_id),
            'config_id': kwargs.get('config_id', old_context.config_id),
            'row_id': kwargs.get('row_id', old_context.row_id),
        }
        ctx = LogContext(**new_kwargs)
    else:
        ctx = LogContext(**kwargs)

    token = _context_var.set(ctx)
    try:
        yield ctx
    finally:
        _context_var.reset(token)


# =============================================================================
# Context-Aware Logger
# =============================================================================

class ContextLogger(logging.Logger):
    """
    Custom logger that automatically includes context in messages.
    """

    def _log_with_context(
        self,
        level: int,
        msg: str,
        args: tuple,
        exc_info=None,
        extra=None,
        stack_info=False,
        stacklevel=1,
    ):
        """Add context prefix to log message."""
        ctx = get_current_context()
        if ctx:
            msg = f"{ctx.to_prefix()}{msg}"

        super()._log(
            level, msg, args,
            exc_info=exc_info,
            extra=extra,
            stack_info=stack_info,
            stacklevel=stacklevel + 1,
        )

    def debug(self, msg, *args, **kwargs):
        if self.isEnabledFor(logging.DEBUG):
            self._log_with_context(logging.DEBUG, msg, args, **kwargs)

    def info(self, msg, *args, **kwargs):
        if self.isEnabledFor(logging.INFO):
            self._log_with_context(logging.INFO, msg, args, **kwargs)

    def warning(self, msg, *args, **kwargs):
        if self.isEnabledFor(logging.WARNING):
            self._log_with_context(logging.WARNING, msg, args, **kwargs)

    def error(self, msg, *args, **kwargs):
        if self.isEnabledFor(logging.ERROR):
            self._log_with_context(logging.ERROR, msg, args, **kwargs)

    def critical(self, msg, *args, **kwargs):
        if self.isEnabledFor(logging.CRITICAL):
            self._log_with_context(logging.CRITICAL, msg, args, **kwargs)


# Register custom logger class
logging.setLoggerClass(ContextLogger)


# =============================================================================
# Timing Utilities
# =============================================================================

@contextmanager
def log_timing(logger: logging.Logger, operation: str, level: int = logging.DEBUG):
    """
    Context manager to log operation duration.

    Usage:
        with log_timing(log, "fetch_row"):
            row = api.get_row(id)
        # Logs: "fetch_row completed in 245ms"
    """
    start = time.perf_counter()
    try:
        yield
    finally:
        elapsed_ms = (time.perf_counter() - start) * 1000
        logger.log(level, f"{operation} completed in {elapsed_ms:.0f}ms")


def log_duration(operation: str, level: int = logging.DEBUG):
    """
    Decorator to log function duration.

    Usage:
        @log_duration("process_image")
        def process_image(data):
            ...
    """
    def decorator(func: Callable):
        @wraps(func)
        def wrapper(*args, **kwargs):
            logger = logging.getLogger(func.__module__)
            start = time.perf_counter()
            try:
                result = func(*args, **kwargs)
                elapsed_ms = (time.perf_counter() - start) * 1000
                logger.log(level, f"{operation} completed in {elapsed_ms:.0f}ms")
                return result
            except Exception as e:
                elapsed_ms = (time.perf_counter() - start) * 1000
                logger.error(f"{operation} failed after {elapsed_ms:.0f}ms: {e}")
                raise
        return wrapper
    return decorator


# =============================================================================
# Flow Logging Helpers
# =============================================================================

def _safe_context_value(key: str, value: Any) -> Any:
    """Redact sensitive context values for logging."""
    from src.utils.safe_log import create_safe_log_data
    safe = create_safe_log_data({key: value})
    return safe.get(key, value)


class FlowLogger:
    """
    Helper for logging multi-step operations with consistent formatting.

    Context and extra values are redacted for PII/sensitive data.
    """

    def __init__(
        self,
        logger: logging.Logger,
        flow_name: str,
        **context,
    ):
        self.logger = logger
        self.flow_name = flow_name
        self.context = context
        self.start_time = None
        self.step_count = 0

    def _format_context(self) -> str:
        """Format context for log message (sensitive values redacted)."""
        if not self.context:
            return ""
        parts = [
            f"{k}={_safe_context_value(k, v)}"
            for k, v in self.context.items()
            if v is not None
        ]
        return f"({', '.join(parts)}) " if parts else ""

    def start(self, message: str) -> None:
        """Log flow start."""
        self.start_time = time.perf_counter()
        self.step_count = 0
        ctx = self._format_context()
        self.logger.info(f"[{self.flow_name}] {ctx}START: {message}")

    def step(self, num: int, message: str) -> None:
        """Log a step in the flow."""
        self.step_count = num
        ctx = self._format_context()
        self.logger.info(f"[{self.flow_name}] {ctx}STEP {num}: {message}")

    def success(self, message: str, **extra_context) -> None:
        """Log successful completion (extra_context redacted)."""
        elapsed = ""
        if self.start_time:
            elapsed_ms = (time.perf_counter() - self.start_time) * 1000
            elapsed = f" [{elapsed_ms:.0f}ms]"

        ctx = self._format_context()
        extra = ""
        if extra_context:
            safe_extra = [
                f"{k}={_safe_context_value(k, v)}" for k, v in extra_context.items()
            ]
            extra = " " + ", ".join(safe_extra)

        self.logger.info(f"[{self.flow_name}] {ctx}OK: {message}{extra}{elapsed}")

    def failure(self, message: str, error: Optional[Exception] = None) -> None:
        """Log failure (exception sanitized to avoid leaking secrets)."""
        from src.utils.safe_log import safe_exception_message

        elapsed = ""
        if self.start_time:
            elapsed_ms = (time.perf_counter() - self.start_time) * 1000
            elapsed = f" [{elapsed_ms:.0f}ms]"

        ctx = self._format_context()
        err_msg = f": {safe_exception_message(error)}" if error else ""

        self.logger.error(f"[{self.flow_name}] {ctx}FAILED: {message}{err_msg}{elapsed}")

    def warning(self, message: str) -> None:
        """Log a warning during the flow."""
        ctx = self._format_context()
        self.logger.warning(f"[{self.flow_name}] {ctx}WARN: {message}")

    def debug(self, message: str) -> None:
        """Log debug info during the flow."""
        ctx = self._format_context()
        self.logger.debug(f"[{self.flow_name}] {ctx}{message}")


# =============================================================================
# Setup Functions
# =============================================================================

def setup_logging(
    level: str = "INFO",
    format_string: Optional[str] = None,
    date_format: str = DATE_FORMAT,
    use_debug_format: bool = False,
    json_format: bool = False,
) -> logging.Logger:
    """
    Configure application logging.

    Args:
        level: Log level (DEBUG, INFO, WARNING, ERROR)
        format_string: Optional custom format string
        date_format: Date format for log timestamps
        use_debug_format: Use detailed format with file/line info

    Returns:
        Root logger instance
    """
    if format_string is None:
        if json_format:
            format_string = STRUCTURED_FORMAT
        elif use_debug_format:
            format_string = DEBUG_FORMAT
        else:
            format_string = STANDARD_FORMAT

    # Get root logger
    root_logger = logging.getLogger()
    root_logger.setLevel(getattr(logging, level.upper()))

    # Clear existing handlers
    root_logger.handlers.clear()

    # Create console handler
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(getattr(logging, level.upper()))

    # Create formatter
    formatter = logging.Formatter(fmt=format_string, datefmt=date_format)
    console_handler.setFormatter(formatter)

    # Add handler
    root_logger.addHandler(console_handler)

    # Reduce noise from third-party libraries
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("PIL").setLevel(logging.WARNING)
    logging.getLogger("asyncio").setLevel(logging.WARNING)

    return root_logger


def get_logger(name: str) -> logging.Logger:
    """
    Get a logger with the given name.

    Args:
        name: Logger name (typically __name__)

    Returns:
        Logger instance
    """
    return logging.getLogger(name)


class LoggerMixin:
    """
    Mixin class that provides a logger property.

    Example:
        class MyService(LoggerMixin):
            def do_something(self):
                self.logger.info("Doing something...")
    """

    @property
    def logger(self) -> logging.Logger:
        """Get logger for this class."""
        return logging.getLogger(self.__class__.__name__)


# =============================================================================
# Convenience Functions
# =============================================================================

def log_separator(
    logger: logging.Logger,
    title: str = "",
    char: str = "=",
    width: int = 70,
    level: int = logging.INFO,
) -> None:
    """
    Log a visual separator line.

    Usage:
        log_separator(log, "WEBHOOK HEALTH CHECK")
        # Logs: ==================== WEBHOOK HEALTH CHECK ====================
    """
    if title:
        padding = (width - len(title) - 2) // 2
        line = f"{char * padding} {title} {char * padding}"
    else:
        line = char * width

    logger.log(level, line)


def log_summary(
    logger: logging.Logger,
    title: str,
    stats: Dict[str, Any],
    level: int = logging.INFO,
) -> None:
    """
    Log a summary block with statistics.

    Usage:
        log_summary(log, "Processing Complete", {
            "rows_processed": 10,
            "images_uploaded": 15,
            "errors": 0,
        })
    """
    log_separator(logger, title, level=level)
    for key, value in stats.items():
        logger.log(level, f"  {key}: {value}")
    log_separator(logger, "", level=level)
