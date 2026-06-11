import logging
import json
import traceback
import re
from datetime import date, datetime, time
from decimal import Decimal
from uuid import UUID

# Compile a robust case-insensitive regex pattern for sensitive credentials and keys to redact from logs
SENSITIVE_KEYS_PATTERN = re.compile(
    r"password|token|secret|key|authorization|cookie|cvv|card|ssn",
    re.IGNORECASE
)

class SafeJSONEncoder(json.JSONEncoder):
    """
    A custom JSON encoder that handles non-serializable objects gracefully
    without raising TypeError, ensuring log calls never crash the application.
    """
    def default(self, obj):
        try:
            if isinstance(obj, (datetime, date, time)):
                return obj.isoformat()
            if isinstance(obj, UUID):
                return str(obj)
            if isinstance(obj, Decimal):
                return float(obj)
            if isinstance(obj, set):
                return list(obj)
            if hasattr(obj, "__dict__"):
                return {k: self.default(v) for k, v in obj.__dict__.items() if not k.startswith("_")}
            return str(obj)
        except Exception:
            return f"<unserializable: {type(obj).__name__}>"

class JsonFormatter(logging.Formatter):
    # Standard Python LogRecord attributes to ignore when looking for custom "extra" data
    RESERVED_FIELDS = {
        "args", "asctime", "created", "exc_info", "exc_text", "filename",
        "funcName", "levelname", "levelno", "lineno", "module", "msecs",
        "message", "msg", "name", "pathname", "process", "processName",
        "relativeCreated", "stack_info", "thread", "threadName"
    }

    def format(self, record):
        # 1. Base log structure
        log_data = {
            "time": f"{self.formatTime(record, self.datefmt)}.{int(record.msecs):03d}",
            "level": record.levelname,
            "message": record.getMessage(),
            "module": record.module,
            "pathname": record.pathname,
            "funcName": record.funcName,
            "line": record.lineno,
            "logger": record.name
        }

        # 2. Add exceptions details if present
        if record.exc_info:
            log_data["trace"] = "".join(traceback.format_exception(*record.exc_info))
            log_data["trace_type"] = type(record.exc_info[1]).__name__ if record.exc_info and record.exc_info[1] else "N/A"

        # 3. Automatically extract and merge any custom 'extra' dictionary variables
        for key, value in record.__dict__.items():
            if key not in self.RESERVED_FIELDS:
                log_data[key] = value

        # 3b. Automatically merge thread/async-safe request log context variables
        from utils.logging.context import get_log_context
        context_data = get_log_context()
        for key, value in context_data.items():
            if key not in log_data:
                log_data[key] = value

        # 4. Recursively sanitize sensitive keys to prevent PII or credential leakage
        sanitized_log_data = self._sanitize_data(log_data)

        # 5. Safe JSON dump
        try:
            return json.dumps(sanitized_log_data, cls=SafeJSONEncoder)
        except Exception as e:
            # Absolute fallback if somehow the custom encoder still failed
            return json.dumps({
                "time": log_data.get("time"),
                "level": "ERROR",
                "logger": "utils.logging.JSONFormatter",
                "message": f"Log serialization failed: {str(e)}",
                "original_message": str(log_data.get("message"))
            })

    def _sanitize_data(self, data):
        """Recursively sanitize keys matching sensitive patterns"""
        if isinstance(data, dict):
            sanitized = {}
            for key, value in data.items():
                if SENSITIVE_KEYS_PATTERN.search(key):
                    sanitized[key] = "***REDACTED***"
                else:
                    sanitized[key] = self._sanitize_data(value)
            return sanitized
        elif isinstance(data, list):
            return [self._sanitize_data(item) for item in data]
        elif isinstance(data, tuple):
            return tuple(self._sanitize_data(item) for item in data)
        elif isinstance(data, set):
            return {self._sanitize_data(item) for item in data}
        else:
            return data