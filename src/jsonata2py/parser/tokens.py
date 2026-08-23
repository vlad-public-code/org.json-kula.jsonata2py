"""Lexical token types for the JSONata expression language.

Ported from org.json_kula.jsonata_jvm.parser.lexer.TokenType / Token.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass


class TokenType(enum.Enum):
    # Literals
    STRING = "STRING"  # "hello", 'hello'
    NUMBER = "NUMBER"  # 42, 3.14, 1e10
    TRUE = "TRUE"  # true
    FALSE = "FALSE"  # false
    NULL = "NULL"  # null
    REGEX = "REGEX"  # /pattern/flags

    # References
    DOLLAR = "DOLLAR"  # $   (context value / root in function params)
    DOLLAR_DOLLAR = "DOLLAR_DOLLAR"  # $$  (root of input document)
    VARIABLE = "VARIABLE"  # $name

    # Identifiers
    IDENTIFIER = "IDENTIFIER"  # field name or function name (bare or `backtick-quoted`)

    # Arithmetic operators
    PLUS = "PLUS"  # +
    MINUS = "MINUS"  # -
    STAR = "STAR"  # *
    SLASH = "SLASH"  # /
    PERCENT = "PERCENT"  # %

    # String concatenation
    AMPERSAND = "AMPERSAND"  # &

    # Comparison operators
    EQUAL = "EQUAL"  # =
    NOT_EQUAL = "NOT_EQUAL"  # !=
    LESS = "LESS"  # <
    LESS_EQUAL = "LESS_EQUAL"  # <=
    GREATER = "GREATER"  # >
    GREATER_EQUAL = "GREATER_EQUAL"  # >=

    # Boolean keywords
    AND = "AND"  # and
    OR = "OR"  # or
    IN = "IN"  # in
    NOT = "NOT"  # not  (keyword, used as function-like prefix)

    # Conditional / binding
    QUESTION = "QUESTION"  # ?
    QUESTION_COLON = "QUESTION_COLON"  # ?:  (Elvis / default operator)
    QUESTION_QUESTION = "QUESTION_QUESTION"  # ??  (Coalescing operator)
    COLON = "COLON"  # :
    COLON_ASSIGN = "COLON_ASSIGN"  # :=

    # Path / step operators
    DOT = "DOT"  # .
    DOT_DOT = "DOT_DOT"  # ..  (not standard JSONata but reserved)
    STAR_STAR = "STAR_STAR"  # **  (descendant wildcard)

    # Chain / transform
    TILDE_GT = "TILDE_GT"  # ~>

    # Other operators
    PIPE = "PIPE"  # |   (transform / filter union)
    CARET = "CARET"  # ^   (sort expression prefix)
    AT = "AT"  # @   (context binding in path steps)
    HASH = "HASH"  # #   (position binding in path steps)

    # Delimiters
    LPAREN = "LPAREN"  # (
    RPAREN = "RPAREN"  # )
    LBRACKET = "LBRACKET"  # [
    RBRACKET = "RBRACKET"  # ]
    LBRACE = "LBRACE"  # {
    RBRACE = "RBRACE"  # }
    COMMA = "COMMA"  # ,
    SEMICOLON = "SEMICOLON"  # ;   (function parameter separator)
    BACKTICK = "BACKTICK"  # `   (should never appear as standalone -- absorbed into IDENTIFIER)

    # End of input
    EOF = "EOF"

    # Deferred lexer error -- value holds the original error message, position holds
    # the error site. Emitted instead of raising so the parser can produce a
    # higher-level error first (e.g. S0202 for an unexpected token before the
    # unterminated-string position).
    ERROR = "ERROR"


@dataclass(slots=True)
class Token:
    """A single lexical token produced by the JSONata Lexer.

    Attributes:
        type: the kind of token
        value: the raw text from the source (empty string for punctuation
            tokens whose type alone carries all the meaning)
        position: zero-based character offset of the first character of
            this token
    """

    type: TokenType
    value: str
    position: int

    @staticmethod
    def of(type: TokenType, value_or_position: str | int, position: int | None = None) -> Token:
        """Convenience factory.

        Token.of(type, position) for punctuation tokens with no text.
        Token.of(type, value, position) for tokens whose text matters.
        """
        if position is None:
            # Two-argument form: value_or_position IS the position.
            assert isinstance(value_or_position, int)
            return Token(type, "", value_or_position)
        # Three-argument form: value_or_position is the token text.
        assert isinstance(value_or_position, str)
        return Token(type, value_or_position, position)

    def __str__(self) -> str:
        if self.value == "":
            return f"{self.type.name}@{self.position}"
        return f"{self.type.name}({self.value})@{self.position}"
