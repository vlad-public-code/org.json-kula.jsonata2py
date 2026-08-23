"""The JSONata value model: native Python values plus three library-owned
sentinels/wrappers.

Ported design (see docs/porting-design-spec.md D1, D6): every JSONata
value is one of dict, list, str, int, float, bool, None, MISSING,
JLambda, or JRegex. None is JSON null; MISSING is JSONata "undefined".

Traps (D1) -- never inline these checks, always go through the helpers
in this module:
  - bool is a subclass of int in Python, so a number test must exclude
    bool explicitly.
  - True == 1 and False == 0 under Python's default equality; JSONata
    deep-equals must compare *kind* before value.
  - None (JSON null) and MISSING (absence) are both falsy in Python's
    `if v:` sense but mean different things; never branch on truthiness
    to distinguish them.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

UNKNOWN_ARITY = -1


class _Missing:
    """Singleton sentinel for JSONata's "undefined" -- the absence of a
    value. Distinct from JSON null, which is represented by None."""

    __slots__ = ()
    _instance: _Missing | None = None

    def __new__(cls) -> _Missing:
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __bool__(self) -> bool:
        return False

    def __repr__(self) -> str:
        return "MISSING"


MISSING = _Missing()


def is_number(v: object) -> bool:
    """True if v is a JSONata number. bool is a subclass of int in Python,
    so it must be excluded explicitly -- never inline this check."""
    t = type(v)
    if t is int or t is float:
        return True
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def is_missing(v: object) -> bool:
    return v is MISSING


def is_null(v: object) -> bool:
    return v is None


@dataclass(frozen=True, slots=True)
class JLambda:
    """A JSONata function value.

    Because the value model is native Python types, a JLambda instance
    can never be confused with input data -- no input document can
    contain one. is_function(v) is isinstance(v, JLambda). $string of a
    function is "" and $type is "function".

    Attributes:
        fn: the underlying callable
        arity: the number of parameters the callback declares, or
            UNKNOWN_ARITY if not statically known. Used by higher-order
            built-ins to decide how much to pass a callback.
    """

    fn: Callable[..., Any]
    arity: int = UNKNOWN_ARITY


def is_function(v: object) -> bool:
    return isinstance(v, JLambda)


@dataclass(frozen=True, slots=True)
class JRegex:
    """A JSONata regex value (the result of evaluating a /pattern/flags
    literal as a value, e.g. bound to a variable and passed to $match)."""

    pattern: str
    flags: str
    compiled: Any = field(compare=False, default=None)


def is_regex(v: object) -> bool:
    return isinstance(v, JRegex)


class Preserved(list[Any]):
    """Marks a nested array as "do not flatten" during array-constructor
    flattening.

    A list subclass rather than a wrapper so every read path (len,
    iteration, indexing, isinstance(x, list)) keeps working untouched --
    only the flattening code checks for the marker.
    """

    __slots__ = ()


def is_value(v: object) -> bool:
    """True for any value JSONata considers a "real" scalar/collection
    value (not MISSING)."""
    return v is not MISSING
