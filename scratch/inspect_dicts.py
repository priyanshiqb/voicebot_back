from _weakrefset import WeakSet
from tempfile import SpooledTemporaryFile

ws = WeakSet()
class Dummy:
    pass
d = Dummy()
ws.add(d)

print("WeakSet __dict__ keys:")
print(ws.__dict__.keys())
for k, v in ws.__dict__.items():
    print(f"  {k}: {type(v)}")

print("\nSpooledTemporaryFile __dict__ keys:")
tmp = SpooledTemporaryFile()
print(tmp.__dict__.keys())
for k, v in tmp.__dict__.items():
    print(f"  {k}: {type(v)}")
