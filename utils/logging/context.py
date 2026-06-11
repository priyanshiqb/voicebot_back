import contextvars

# Create a thread-safe and async-safe ContextVar to hold global log variables
_log_context = contextvars.ContextVar("voicebot_log_context", default={})

def set_log_context(data: dict):
    """Set logging context data (creates a copy to avoid references issue)"""
    _log_context.set(dict(data))

def get_log_context() -> dict:
    """Retrieve the current logging context dict"""
    return _log_context.get()

def clear_log_context():
    """Clear all logging context variables"""
    _log_context.set({})
