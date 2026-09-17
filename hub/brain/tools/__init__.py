"""
Importing this package registers every tool.

Adding a capability is one import line here plus the module itself -- there is
no second registry to keep in sync.
"""
from . import google, home, memory, pc, phone, timers, web  # noqa: F401

__all__ = ["google", "home", "memory", "pc", "phone", "timers", "web"]
