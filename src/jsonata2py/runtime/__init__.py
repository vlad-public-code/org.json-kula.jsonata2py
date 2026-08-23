"""The flat namespace generated modules import from.

Load-bearing: generated code does
`from jsonata2py.runtime import *` and then calls runtime
functions bare (field(...), mul_d(...), fn_sum(...), ...). Every name the
translator can emit must be exported here via __all__ (mirrors core.py's
own __all__, which is the JsonataRuntime.java facade equivalent).
"""

from __future__ import annotations

from .core import *  # noqa: F403
from .core import __all__ as _core_all

__all__ = list(_core_all)
