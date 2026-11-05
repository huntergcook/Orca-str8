"""Compatibility shim: the top manager is now the director (see director.py)."""
from .director import *  # noqa: F401,F403
from .director import Director as TopManager  # noqa: F401
