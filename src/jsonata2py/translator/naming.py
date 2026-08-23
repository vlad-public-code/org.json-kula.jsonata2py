"""Naming conventions shared across the translator's code-gen files.

Per docs/porting-design-spec.md D3: JSONata variable $name maps to Python
local v_name. Compiler-internal identifiers get a single leading
underscore (_ctx, _root, _const0, _block0, _el0, ...) -- generated code
never uses a leading double-underscore, so no name is at risk of Python's
class-body name mangling even though generated code is module-level
functions (not methods), where mangling would not apply anyway.
"""

from __future__ import annotations


def pyvar(name: str) -> str:
    """Maps a JSONata variable name (without the leading $) to its Python
    local variable name."""
    return f"v_{name}"


def pyvar_ref(name: str) -> str:
    """The holder-array box variable for a self-referential/forward-referenced
    binding: v_nameRef[0] (Java) -> _ref_name[0] (Python).

    The prefix must live in the compiler-internal (single leading
    underscore) namespace, which pyvar() can never emit. Naming it
    v_{name}_ref instead would be exactly pyvar("{name}_ref"), so the user
    expression ($f := function($x){...$f...}; $f_ref := 99; $f(0)) would
    clobber the holder box of $f with the value of $f_ref."""
    return f"_ref_{name}"


def py_string(s: str) -> str:
    """A Python string literal for s. repr() of a str is always a valid
    Python literal. Alias of module_assembler.python_string, re-exported
    here since most call sites already import from naming."""
    return repr(s)
