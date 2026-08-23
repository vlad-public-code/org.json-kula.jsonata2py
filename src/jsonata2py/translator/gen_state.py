"""Mutable state shared across all GenCtx instances in one translation.

Ported from org.json_kula.jsonata_jvm.translator.GenState.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..parser.ast_nodes import AstNode


class GenState:
    def __init__(self) -> None:
        self.counter = 0
        # Memoises scope_analyzer.contains_parent_step by id(node) for the
        # lifetime of this translation -- the translator calls it repeatedly
        # on overlapping subtrees from several call sites. Never module-
        # level: id() is reused once a node is freed.
        self._contains_parent_step_cache: dict[int, bool] = {}
        # Buffer of complete top-level `def ...` blocks, appended as they're
        # generated (mirrors Java's helperMethods StringBuilder).
        self.helper_defs: list[str] = []
        # Local declaration lines emitted before the body expression inside
        # _evaluate (e.g. counter-list declarations for global #$pos counters).
        self.local_declarations: list[str] = []

        # Literal values hoisted to module-level constants, keyed by the
        # Python expression that builds them. Per D8.4/6.4: only containers
        # (dict/list literals) are worth hoisting in Python -- scalars are
        # already interned in the code object -- but object-constructor key
        # tuples are still hoisted here too.
        self._constants: dict[str, str] = {}
        self._key_arrays: dict[str, str] = {}

        # Stack of locally-bound variable name sets, one entry per active
        # scope (block or lambda body). isLocal() decides whether a
        # VariableRef should resolve to a Python local variable or to a
        # runtime binding lookup.
        self.scope_stack: list[set[str]] = []

        # Variables that use an array-holder pattern for recursive
        # self-reference. When name is in this set, visit_variable_ref emits
        # v_nameRef[0] instead of v_name.
        self.holder_vars: set[str] = set()

        # Per-scope alias maps: when a multi-param lambda uses id-suffixed
        # Python names to avoid shadowing, the canonical JSONata name maps
        # to its Python alias. Mirrors scope_stack -- pushed/popped together.
        self._alias_stack: list[dict[str, str]] = []

        # Set to a variable name while generating a PartialApplication body.
        self.partial_ph_var: str | None = None
        self.partial_ph_need_idx = False
        self.partial_ph_idx = 0

    def next_id(self) -> int:
        i = self.counter
        self.counter += 1
        return i

    def contains_parent_step(self, node: AstNode | None) -> bool:
        """Memoised scope_analyzer.contains_parent_step -- prefer this over
        calling the module function directly from translator code."""
        from . import scope_analyzer

        return scope_analyzer.contains_parent_step(node, self._contains_parent_step_cache)

    # -------------------------------------------------------------------------
    # Constant / key-array hoisting
    # -------------------------------------------------------------------------

    def constant(self, python_expression: str) -> str:
        """Returns the module-level name holding python_expression, creating
        it if needed."""
        name = self._constants.get(python_expression)
        if name is None:
            name = f"_const{len(self._constants)}"
            self._constants[python_expression] = name
        return name

    def key_array(self, python_initialiser: str) -> str:
        """Returns the module-level name holding the given tuple/list
        initialiser."""
        name = self._key_arrays.get(python_initialiser)
        if name is None:
            name = f"_keys{len(self._key_arrays)}"
            self._key_arrays[python_initialiser] = name
        return name

    def constant_declarations(self) -> str:
        """Emits the module-level assignment lines for every hoisted literal
        and key array."""
        if not self._constants and not self._key_arrays:
            return ""
        lines = []
        for expression, name in self._constants.items():
            lines.append(f"{name} = {expression}")
        for initialiser, name in self._key_arrays.items():
            lines.append(f"{name} = {initialiser}")
        return "\n".join(lines) + "\n"

    # -------------------------------------------------------------------------
    # Helper `def` generation (Python lambdas can't contain statements)
    # -------------------------------------------------------------------------

    def new_helper_def(self, name_prefix: str, params: list[str], body_lines: list[str]) -> str:
        """Appends a top-level `def name(params): body_lines` to
        helper_defs and returns the function name. body_lines must already
        end with a return statement (or raise)."""
        name = f"_{name_prefix}{self.next_id()}"
        header = f"def {name}({', '.join(params)}):"
        indented = "\n".join(f"    {line}" for line in body_lines)
        self.helper_defs.append(f"{header}\n{indented}\n")
        return name

    # -------------------------------------------------------------------------
    # Scope management
    # -------------------------------------------------------------------------

    def push_scope(self) -> None:
        self.scope_stack.append(set())
        self._alias_stack.append({})

    def pop_scope(self) -> None:
        if self.scope_stack:
            self.scope_stack.pop()
            self._alias_stack.pop()

    def add_local_var(self, name: str) -> None:
        if self.scope_stack:
            self.scope_stack[-1].add(name)

    def add_local_var_with_alias(self, name: str, python_name: str) -> None:
        if self.scope_stack:
            self.scope_stack[-1].add(name)
            self._alias_stack[-1][name] = python_name

    def is_local(self, name: str) -> bool:
        return any(name in scope for scope in self.scope_stack)

    def get_alias(self, name: str) -> str | None:
        """Returns the Python alias for name in the innermost scope that
        defines name, or None if no alias exists in that scope. Searches
        scope_stack/alias_stack from innermost (end of list) to outermost,
        stopping at the defining scope."""
        for scope, aliases in zip(reversed(self.scope_stack), reversed(self._alias_stack), strict=True):
            if name in scope:
                return aliases.get(name)
        return None
