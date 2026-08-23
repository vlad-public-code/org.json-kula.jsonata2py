"""Exception hierarchy for jsonata2py.

Mirrors the Java exception hierarchy (JsonataException and its
implementors) from jsonata-jvm-compiler. Error codes (T0410, D3050,
U1001, ...) are the contract the official JSONata test suite checks
and must be preserved byte-for-byte with the Java source.
"""

from __future__ import annotations

import re as _re

_ERROR_CODE_PATTERN = _re.compile(r"[A-Z]\d{4}")


class JsonataError(Exception):
    """Base class for all errors raised by this library."""

    def __init__(self, error_code: str | None, message: str) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.message = message


class JsonataCompilationError(JsonataError):
    """Raised by JsonataExpressionFactory.compile when an expression cannot
    be turned into a JsonataExpression.

    The cause is always one of:
      - ParseError -- the expression is syntactically invalid
      - LoadError -- the generated Python source failed to compile
        (internal error)
    """

    def __init__(self, error_code: str | None, message: str, cause: BaseException | None = None) -> None:
        super().__init__(error_code, message)
        if cause is not None:
            self.__cause__ = cause


class JsonataEvaluationError(JsonataError):
    """Raised when a generated JSONata expression cannot be evaluated.

    Covers two failure modes: the input is not valid JSON, or the
    expression logic fails at runtime (e.g. type mismatch, division by
    zero).
    """

    def __init__(self, error_code: str | None, message: str, cause: BaseException | None = None) -> None:
        super().__init__(error_code, message)
        if cause is not None:
            self.__cause__ = cause


class ParseError(JsonataError):
    """Raised when a JSONata expression string cannot be parsed."""

    def __init__(self, error_code: str | None, message: str, position: int = -1) -> None:
        full_message = f"{message} (position {position})" if position >= 0 else message
        super().__init__(error_code, full_message)
        self.position = position

    @staticmethod
    def with_error_code_from_message(message: str, position: int) -> ParseError:
        return ParseError(_extract_error_code(message), message, position)


def _extract_error_code(message: str) -> str:
    match = _ERROR_CODE_PATTERN.search(message)
    return match.group() if match else "UNKNOWN"


class _RuntimeEvaluationError(JsonataError):
    """Internal error raised during evaluation of generated code.

    Mapped to JsonataEvaluationError at the CompiledExpression.evaluate
    boundary. Never escapes the library's public API.
    """


class LoadError(JsonataError):
    """Raised when generated Python source cannot be compiled or executed
    as a CompiledExpression."""

    def __init__(self, message: str, cause: BaseException | None = None) -> None:
        super().__init__(None, message)
        if cause is not None:
            self.__cause__ = cause


class TranslatorError(JsonataError):
    """Raised for internal errors during AST-to-source translation."""

    def __init__(self, error_code: str | None, message: str, cause: BaseException | None = None) -> None:
        super().__init__(error_code, message)
        if cause is not None:
            self.__cause__ = cause
