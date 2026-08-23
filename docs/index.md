---
title: jsonata2py
---

[![License](https://img.shields.io/badge/License-MIT-blue.svg)](https://github.com/vlad-public-code/org.json-kula.jsonata2py/blob/main/LICENSE)

A Python 3.11+ library that translates [JSONata](https://jsonata.org) expressions into native Python source at runtime. Each expression is parsed, optimised, and translated to Python source, which is then compiled in-memory (`compile()` + `exec()`) and returned as a ready-to-call `CompiledExpression` instance.

Port of [jsonata-jvm-compiler](https://vlad-public-code.github.io/org.json-kula.jsonata-jvm-compiler/) (Java) — same pipeline, same design, a different host runtime.

All 1,281 files of the [official JSONata test suite](https://github.com/jsonata-js/jsonata/blob/master/test/test-suite/TESTSUITE.md) pass.

---

## Requirements

| Requirement | Version |
|---|---|
| Python | 3.11+ |
| [`regex`](https://pypi.org/project/regex/) | 2023.0.0+ (Oniguruma-equivalent regex engine — used for `/pattern/flags` literals and the `$match`, `$replace`, `$split`, `$contains` functions) |

---

## Getting started

### 1. Install

```
pip install jsonata2py
```

### 2. Compile an expression

```python
import jsonata2py as jsonata

expr = jsonata.compile("Account.Order.Product.Price * 1.2")
```

`compile()` runs the full pipeline once and returns a reusable, **thread-safe** object. Compile expressions at startup and reuse them for every request — do not call `compile()` on the hot path.

`jsonata.compile()` is a module-level convenience backed by a lazily-created, process-wide `JsonataExpressionFactory`. For repeated compilation in a hot path, or anything beyond a script, construct your own factory and reuse it:

```python
factory = jsonata.JsonataExpressionFactory()
expr = factory.compile("Account.Order.Product.Price * 1.2")
exprs = factory.compile_all([
    "Account.Order.Product.Price * 1.2",
    "$sum(items.price)",
    'status = "active"',
])
```

`compile_all` exists for API parity with the Java library, where batching many expressions into one `javac` invocation was worth roughly 10x. There is no equivalent win here — Python's `compile()` builtin costs microseconds to low milliseconds per call regardless of batching — so `compile_all` is implemented as a plain loop and kept only so code written against both libraries compiles unchanged.

### 3. Evaluate against data

```python
input_data = {
    "Account": {
        "Order": {
            "Product": {"Price": 50.0}
        }
    }
}

result = expr.evaluate(input_data)  # -> 60.0
```

`evaluate()` accepts and returns plain Python values: `dict`, `list`, `str`, `int`, `float`, `bool`, `None` (JSON null), or `jsonata.MISSING` (JSONata's `undefined` — never conflated with `None`). The same `CompiledExpression` instance can be evaluated concurrently from multiple threads.

---

## Exception types

| Exception | When raised |
|---|---|
| `JsonataCompilationError` | `compile()` — the expression is syntactically invalid or (rarely) the generated code fails to compile |
| `JsonataEvaluationError` | `evaluate()` — the expression cannot be applied to the given input (type mismatch, division by zero, etc.) |

```python
try:
    expr = factory.compile(expression)
    result = expr.evaluate(data)
except jsonata.JsonataCompilationError as e:
    ...  # bad expression -- e.error_code, e.message, e.__cause__ carries a ParseError with source position
except jsonata.JsonataEvaluationError as e:
    ...  # bad input or runtime error -- e.error_code is a JSONata error code like "T2001"
```

---

## JSONata language features

The library implements all JSONata language features, functions as first-class values included: a
function can be stored in a variable, put in an array or object, passed to and returned from another
function, and carried across the binding boundary in either direction — see
[Functions as values](#functions-as-values).

## Bindings

Bindings let you inject named values and Python functions into an expression at runtime. Inside the expression they are referenced as `$name` (values) or called as `$name(args...)` (functions).

### Per-evaluation bindings

Pass a `JsonataBindings` instance as the second argument to `evaluate()` to supply values or functions for a single call:

```python
expr = factory.compile("$taxRate * subtotal")

bindings = jsonata.JsonataBindings().bind_value("taxRate", 0.2)

result = expr.evaluate({"subtotal": 500}, bindings)  # -> 100.0
```

Per-evaluation bindings are not stored on the expression instance and do not affect other calls.

### Permanent bindings

Use `assign()` and `register_function()` to attach bindings permanently to an expression instance. They apply to every subsequent `evaluate()` call.

```python
expr = factory.compile("$round2($taxRate * subtotal)")

# Permanent value
expr.assign("taxRate", 0.2)

# Permanent function
@jsonata.bound_function("<n:n>")
def round2(n: float) -> float:
    return round(n, 2)

expr.register_function("round2", round2)

r1 = expr.evaluate({"subtotal": 100})  # -> 20.0
r2 = expr.evaluate({"subtotal": 333})  # -> 66.6
```

Permanent bindings are isolated per instance — assigning to one `CompiledExpression` does not affect any other.

### Precedence

When both a permanent binding and a per-evaluation binding exist for the same name, the **per-evaluation binding wins**.

### Functions as values

A bound function is not only callable — `$name` on its own is a **function value**, so it can be
passed to a higher-order built-in, piped through `~>`, or handed to another bound function:

```python
bindings = jsonata.JsonataBindings().bind_function("double", doubler)

factory.compile("$map([1,2,3], $double)").evaluate(data, bindings)  # -> [2, 4, 6]
factory.compile("5 ~> $double").evaluate(data, bindings)            # -> 10
factory.compile("$type($double)").evaluate(data, bindings)          # -> "function"
```

This is what makes a [library](#jsonata-libraries) export usable as an argument as well as a call
target, since exports are supplied through `register_function`.

The reverse also holds: a function *value* can be bound with `bind_value` and called by name.
`jsonata2py.runtime.lambdas.lambda_node` builds one from a Python callable, with the
number of parameters it takes:

```python
from jsonata2py.runtime.lambdas import lambda_node

times_ten = lambda_node(lambda x: x * 10, 1)

bindings = jsonata.JsonataBindings().bind_value("f", times_ten)

factory.compile("$f(3)").evaluate(data, bindings)          # -> 30
factory.compile("$map([1,2], $f)").evaluate(data, bindings)  # -> [10, 20]
```

Both maps are consulted, and the one that matches the position wins: `$name` in value position
prefers a value binding, `$name(...)` at a call site prefers a function binding.

**Arity.** How many arguments reach a bound function used as a value is decided by its declared
signature — `<nn:b>` makes a two-argument function, so `$sort([2,3,1], $desc)` receives a comparator
pair and `$map` supplies the index. A signature that does not pin the arity down (absent,
unparseable, or variadic) yields a one-argument function value. This is the same limitation
hand-written JSONata lambdas have: a packed argument tuple is an array, and so is a single array
argument. Declare a fixed arity to receive several arguments.

### Implementing a bound function

Two ways to bind a Python function:

**The `bound_function` decorator** — plain positional arguments, the signature declares the arity:

```python
@jsonata.bound_function("<n:n>")
def round2(n: float) -> float:
    return round(n, 2)
```

**A `JsonataBoundFunction`-shaped object** — for cases needing access to the raw `JsonataFunctionArguments` (out-of-range access returns `jsonata.MISSING` rather than raising):

```python
class Adder:
    def get_function_signature(self) -> str | None:
        return "<nn:n>"

    def apply(self, args: jsonata.JsonataFunctionArguments):
        return args.get(0) + args.get(1)
```

Either form may raise `JsonataEvaluationError` from `apply`/the function body.

### Function signature syntax

The signature has the form `<params:return>` where `params` is a sequence of type symbols and `return` is a single type symbol.

**Simple types**

| Symbol | Type |
|---|---|
| `b` | Boolean |
| `n` | number |
| `s` | string |
| `l` | null |

**Complex types**

| Symbol | Type |
|---|---|
| `a` | array |
| `o` | object |
| `f` | function |
| `j` | any JSON type — equivalent to `(bnsloa)` |
| `u` | Boolean, number, string, or null — equivalent to `(bnsl)` |
| `x` | any type at all, functions included — equivalent to `(bnsloaf)` |
| `(sao)` | union: string, array, or object |

**Parametrised types**: `a<s>` (array of strings), `a<x>` (array of any type), `f<n:n>` (a function
from number to number). A parametrised `f` requires a function, but the argument function's own
parameter and return types are not checked — jsonata-js does not check them either.

An argument declared `f` that is not a function is rejected with `T0410`. Note that `j` is documented
by the JSONata spec as *excluding* functions but does not reject one here; declare `f` when you
require a function.

**Option modifiers** appended to a type symbol:

| Modifier | Meaning |
|---|---|
| `+` | One or more arguments of this type (variadic) |
| `?` | Optional argument |
| `-` | Use the context value ("focus") if the argument is missing |

Example: `$length` has signature `<s-:n>` — accepts a string (using context as focus if omitted) and returns a number.

---

## JSONata libraries

The bindings above are written in Python: a bound function per function, an `assign` per value, repeated for every expression that needs them. A **library** is the same set of bindings written in JSONata instead — once, in one file — and applied to any expression that needs it.

A library is nothing more than a **definition expression**: ordinary JSONata that binds names and returns the names to export.

```
(
  $vatRate := 0.2;
  $round2  := function($n){ $round($n, 2) };
  $gross   := function($net){ $round2($net * (1 + $vatRate)) };
  $format  := function($n){ "£" & $string($round2($n)) };

  ["gross", "format", "vatRate"]
)
```

That is a complete, valid JSONata expression. Evaluate it in any JSONata engine and it returns `["gross", "format", "vatRate"]` — the export list *is* the expression's result, not a parameter passed from Python. So a definition file can be linted, tested and run by tools that know nothing about this library, and it states its own interface: nothing outside it decides what it provides.

```python
billing = factory.compile_library(definition)

billing.functions   # dict[str, JsonataBoundFunction] -- gross, format
billing.constants    # dict[str, Any]                  -- vatRate
```

Each exported name lands in one dict or the other according to **what it evaluated to** — the definition never says which is which. Names it binds but does not export (`$round2` here) stay private, while remaining reachable from the exported functions.

### Providing bindings from a library

`use_library` applies a whole library, functions and constants together, so the caller never has to
know which name is which. On the expression it is permanent, for the lifetime of that instance:

```python
invoice = factory.compile("lines.$gross(amount) ~> $sum() ~> $format()")

invoice.use_library(billing)
```

or per evaluation, when different calls need different libraries:

```python
bindings = jsonata.JsonataBindings().use_library(billing)

invoice.evaluate(data, bindings)
```

It returns the same `JsonataBindings`, so libraries and one-off bindings compose in a single
expression:

```python
bindings = (
    jsonata.JsonataBindings()
    .use_library(billing)
    .use_library(formatting)
    .bind_value("today", today)
)
```

Applying two libraries that export the same name leaves the later one in place, exactly as re-binding
a name always does.

Either way the expression sees `$gross(...)`, `$format(...)` and `$vatRate` exactly as if they had been written in Python — the precedence rules above apply unchanged, so a per-evaluation binding still wins over a library one registered permanently.

Applying a library to every expression in an application is one line each:

```python
for expr in factory.compile_all(expressions):
    expr.use_library(billing)
```

### What a definition can contain

Anything JSONata can express. Exported functions may be recursive, mutually recursive, closures over private helpers, `λ`-notation, functions returned by other functions, `~>` chains, or partial applications:

```
(
  $pi := 3.1415926535897932384626;

  /* private helpers — not exported, still reachable */
  $product   := function($a, $b) { $a * $b };
  $factorial := function($n) { $n = 0 ? 1 : $reduce([1..$n], $product) };

  $sin := function($x){ $cos($x - $pi/2) };
  $cos := function($x){
    $x > $pi ? $cos($x - 2 * $pi) : $x < -$pi ? $cos($x + 2 * $pi) :
      $sum([0..12].($power(-1, $) * $power($x, 2*$) / $factorial(2*$)))
  };

  ["sin", "cos", "pi"]
)
```

Constants are values, not expressions: the definition runs **once**, when the library is compiled, so `$total := $sum([1..10])` exports the number `55`. Functions, by contrast, run whenever they are called.

The export list is itself an expression — `["sin", "cos"]` is the usual form, a single `"sin"` works, and so does a list computed at definition time. A definition that forgets its export list ends on its last binding and therefore returns a *function*; that is rejected with `must return an array of function names`.

Exported functions can also be called straight from Python, with no expression involved:

```python
gross = billing.functions["gross"]
result = gross.apply(jsonata.JsonataFunctionArguments([100]))
```

### Signatures

Each exported function reports a JSONata signature:

| Definition | Reported signature |
|---|---|
| `$twice := function($x)<n:n>{ $x * 2 }` | `<n:n>` — the declared one |
| `$volume := function($l, $w, $h){ ... }` | `<j?j?j?:j>` — synthesised, all-optional |
| `$normalize := $uppercase ~> $trim` | none — arity known only at call time |

The synthesised form is deliberately permissive: JSONata lets a lambda be called with fewer arguments than it declares (the rest are *undefined*), and `j` applies no coercion — so an exported function accepts exactly what the same function accepts inside JSONata. Ask for something stricter with a signature override:

```python
lib = factory.compile_library(
    definition,
    jsonata.JsonataLibraryOptions().with_signature("$gross", "<n:n>"),
)

# "<n:n>" coerces at the boundary: $gross("100") works
```

### Lifetime and options

A library owns one generated module, so build it once at startup and keep it — the same advice as `compile()`. Exported functions are thread-safe and may be called concurrently.

`JsonataLibrary` supports the `with` statement; `close()` retires the exported functions (calling one afterwards raises `JsonataEvaluationError`), which is only worth doing when the lifetime should be explicit. Constants keep working — they are ordinary values. Letting the library become unreachable releases everything.

`JsonataLibraryOptions` also carries the document the definition is evaluated against (`with_input`, for a definition that reads from data) and the bindings visible while it runs (`with_bindings`).

### A definition must be self-contained

Every name a definition uses has to come from somewhere it controls: a name it binds itself, a JSONata built-in, or a name handed to it at build time. Anything else is rejected when the library is compiled:

```
($withVat := function($net){ $net * (1 + $vatRate) }; ["withVat"])

-> JsonataCompilationError: The definition expression uses $vatRate, which it does not bind
   and which is not a JSONata built-in. Bind it in the definition, or supply it through
   JsonataLibraryOptions.bindings.
```

The alternative — resolving `$vatRate` against whatever happens to be bound where `$withVat` is *called* — would make a library's behaviour depend on its caller, and would make a typo (`$rat` for `$rate`) indistinguishable from a deliberate hook. Failing at build time names both the problem and the fix.

To parameterise a library, supply the values when you build it:

```python
lib = factory.compile_library(
    definition,
    jsonata.JsonataLibraryOptions().with_bindings(
        jsonata.JsonataBindings().bind_value("vatRate", rate)
    ),
)
```

Those names are then in scope for the definition, and are captured by the functions it exports.

Lambda parameters, bindings inside nested blocks, forward references between siblings (mutual recursion), and path bindings (`@$v`, `#$i`) all count as bound — only genuinely unresolvable names are reported.

One further semantic worth knowing: **the caller's evaluation is reused.** Called from inside an expression, an exported function shares that evaluation's recursion budget (100 nested calls) and its `set_timeout` deadline.

---

## Advanced usage

### Evaluation timeout

Call `set_timeout(timeout_ms)` on an expression instance to cap how long a single `evaluate()` call may run. If the deadline is exceeded, a `JsonataEvaluationError` with error code `U1001` is raised.

```python
expr = factory.compile("...")
expr.set_timeout(500)  # 500 ms wall-clock limit per evaluate() call

try:
    result = expr.evaluate(data)
except jsonata.JsonataEvaluationError as e:
    if e.error_code == "U1001":
        ...  # evaluation exceeded 500 ms
```

Pass `0` to remove the timeout. The timeout applies to all future `evaluate()` calls on the instance; concurrent calls on the same instance each track their own independent deadline (per-evaluation state lives in a `contextvars.ContextVar`, not shared mutable state — see [Thread safety](#thread-safety)). Setting a timeout has no measurable overhead on evaluations that complete before the deadline.

### Inspecting the source expression

```python
expr = factory.compile("$sum(items.price)")
print(expr.source_jsonata)  # -> "$sum(items.price)"
```

### Accessing the generated Python source

`factory.translate()` runs the pipeline up to source generation without compiling it — useful for debugging or inspection. The output format is unstable across versions.

```python
python_source = factory.translate("price * qty")
print(python_source)
```

### Loading pre-generated Python source

If you have previously generated and saved a source string, load it directly without re-parsing:

```python
from jsonata2py.loader.loader import ExpressionLoader

loader = ExpressionLoader()
entry_point, source_jsonata = loader.load(python_source)
```

---

## Performance

jsonata2py translates each expression to Python source once, then evaluates that
compiled code many times. Compilation costs more than the alternatives; evaluation
costs less. Everything below follows from that trade.

### Measured against the other PyPI implementations

Same expression, same input document, same acceptance check — all four produce
**identical, verified-correct output**. The workload is the analytical benchmark the
Java sibling project uses: variable bindings, nested navigation, array filtering,
`$sum`, `$count`, `$average`, `$max`, `$min`, `$distinct`, string operations,
arithmetic and a conditional.

| | jsonata2py | [`jsonatapy`](https://pypi.org/project/jsonatapy/) | [`jsonata-rs`](https://pypi.org/project/jsonata-rs/) | [`jsonata-python`](https://pypi.org/project/jsonata-python/) |
|---|---|---|---|---|
| Implementation | translator, pure Python | native, Rust/PyO3 | native, Rust/PyO3 | interpreter, pure Python |
| **Evaluation** (`dict`→`dict`) | **214 µs** | 257 µs | 517 µs | 5 471 µs |
| Relative | **baseline** | 1.20x slower | 2.42x slower | 25.6x slower |
| Throughput | **4 677/s** | 3 889/s | 1 934/s | 183/s |
| Cold compilation | 6.92 ms | **0.26 ms** | 1.11 ms | 7.31 ms |
| Wheels on PyPI | pure Python (any platform) | 16, incl. Windows | 5 — **no Windows wheel** | pure Python (any platform) |

Versions measured: jsonata2py 0.1.0, jsonatapy 2.2.7, jsonata-rs 0.1.4,
jsonata-python 0.7.0 — each the latest release on PyPI when these numbers were
taken (re-measured 2026-08-24).

### Why a pure-Python library beats two Rust ones here

This is the part worth understanding before you trust the table, because
"pure Python beats Rust" is not a claim that should be taken at face value.

A native extension has to move your data across the FFI boundary. Every
`evaluate()` call converts the input `dict` into the extension's own value
representation and converts the result back. That cost scales with the size of the
data, not the complexity of the expression. jsonata2py generates Python code that
reads *the objects you already have* — it converts nothing.

So the native libraries pay a per-call tax that jsonata2py does not, and on
documents of this size that tax exceeds their raw evaluation advantage. Three
independent observations confirm the mechanism rather than assuming it:

1. **Give `jsonatapy` a path that avoids the boundary and it wins.** Its native
   `evaluate_json()` takes JSON text and returns JSON text, never materialising a
   Python object graph: 254 µs versus jsonata2py's 270 µs for the same
   text-in/text-out work. Exactly the reversal the explanation predicts.
2. **Shrink the expression and the advantage inverts.** On a trivial
   `$sum(items.value)`, jsonatapy is ~1.15-1.3x *faster* than jsonata2py — there is
   barely any evaluation work for jsonata2py's compiled code to win back.
3. **Grow the document and the advantage erodes.** Marshalling cost rises with
   document size until it cancels the native speed advantage entirely:

   | `$sum(items.value)` | n=10 | n=100 | n=1 000 | n=10 000 |
   |---|---|---|---|---|
   | jsonata2py | 2.9 µs | 20.4 µs | 195 µs | 1 989 µs |
   | jsonatapy | 2.5 µs | 16.3 µs | 151 µs | 2 133 µs |
   | jsonata-rs | 6.0 µs | 41.0 µs | 373 µs | 4 753 µs |
   | jsonata-python | 67.2 µs | 461 µs | 4 322 µs | 42 115 µs |

The honest summary: **jsonata2py is fastest when a non-trivial expression runs
against Python objects you already hold.** That is the common case for a JSONata
library embedded in a Python service, which is why it leads the first table. It is
not a general claim to be faster than Rust.

### When compilation pays for itself

Compilation is a one-time cost; evaluation is what repeats. Dividing the extra
compile time by the per-evaluation saving gives the break-even point on this
workload:

| Compared with | Extra compile cost | Saved per evaluation | Break-even |
|---|---|---|---|
| `jsonatapy` | +6.66 ms | 43 µs | **~154 evaluations** |
| `jsonata-rs` | +5.81 ms | 303 µs | **~19 evaluations** |
| `jsonata-python` | *none* — 0.4 ms cheaper | 5 257 µs | **immediately** |

Compile once at startup, evaluate on the hot path, and the compilation cost stops
mattering after the first few hundred calls at worst. Compile inside a request
handler and you pay it every time — see the guidance below.

### Repeat compilation of the same text

Compiling *the same expression text* again is far cheaper than the table suggests,
in two tiers. While an earlier `CompiledExpression` for that text is still
reachable, a repeat `compile()` reuses its entry point and costs **~1 µs**. Once
that has been collected, the factory still holds the compiled *code object*, and
re-executing it into a fresh module namespace costs **~15 µs** rather than
re-running the whole pipeline — the case a long-lived process actually hits when it
compiles an expression, uses it, drops it, and meets the same text again later.

Neither tier can leak. The first holds only a *weak* reference to the entry point.
The second holds a code object, which references neither a module namespace nor a
`CompiledExpression`, so dropping every reference to one still leaves its generated
module fully collectible (`tests/test_memory.py` asserts exactly this). The
code-object tier is bounded by generated-source bytes rather than entry count,
because generated modules differ in size by two orders of magnitude.

This does not relax the "don't call `compile()` on the hot path" guidance: text the
cache has never seen, or has already evicted, still pays full price.

### Where the speed comes from

Per-node visitor dispatch and type-check chains disappear at compile time; a JSONata
variable becomes a Python local; a built-in call resolves directly to the runtime
function the translator already chose, with no re-dispatch; `and`/`or` are emitted
as Python's own short-circuiting operators rather than helper calls taking a closure
per operand; sorting uses a native key-sort instead of a comparator callback wrapped
in `functools.cmp_to_key`; and fused aggregate paths (`$count(x[field = "value"])`,
`$sum(x.field)`) run as a single loop with no intermediate list.

The Java sibling's headline — ~40x over JSONata4Java — does **not** carry over.
That number comes from JIT-compiled bytecode replacing an AST interpreter, and
CPython has no JIT: generated Python source runs on the same interpreter an AST
walker would. What compilation still removes is the per-node overhead listed above.

Unlike the Java library, batching many `compile()` calls into `compile_all` is
**not** a meaningful win here — see [Compile an expression](#2-compile-an-expression).

### Reproducing these numbers

```
pytest tests/benchmarks -m benchmark
```

Measured on an Intel Core i7-1185G7 @ 3.00 GHz (4 cores), Windows 11, CPython
3.14.3. Methodology, because cross-library benchmarks are easy to get wrong:

- **Each implementation runs in its own process.** Measuring them in one
  interpreter made `jsonata-python` look 2.5x slower than it is — discarded
  compiled modules from jsonata2py's cold-compile rounds created GC pressure that
  landed on whichever allocation-heavy library was measured next.
- **Trials are interleaved** (round-robin, repeated) so slow machine drift affects
  every library equally instead of favouring whichever ran first. The minimum
  across trials is kept; noise only ever adds time.
- **jsonata2py's compile cache is deliberately defeated** for the compile row (a
  fresh factory plus a unique inert comment), so it is a genuine cold compile
  measured against libraries that have no cache at all.
- **`jsonata-rs` and `jsonata-python` were measured in separate virtualenvs**, for
  the reason in the next section, with jsonata2py present in both as the anchor
  used to normalise across them.

Timings are not portable between machines. Regressions against your own baseline are
gated by an opt-in check: `pytest tests/benchmarks -m perfgate --perf-record` to
record, then `-m perfgate` to enforce. Baselines are not committed.

## Choosing between jsonata2py and the alternatives

**Use jsonata2py when you evaluate the same expression many times against Python
objects.** That is where the design pays: compile once at startup, then every
evaluation runs generated Python code directly over the `dict`s and `list`s you
already have, with no marshalling and no AST walk. Concretely, it is the right
default if you hold a compiled expression on a service, a worker, or a pipeline
stage and call it per request, per row, or per message — and especially if you want
no native dependency, no build toolchain, and one wheel that installs everywhere.
It is also the only one of the four with typed bindings, injectable Python
functions, JSONata libraries, an evaluation timeout, and full `mypy --strict`
coverage, so it fits best when JSONata is a first-class part of your application
rather than an occasional utility call.

**Prefer [`jsonatapy`](https://pypi.org/project/jsonatapy/)** when your data is
already JSON *text* and you want text back — its native `evaluate_json()` keeps
everything on the Rust side and beats jsonata2py at that. Prefer it too for
short-lived work where compilation dominates (its cold compile is 27x cheaper, so
under ~154 evaluations of a given expression it comes out ahead), or for very simple
expressions over small documents, where there is too little evaluation work for a
translator to win back. It ships Windows wheels and has the broadest wheel coverage
of the native options.

**Prefer [`jsonata-rs`](https://pypi.org/project/jsonata-rs/)** if you specifically
want its Rust implementation of the jsonata-java reference semantics and you are on
Linux or macOS. Be aware of two practical constraints: it publishes **no Windows
wheel**, so Windows users need a Rust toolchain to install it at all; and it
installs a top-level module named `jsonata` — the *same* name
[`jsonata-python`](https://pypi.org/project/jsonata-python/) uses — so the two
silently overwrite each other and cannot coexist in one environment. On this
workload it measured ~2.5x slower than jsonata2py.

**Prefer [`jsonata-python`](https://pypi.org/project/jsonata-python/)** when you
want the closest thing to the reference implementation and performance genuinely
does not matter — a one-off script, a test fixture, a CLI that evaluates an
expression once and exits. It is a pure-Python AST interpreter, which makes it easy
to read and debug, but it evaluates ~26x slower than jsonata2py here *and* compiles
more slowly, so there is no workload shape where it is the faster choice.

## Thread safety

A `JsonataExpressionFactory` instance and all `CompiledExpression` instances it produces are fully thread-safe. `evaluate()` is stateless — each call reads the input independently and returns a new value without modifying any shared state. Per-evaluation state (bindings overlay, timeout deadline, call depth) lives in a `contextvars.ContextVar`, which is also what makes evaluation correct across `asyncio` tasks, not just OS threads: each task runs in its own copied context.

```python
# Compile once at startup
total_price = factory.compile("$sum(items.(price * qty))")

# Call concurrently from any number of threads
import concurrent.futures
with concurrent.futures.ThreadPoolExecutor(max_workers=16) as pool:
    pool.submit(total_price.evaluate, request_data)
```

---

## Architecture overview

```
expression string
       |
       v
  Parser.parse()                  -> AstNode (frozen dataclass hierarchy)
       |
       v
  optimize()                      -> AstNode (constant-folded, simplified)
       |
       v
  Translator.translate()          -> Python 3.11+ source string
       |
       v
  ExpressionLoader.load()         -> CompiledExpression (compiled, in-memory)
       |
       v
  expr.evaluate(data)             -> a plain Python value
```

`JsonataExpressionFactory.compile()` runs this entire pipeline in a single call.

### Package structure

| Module | Contents |
|---|---|
| `jsonata2py` | Public API: `compile`, `compile_all`, `CompiledExpression`, `JsonataExpressionFactory`, `JsonataBindings`, `JsonataBoundFunction`, `JsonataFunctionArguments`, `bound_function`, `JsonataLibrary`, `JsonataLibraryOptions`, `JsonataError` and its subclasses, `MISSING` |
| `jsonata2py.parser` | `Parser`, lexer, tokens, AST node dataclasses |
| `jsonata2py.optimizer` | `optimize` |
| `jsonata2py.translator` | `Translator` and the code-generation helpers it uses |
| `jsonata2py.runtime` | Runtime support: `core`, `context` (the `ContextVar`-based evaluation state), `lambdas`, `sequences`, `signature`, `values`, plus `strings/`, `numeric/`, `datetime/` built-in packages |
| `jsonata2py.loader` | `ExpressionLoader` |

---

## License

This project is licensed under the [MIT License](https://github.com/vlad-public-code/org.json-kula.jsonata2py/blob/main/LICENSE).

## See also

- [jsonata-jvm-compiler](https://vlad-public-code.github.io/org.json-kula.jsonata-jvm-compiler/) — the Java sibling this library is ported from.
- [tracked-json](https://vlad-public-code.github.io/org.json-kula.tracked-json/) — Jackson JsonNode wrapper that tracks each node's location (JsonPointer) and document root through every navigation — get, path, at, parent(), and JSONPath (RFC 9535). Includes JSON Patch (RFC 6902).
- [Valem](https://vlad-public-code.github.io/org.json-kula.valem/) — deterministic reactive computation runtime for AI-generated structured data models.
- [Valem Sandbox](https://valem.run/)
