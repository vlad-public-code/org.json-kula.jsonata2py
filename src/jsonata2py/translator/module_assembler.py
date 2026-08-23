"""Python source file template builder for the code generator.

Ported from org.json_kula.jsonata_jvm.translator.ClassAssembler (renamed
per docs/porting-design-spec.md D3/6.6: Python emits a module of
functions, not a class).
"""

from __future__ import annotations


def build_module(
    body_expr: str,
    helper_defs: str,
    local_declarations: str,
    source_expression: str,
    constant_declarations: str,
) -> str:
    lines = ["from jsonata2py.runtime import *", ""]
    lines.append(f"_SOURCE = {python_string(source_expression)}")
    if constant_declarations:
        lines.append(constant_declarations.rstrip("\n"))
    if helper_defs:
        lines.append(helper_defs.rstrip("\n"))
    lines.append("")
    lines.append("def _evaluate(_root, _ctx=None):")
    lines.append("    if _ctx is None:")
    lines.append("        _ctx = _root")
    if local_declarations:
        for decl_line in local_declarations.splitlines():
            lines.append(f"    {decl_line}")
    lines.append(f"    return {body_expr}")
    lines.append("")
    return "\n".join(lines)


def python_string(s: str) -> str:
    """Wraps s as a Python string literal with proper escaping. repr() of a
    str is a valid Python literal by definition."""
    return repr(s)


def one_arg(args: list[str]) -> str:
    return args[0] if args else "NULL"


def ctx_arg(args: list[str], ctx_var: str) -> str:
    """Returns the first argument expression, or ctx_var when the argument
    list is empty. Use this for built-in functions whose JSONata signature
    carries the '-' modifier (use the context value as the default
    argument)."""
    return args[0] if args else ctx_var
