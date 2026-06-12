import sys
from datetime import datetime
from decimal import Decimal
from uuid import UUID
import json
from _weakrefset import WeakSet
from tempfile import SpooledTemporaryFile

# Paste the original SafeJSONEncoder
class SafeJSONEncoderOriginal(json.JSONEncoder):
    def default(self, obj):
        try:
            if isinstance(obj, (datetime, datetime.date, datetime.time)):
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

# Paste the new SafeJSONEncoder
class SafeJSONEncoderFixed(json.JSONEncoder):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._seen = set()
        self._depth = 0
        self.MAX_DEPTH = 10

    def default(self, obj):
        try:
            if id(obj) in self._seen:
                return f"<circular_reference: {type(obj).__name__}>"
            if self._depth > self.MAX_DEPTH:
                return f"<max_depth_exceeded: {type(obj).__name__}>"
            if isinstance(obj, (datetime, datetime.date, datetime.time)):
                return obj.isoformat()
            if isinstance(obj, UUID):
                return str(obj)
            if isinstance(obj, Decimal):
                return float(obj)
            if isinstance(obj, set):
                return list(obj)
            if hasattr(obj, "__dict__"):
                self._seen.add(id(obj))
                self._depth += 1
                try:
                    return {k: self.default(v) for k, v in obj.__dict__.items() if not k.startswith("_")}
                finally:
                    self._depth -= 1
                    self._seen.remove(id(obj))
            return str(obj)
        except Exception:
            return f"<unserializable: {type(obj).__name__}>"

# Create a WeakSet
ws = WeakSet()
class Dummy:
    pass
d = Dummy()
ws.add(d)

print("Testing original encoder with WeakSet...")
try:
    res = json.dumps(ws, cls=SafeJSONEncoderOriginal)
    print("Original encoder output for WeakSet:", res)
except Exception as e:
    print("Original encoder failed with WeakSet:", type(e), e)

print("\nTesting fixed encoder with WeakSet...")
try:
    res = json.dumps(ws, cls=SafeJSONEncoderFixed)
    print("Fixed encoder output for WeakSet:", res)
except Exception as e:
    print("Fixed encoder failed with WeakSet:", type(e), e)

# Create a SpooledTemporaryFile
tmp_file = SpooledTemporaryFile(max_size=1024)
tmp_file.write(b"hello")

print("\nTesting original encoder with SpooledTemporaryFile...")
try:
    res = json.dumps(tmp_file, cls=SafeJSONEncoderOriginal)
    print("Original encoder output for SpooledTemporaryFile:", res)
except Exception as e:
    print("Original encoder failed with SpooledTemporaryFile:", type(e), e)

print("\nTesting fixed encoder with SpooledTemporaryFile...")
try:
    res = json.dumps(tmp_file, cls=SafeJSONEncoderFixed)
    print("Fixed encoder output for SpooledTemporaryFile:", res)
except Exception as e:
    print("Fixed encoder failed with SpooledTemporaryFile:", type(e), e)
