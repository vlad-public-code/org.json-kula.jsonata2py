"""Hand-written lexer (tokenizer) for the JSONata expression language.

Ported from org.json_kula.jsonata_jvm.parser.lexer.Lexer.

Produces a flat list of Tokens from a source string. Whitespace and
comments (/* ... */) are silently skipped.

Supported literals:
  - Double-quoted strings with standard JSON escape sequences
  - Single-quoted strings (same escape rules)
  - Numbers: integer, decimal, and scientific notation
  - Keywords: true, false, null, and, or, in, not
  - Regex literals: /pattern/flags

Backtick-quoted names are emitted as TokenType.IDENTIFIER with the raw
(unescaped) content between the backticks as the value.
"""

from __future__ import annotations

from ..errors import ParseError
from .tokens import Token, TokenType

_KEYWORDS: dict[str, TokenType] = {
    "true": TokenType.TRUE,
    "false": TokenType.FALSE,
    "null": TokenType.NULL,
    "and": TokenType.AND,
    "or": TokenType.OR,
    "in": TokenType.IN,
    "not": TokenType.NOT,
}

_SINGLE_CHAR_TOKENS: dict[str, TokenType] = {
    "+": TokenType.PLUS,
    "-": TokenType.MINUS,
    "%": TokenType.PERCENT,
    "&": TokenType.AMPERSAND,
    ",": TokenType.COMMA,
    ";": TokenType.SEMICOLON,
    "(": TokenType.LPAREN,
    ")": TokenType.RPAREN,
    "[": TokenType.LBRACKET,
    "]": TokenType.RBRACKET,
    "{": TokenType.LBRACE,
    "}": TokenType.RBRACE,
    "@": TokenType.AT,
    "^": TokenType.CARET,
    "|": TokenType.PIPE,
    "#": TokenType.HASH,
    "=": TokenType.EQUAL,
}

_DIVISION_CONTEXT = {
    TokenType.NUMBER,
    TokenType.STRING,
    TokenType.TRUE,
    TokenType.FALSE,
    TokenType.NULL,
    TokenType.RPAREN,
    TokenType.RBRACKET,
    TokenType.RBRACE,
    TokenType.VARIABLE,
    TokenType.DOLLAR,
    TokenType.DOLLAR_DOLLAR,
    TokenType.IDENTIFIER,
}


def _is_ident_start(c: str) -> bool:
    return c.isalpha() or c == "_"


def _is_ident_part(c: str) -> bool:
    return c.isalnum() or c == "_"


class Lexer:
    def __init__(self, src: str) -> None:
        self.src = src
        self.pos = 0
        self.last_token: TokenType | None = None

    @staticmethod
    def tokenize(source: str) -> list[Token]:
        """Tokenizes source and returns all tokens including a terminal EOF token."""
        return Lexer(source)._scan()

    # -------------------------------------------------------------------------

    def _scan(self) -> list[Token]:
        tokens: list[Token] = []
        n = len(self.src)
        while self.pos < n:
            self._skip_whitespace_and_comments()
            if self.pos >= n:
                break

            start = self.pos
            c = self.src[self.pos]
            tt = _SINGLE_CHAR_TOKENS.get(c)

            if tt is not None:
                self.pos += 1
                token = Token(tt, "", start)
            elif c == "/":
                token = self._lex_slash_or_regex(start)
            elif c == "?":
                token = self._lex_question(start)
            elif c == "*":
                token = self._lex_star(start)
            elif c == ".":
                token = self._lex_dot(start)
            elif c == ":":
                token = self._lex_colon(start)
            elif c == "<":
                token = self._lex_less(start)
            elif c == ">":
                token = self._lex_greater(start)
            elif c == "!":
                token = self._lex_bang(start)
            elif c == "~":
                token = self._lex_tilde(start)
            elif c == "$":
                token = self._lex_dollar(start)
            elif c in ('"', "'"):
                token = self._lex_string(start)
            elif c == "`":
                token = self._lex_backtick_identifier(start)
            elif c.isdigit():
                token = self._lex_number(start)
            elif _is_ident_start(c):
                token = self._lex_identifier_or_keyword(start)
            else:
                raise ParseError(None, f"Unexpected character: '{c}'", start)

            tokens.append(token)
            self.last_token = token.type
        tokens.append(Token(TokenType.EOF, "", self.pos))
        return tokens

    # -------------------------------------------------------------------------
    # Multi-char token helpers
    # -------------------------------------------------------------------------

    def _lex_question(self, start: int) -> Token:
        self.pos += 1
        if self.pos < len(self.src) and self.src[self.pos] == ":":
            self.pos += 1
            return Token(TokenType.QUESTION_COLON, "", start)
        if self.pos < len(self.src) and self.src[self.pos] == "?":
            self.pos += 1
            return Token(TokenType.QUESTION_QUESTION, "", start)
        return Token(TokenType.QUESTION, "", start)

    def _lex_star(self, start: int) -> Token:
        self.pos += 1
        if self.pos < len(self.src) and self.src[self.pos] == "*":
            self.pos += 1
            return Token(TokenType.STAR_STAR, "", start)
        return Token(TokenType.STAR, "", start)

    def _lex_dot(self, start: int) -> Token:
        self.pos += 1
        if self.pos < len(self.src) and self.src[self.pos] == ".":
            self.pos += 1
            return Token(TokenType.DOT_DOT, "", start)
        return Token(TokenType.DOT, "", start)

    def _lex_colon(self, start: int) -> Token:
        self.pos += 1
        if self.pos < len(self.src) and self.src[self.pos] == "=":
            self.pos += 1
            return Token(TokenType.COLON_ASSIGN, "", start)
        return Token(TokenType.COLON, "", start)

    def _lex_less(self, start: int) -> Token:
        self.pos += 1
        if self.pos < len(self.src) and self.src[self.pos] == "=":
            self.pos += 1
            return Token(TokenType.LESS_EQUAL, "", start)
        return Token(TokenType.LESS, "", start)

    def _lex_greater(self, start: int) -> Token:
        self.pos += 1
        if self.pos < len(self.src) and self.src[self.pos] == "=":
            self.pos += 1
            return Token(TokenType.GREATER_EQUAL, "", start)
        return Token(TokenType.GREATER, "", start)

    def _lex_bang(self, start: int) -> Token:
        self.pos += 1
        if self.pos < len(self.src) and self.src[self.pos] == "=":
            self.pos += 1
            return Token(TokenType.NOT_EQUAL, "", start)
        raise ParseError("S0204", "Unexpected '!' character", start)

    def _lex_tilde(self, start: int) -> Token:
        self.pos += 1
        if self.pos < len(self.src) and self.src[self.pos] == ">":
            self.pos += 1
            return Token(TokenType.TILDE_GT, "", start)
        raise ParseError(None, "Expected '>' after '~'", start)

    def _lex_dollar(self, start: int) -> Token:
        self.pos += 1  # consume '$'
        if self.pos < len(self.src) and self.src[self.pos] == "$":
            self.pos += 1
            return Token(TokenType.DOLLAR_DOLLAR, "", start)
        # $name or bare $
        name_start = self.pos
        while self.pos < len(self.src) and _is_ident_part(self.src[self.pos]):
            self.pos += 1
        name = self.src[name_start : self.pos]
        if name == "":
            return Token(TokenType.DOLLAR, "", start)
        return Token(TokenType.VARIABLE, name, start)

    # -------------------------------------------------------------------------
    # Slash: division or regex literal
    # -------------------------------------------------------------------------

    def _lex_slash_or_regex(self, start: int) -> Token:
        """Decides whether '/' starts a regex literal or is division, based on
        the type of the last emitted token.

        A '/' is treated as division only when it follows a value-producing
        token: a literal (NUMBER, STRING, TRUE, FALSE, NULL), a closing
        bracket/paren (RPAREN, RBRACKET, RBRACE), a variable (VARIABLE,
        DOLLAR, DOLLAR_DOLLAR), or a bare identifier (IDENTIFIER). In all
        other positions (start of expression, after an operator, after an
        opening bracket) a '/' begins a regex literal.
        """
        if self.last_token is not None and self.last_token in _DIVISION_CONTEXT:
            self.pos += 1
            return Token(TokenType.SLASH, "", start)
        return self._lex_regex(start)

    def _lex_regex(self, start: int) -> Token:
        """Lexes a regex literal /pattern/flags.

        The token value is "pattern/flags" (the enclosing slashes are
        consumed but not included; the separator slash before flags is kept
        as a delimiter so the pattern and flags can be recovered with
        value.rindex('/')).
        """
        self.pos += 1  # consume opening '/'
        pattern_chars: list[str] = []
        in_char_class = False
        n = len(self.src)
        while self.pos < n:
            c = self.src[self.pos]
            if c == "[":
                in_char_class = True
                pattern_chars.append(c)
                self.pos += 1
            elif c == "]":
                in_char_class = False
                pattern_chars.append(c)
                self.pos += 1
            elif c == "\\" and self.pos + 1 < n:
                pattern_chars.append(c)
                pattern_chars.append(self.src[self.pos + 1])
                self.pos += 2
            elif c == "/" and not in_char_class:
                self.pos += 1  # consume closing '/'
                break
            elif c in ("\n", "\r"):
                raise ParseError(None, "Unterminated regex literal", start)
            else:
                pattern_chars.append(c)
                self.pos += 1
        # Collect flags -- JSONata supports 'i' (case-insensitive) and 'm' (multiline)
        flag_chars: list[str] = []
        while self.pos < n:
            f = self.src[self.pos]
            if f in ("i", "m"):
                flag_chars.append(f)
                self.pos += 1
            else:
                break
        pattern = "".join(pattern_chars)
        flags = "".join(flag_chars)
        return Token(TokenType.REGEX, f"{pattern}/{flags}", start)

    # -------------------------------------------------------------------------
    # String literals
    # -------------------------------------------------------------------------

    def _lex_string(self, start: int) -> Token:
        quote = self.src[self.pos]
        self.pos += 1  # consume opening quote
        sb: list[str] = []
        n = len(self.src)
        while self.pos < n:
            c = self.src[self.pos]
            if c == quote:
                self.pos += 1  # consume closing quote
                return Token(TokenType.STRING, "".join(sb), start)
            if c == "\\":
                self.pos += 1
                if self.pos >= n:
                    raise ParseError(None, "Unterminated string escape", self.pos - 1)
                esc = self.src[self.pos]
                self.pos += 1
                if esc == '"':
                    sb.append('"')
                elif esc == "'":
                    sb.append("'")
                elif esc == "\\":
                    sb.append("\\")
                elif esc == "/":
                    sb.append("/")
                elif esc == "b":
                    sb.append("\b")
                elif esc == "f":
                    sb.append("\f")
                elif esc == "n":
                    sb.append("\n")
                elif esc == "r":
                    sb.append("\r")
                elif esc == "t":
                    sb.append("\t")
                elif esc == "u":
                    ch = self._read_unicode_escape(self.pos - 2)
                    # A \uXXXX high surrogate immediately followed by a
                    # \uYYYY low surrogate encodes one astral-plane code
                    # point (JSON strings are UTF-16-escaped like JS
                    # source); Python's chr() treats each \u escape as an
                    # independent code point, so without this the pair
                    # would become two unpaired (invalid) surrogate
                    # characters instead of merging into one.
                    if (
                        0xD800 <= ord(ch) <= 0xDBFF
                        and self.pos + 1 < n
                        and self.src[self.pos] == "\\"
                        and self.src[self.pos + 1] == "u"
                    ):
                        save_pos = self.pos
                        self.pos += 2
                        low = self._read_unicode_escape(save_pos)
                        if 0xDC00 <= ord(low) <= 0xDFFF:
                            codepoint = 0x10000 + (ord(ch) - 0xD800) * 0x400 + (ord(low) - 0xDC00)
                            ch = chr(codepoint)
                        else:
                            self.pos = save_pos  # not a low surrogate: rewind, handle separately
                    sb.append(ch)
                else:
                    raise ParseError("S0103", f"Unsupported escape sequence: \\{esc}", self.pos - 2)
            else:
                sb.append(c)
                self.pos += 1
        # Emit a deferred-error token so the parser can produce a higher-level
        # error (e.g. S0202) if it encounters an unexpected token before this position.
        return Token(TokenType.ERROR, "S0101: Unterminated string literal", start)

    def _read_unicode_escape(self, error_pos: int) -> str:
        if self.pos + 4 > len(self.src):
            raise ParseError("S0104", "The escape sequence \\u must be followed by 4 hex digits", error_pos)
        hex_str = self.src[self.pos : self.pos + 4]
        self.pos += 4
        try:
            return chr(int(hex_str, 16))
        except ValueError:
            raise ParseError(
                "S0104", f"The escape sequence \\u must be followed by 4 hex digits: {hex_str}", error_pos
            ) from None

    # -------------------------------------------------------------------------
    # Backtick identifiers
    # -------------------------------------------------------------------------

    def _lex_backtick_identifier(self, start: int) -> Token:
        self.pos += 1  # consume opening backtick
        name_start = self.pos
        n = len(self.src)
        while self.pos < n and self.src[self.pos] != "`":
            self.pos += 1
        if self.pos >= n:
            raise ParseError("S0105", "Unterminated backtick identifier", start)
        name = self.src[name_start : self.pos]
        self.pos += 1  # consume closing backtick
        return Token(TokenType.IDENTIFIER, name, start)

    # -------------------------------------------------------------------------
    # Numbers
    # -------------------------------------------------------------------------

    def _lex_number(self, start: int) -> Token:
        begin = self.pos
        n = len(self.src)
        # Integer part
        while self.pos < n and self.src[self.pos].isdigit():
            self.pos += 1
        # Decimal part
        if self.pos < n and self.src[self.pos] == "." and self.pos + 1 < n and self.src[self.pos + 1].isdigit():
            self.pos += 1  # consume '.'
            while self.pos < n and self.src[self.pos].isdigit():
                self.pos += 1
        # Exponent part
        if self.pos < n and self.src[self.pos] in ("e", "E"):
            self.pos += 1
            if self.pos < n and self.src[self.pos] in ("+", "-"):
                self.pos += 1
            if self.pos >= n or not self.src[self.pos].isdigit():
                raise ParseError(None, "Malformed number exponent", start)
            while self.pos < n and self.src[self.pos].isdigit():
                self.pos += 1
        num_str = self.src[begin : self.pos]
        # A number immediately followed by a letter/underscore is a syntax error (e.g. 7a)
        if self.pos < n and _is_ident_start(self.src[self.pos]):
            raise ParseError(
                "S0201", f"Syntax error: malformed number '{num_str}{self.src[self.pos]}'", start
            )
        try:
            val = float(num_str)
            if val in (float("inf"), float("-inf")):
                raise ParseError("S0102", f"Number out of range: {num_str}", start)
        except ValueError:
            raise ParseError("S0102", f"Number out of range: {num_str}", start) from None
        return Token(TokenType.NUMBER, num_str, start)

    # -------------------------------------------------------------------------
    # Identifiers and keywords
    # -------------------------------------------------------------------------

    def _lex_identifier_or_keyword(self, start: int) -> Token:
        begin = self.pos
        n = len(self.src)
        while self.pos < n and _is_ident_part(self.src[self.pos]):
            self.pos += 1
        text = self.src[begin : self.pos]
        kw = _KEYWORDS.get(text)
        if kw is not None:
            return Token(kw, text, start)
        return Token(TokenType.IDENTIFIER, text, start)

    # -------------------------------------------------------------------------
    # Whitespace and comments
    # -------------------------------------------------------------------------

    def _skip_whitespace_and_comments(self) -> None:
        n = len(self.src)
        while self.pos < n:
            c = self.src[self.pos]
            if c.isspace():
                self.pos += 1
            elif c == "/" and self.pos + 1 < n and self.src[self.pos + 1] == "*":
                self._skip_block_comment()
            else:
                break

    def _skip_block_comment(self) -> None:
        start = self.pos
        self.pos += 2  # skip /*
        n = len(self.src)
        while self.pos + 1 < n:
            if self.src[self.pos] == "*" and self.src[self.pos + 1] == "/":
                self.pos += 2
                return
            self.pos += 1
        raise ParseError("S0106", "Unterminated block comment", start)
