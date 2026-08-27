"""Who may reach the console, and what they may do once inside.

Re-exported from ``user.permissions`` rather than redefined: the gate belongs with the
model that carries ``platform_role``, and two copies of an authorisation check is one
copy too many.
"""

from user.permissions import HasConsolePermission, IsPlatformStaff

__all__ = ["IsPlatformStaff", "HasConsolePermission"]
