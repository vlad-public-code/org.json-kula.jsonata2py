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

# Lives in signature.py because it is signature-directed argument validation,
# not a runtime primitive; core.py re-exports nothing else from that module.
from .signature import call_builtin_ctx, sig_a, sig_t

# Tuple-mode path evaluation (`%`, `@$v`, `#$v`). Flat like the rest of the
# namespace, because generated code calls every runtime function bare.
from .tuples import *  # noqa: F403
from .tuples import __all__ as _tuples_all

__all__ = [*_core_all, "call_builtin_ctx", "sig_a", "sig_t", *_tuples_all]
