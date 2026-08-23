"""Core picture-string formatter for $formatNumber.

Ported from org.json_kula.jsonata_jvm.runtime.numeric.DecimalPicture.

Implements the XPath/JSONata decimal-format picture string specification
(W3C XSLT 2.0 section 16): prefix/suffix, mandatory/optional digit
placeholders, grouping separators, decimal separator, exponent separator,
percent/per-mille, and Unicode digit families.

Java delegates the actual digit formatting to java.text.DecimalFormat
(HALF_EVEN rounding) and then does a large amount of custom
post-processing for scientific notation and manual grouping. Since the
DecimalFormat pattern built here is always drawn from a narrow character
set (only '0'/'#'/'.'/'E', grouping disabled), this port implements that
narrow subset directly with Python's decimal.Decimal instead of pulling
in a general decimal-format library -- see _format_fixed.
"""

from __future__ import annotations

from decimal import ROUND_HALF_EVEN, ROUND_HALF_UP, Decimal

from ...errors import _RuntimeEvaluationError as RuntimeEvaluationError


def format(
    v: float,
    pic: str,
    decimal_sep: str,
    group_sep: str,
    exponent_sep: str,
    percent: str,
    per_mille: str,
    zero_digit: str,
    digit_char: str,
) -> str:
    """Formats v (non-negative) according to the given single sub-picture
    string. The caller is responsible for splitting positive/negative
    sub-pictures and handling sign; v should already be the absolute
    value with any percent/per-mille scaling applied."""
    specials = [percent, per_mille]

    prefix: list[str] = []
    int_spec: list[str] = []
    frac_spec: list[str] = []
    exp_spec: list[str] = []
    suffix: list[str] = []

    int_group_offsets: list[int] = []
    frac_group_offsets: list[int] = []

    in_core = False
    past_decimal = False
    past_exponent = False
    in_suffix = False

    int_digit_count = 0
    frac_digit_count = 0
    int_digits_since_last_comma = 0
    frac_digits_since_last_comma = 0
    int_saw_comma = False

    decimal_count = 0
    percent_count = 0
    per_mille_count = 0
    last_was_grp = False
    int_had_opt_after_mand = False
    frac_had_opt_after_mand = False
    int_last_was_mand = False
    frac_last_was_opt = False

    i = 0
    n = len(pic)
    while i < n:
        matched_special = None
        for special in specials:
            if special and pic.startswith(special, i):
                matched_special = special
                break
        if matched_special is not None:
            is_percent = matched_special == percent
            is_per_mille = matched_special == per_mille
            if is_percent:
                percent_count += 1
            if is_per_mille:
                per_mille_count += 1
            if not in_core or in_suffix:
                (suffix if in_core else prefix).append(matched_special)
            elif past_exponent:
                raise RuntimeEvaluationError(
                    "D3092",
                    "$formatNumber: a percent or per-mille character must not appear in the exponent "
                    "part of the picture string",
                )
            else:
                in_suffix = True
                suffix.append(matched_special)
            i += len(matched_special)
            continue

        c = pic[i]
        is_mand = _is_mandatory_digit(c, zero_digit)
        is_opt = c == digit_char
        is_dec = c == decimal_sep
        is_grp = c == group_sep
        is_exp = c == exponent_sep
        is_core_char = is_mand or is_opt or is_dec or is_grp or is_exp

        if not in_core and is_core_char:
            in_core = True

        if not in_core:
            prefix.append(c)
        elif in_suffix:
            suffix.append(c)
        elif past_exponent:
            if is_mand or is_opt:
                exp_spec.append("0" if is_mand else "#")
            elif is_grp:
                raise RuntimeEvaluationError(
                    "D3093",
                    "$formatNumber: a grouping separator must not appear in the exponent part of the "
                    "picture string",
                )
            else:
                in_suffix = True
                suffix.append(c)
        elif past_decimal:
            if is_mand or is_opt:
                frac_spec.append("0" if is_mand else "#")
                frac_digit_count += 1
                frac_digits_since_last_comma += 1
                if is_mand and frac_last_was_opt:
                    frac_had_opt_after_mand = True
                if is_opt:
                    frac_last_was_opt = True
                last_was_grp = False
            elif is_dec:
                decimal_count += 1
                last_was_grp = False
                in_suffix = True
                suffix.append(c)
            elif is_grp:
                if last_was_grp:
                    raise RuntimeEvaluationError(
                        "D3089", "$formatNumber: a grouping separator must not be adjacent to another grouping separator"
                    )
                if frac_digits_since_last_comma > 0:
                    frac_group_offsets.append(frac_digits_since_last_comma)
                    frac_digits_since_last_comma = 0
                last_was_grp = True
            elif is_exp:
                if last_was_grp:
                    raise RuntimeEvaluationError(
                        "D3087", "$formatNumber: a grouping separator must not be adjacent to a decimal separator"
                    )
                past_exponent = True
                last_was_grp = False
            else:
                in_suffix = True
                suffix.append(c)
                last_was_grp = False
        else:
            # Integer part
            if is_mand or is_opt:
                int_spec.append("0" if is_mand else "#")
                int_digit_count += 1
                int_digits_since_last_comma += 1
                if is_opt and int_last_was_mand:
                    int_had_opt_after_mand = True
                if is_mand:
                    int_last_was_mand = True
                last_was_grp = False
            elif is_grp:
                if last_was_grp:
                    raise RuntimeEvaluationError(
                        "D3089", "$formatNumber: a grouping separator must not be adjacent to another grouping separator"
                    )
                if int_digits_since_last_comma > 0:
                    int_group_offsets.append(int_digits_since_last_comma)
                    int_digits_since_last_comma = 0
                int_saw_comma = True
                last_was_grp = True
            elif is_dec:
                if last_was_grp:
                    raise RuntimeEvaluationError(
                        "D3087", "$formatNumber: a grouping separator must not be adjacent to a decimal separator"
                    )
                decimal_count += 1
                past_decimal = True
                last_was_grp = False
            elif is_exp:
                past_exponent = True
                last_was_grp = False
            elif int_digit_count > 0 or int_saw_comma:
                raise RuntimeEvaluationError("D3086", "$formatNumber: an invalid character appeared in the sub-picture")
            else:
                in_suffix = True
                suffix.append(c)
                last_was_grp = False
        i += 1

    # Post-parse validation
    if decimal_count > 1:
        raise RuntimeEvaluationError("D3081", "$formatNumber: there must only be one decimal separator in the picture string")
    if percent_count > 1:
        raise RuntimeEvaluationError("D3082", "$formatNumber: there must only be one percent character in the picture string")
    if per_mille_count > 1:
        raise RuntimeEvaluationError("D3083", "$formatNumber: there must only be one per-mille character in the picture string")
    if percent_count > 0 and per_mille_count > 0:
        raise RuntimeEvaluationError(
            "D3084", "$formatNumber: a picture string must not contain both a percent and a per-mille character"
        )
    if past_exponent and int_digit_count == 0 and frac_digit_count == 0:
        raise RuntimeEvaluationError(
            "D3085", "$formatNumber: the picture string must contain at least one digit or zero-digit placeholder"
        )
    if int_had_opt_after_mand:
        raise RuntimeEvaluationError(
            "D3090",
            "$formatNumber: an optional digit character must not appear after a mandatory digit character in "
            "the integer part of the picture string",
        )
    if frac_had_opt_after_mand:
        raise RuntimeEvaluationError(
            "D3091",
            "$formatNumber: a mandatory digit character must not appear after an optional digit character in "
            "the fractional part of the picture string",
        )
    if last_was_grp and not past_decimal and not past_exponent:
        raise RuntimeEvaluationError(
            "D3088", "$formatNumber: a grouping separator must not appear at the end of the integer part of the picture string"
        )

    if not in_core:
        int_spec.append("0")

    int_spec_s = "".join(int_spec)
    frac_spec_s = "".join(frac_spec)
    exp_spec_s = "".join(exp_spec)

    min_int_digits = int_spec_s.count("0")
    min_frac_digits = frac_spec_s.count("0")
    max_frac_digits = len(frac_spec_s)

    bd_val = Decimal(repr(v))
    int_part, frac_part = _format_fixed(bd_val, min_int_digits, min_frac_digits, max_frac_digits)
    exp_str: str | None = None

    # Scientific notation: re-normalise to match picture intent.
    if past_exponent:
        frac_mandatory = frac_spec_s.count("0")
        has_mandatory_leading = "0" in int_spec_s
        starts_with_optional = int_spec_s == "" or int_spec_s == "#"

        bd = Decimal(repr(v))

        if has_mandatory_leading:
            int_digits = int_spec_s.count("0")
            if int_digits == 0:
                int_digits = 1

            lower = Decimal(10) ** (int_digits - 1)
            upper = Decimal(10) ** int_digits
            test = bd
            exp = 0

            while test >= upper:
                test = test / Decimal(10)
                exp += 1
            while 0 < test < lower:
                test = test * Decimal(10)
                exp -= 1

            rounded = test.quantize(Decimal(1).scaleb(-frac_mandatory) if frac_mandatory > 0 else Decimal(1), rounding=ROUND_HALF_UP)
            s = _plain_string(rounded)
            p = s.find(".")
            int_part = s[:p] if p >= 0 else s
            frac_part = s[p + 1 :] if p >= 0 else ""
            exp_str = str(exp)

        elif starts_with_optional:
            if frac_mandatory == 0:
                rounded_val = round(v * 10) / 10.0
                int_part = str(int(rounded_val))
                frac_part = str(round((rounded_val - int(rounded_val)) * 10))
                exp_str = "0"
            else:
                rounded = bd.quantize(Decimal(1).scaleb(-frac_mandatory), rounding=ROUND_HALF_UP)
                s = _plain_string(rounded)
                p = s.find(".")
                int_part = s[:p] if p >= 0 else s
                frac_part = s[p + 1 :] if p >= 0 else ""
                if pic.startswith(".") and int_part == "0":
                    int_part = ""
                exp_str = "0"

    # Insert grouping separators.
    digit_base_cp = _digit_base(zero_digit)
    custom_dig = digit_base_cp != ord("0")
    custom_dec = decimal_sep != "."
    custom_grp = group_sep != ","
    grp_char = group_sep if custom_grp else ","

    if int_saw_comma and int_group_offsets:
        int_part = _insert_grouping(int_part, int_group_offsets, int_digits_since_last_comma, grp_char)
    if frac_group_offsets:
        frac_part = _insert_grouping_frac(frac_part, frac_group_offsets, grp_char)

    # Assemble result.
    out: list[str] = ["".join(prefix)]
    _append_numeric_part(out, int_part, custom_dig, digit_base_cp)
    if past_decimal:
        out.append(decimal_sep if custom_dec else ".")
        _append_numeric_part(out, frac_part, custom_dig, digit_base_cp)
    if exp_str is not None:
        out.append(exponent_sep)
        exp_len = len(exp_spec_s)
        if exp_str.startswith("-"):
            out.append("-")
            exp_str = exp_str[1:]
        pad_char = chr(digit_base_cp) if custom_dig else "0"
        zeros = max(0, exp_len - len(exp_str))
        out.append(pad_char * zeros)
        _append_numeric_part(out, exp_str, custom_dig, digit_base_cp)
    out.append("".join(suffix))
    return "".join(out)


def _plain_string(d: Decimal) -> str:
    """Formats d without scientific notation, keeping trailing zeros as
    quantize left them (mirrors BigDecimal.toPlainString)."""
    sign, digits, exponent = d.as_tuple()
    # as_tuple()'s exponent is `int | Literal['n', 'N', 'F']`; the literal
    # forms occur only for NaN/Infinity, which never reach these
    # finite-only formatters.
    assert isinstance(exponent, int)
    digit_str = "".join(str(x) for x in digits)
    if exponent >= 0:
        s = digit_str + "0" * exponent
    else:
        frac_len = -exponent
        if len(digit_str) <= frac_len:
            digit_str = "0" * (frac_len - len(digit_str) + 1) + digit_str
        s = digit_str[: len(digit_str) - frac_len] + "." + digit_str[len(digit_str) - frac_len :]
    return ("-" + s) if sign else s


# =============================================================================
# Fixed-point formatting (java.text.DecimalFormat narrow-subset replacement)
# =============================================================================


def _format_fixed(bd: Decimal, min_int_digits: int, min_frac_digits: int, max_frac_digits: int) -> tuple[str, str]:
    """Mimics java.text.DecimalFormat non-scientific formatting for a
    pattern built only from '0'/'#' (rounding HALF_EVEN to
    max_frac_digits, trailing optional-zero stripping down to
    min_frac_digits, leading zero-padding up to min_int_digits, and an
    EMPTY integer part -- not "0" -- when min_int_digits is 0 and the
    rounded value's integer part is zero, matching Java's own behaviour)."""
    if max_frac_digits > 0:
        rounded = bd.quantize(Decimal(1).scaleb(-max_frac_digits), rounding=ROUND_HALF_EVEN)
    else:
        rounded = bd.quantize(Decimal(1), rounding=ROUND_HALF_EVEN)

    _sign, digits, exponent = rounded.as_tuple()
    # as_tuple()'s exponent is `int | Literal['n', 'N', 'F']`; the literal
    # forms occur only for NaN/Infinity, which never reach these
    # finite-only formatters.
    assert isinstance(exponent, int)
    digit_str = "".join(str(d) for d in digits)
    if exponent < 0:
        frac_len = -exponent
        if len(digit_str) <= frac_len:
            digit_str = "0" * (frac_len - len(digit_str) + 1) + digit_str
        int_str = digit_str[: len(digit_str) - frac_len]
        frac_str = digit_str[len(digit_str) - frac_len :]
    else:
        int_str = digit_str + "0" * exponent
        frac_str = ""

    frac_str = frac_str.ljust(max_frac_digits, "0")[: max_frac_digits if max_frac_digits > 0 else 0]

    if len(frac_str) > min_frac_digits:
        stripped = frac_str.rstrip("0")
        if len(stripped) < min_frac_digits:
            stripped = stripped.ljust(min_frac_digits, "0")
        frac_str = stripped

    int_str = int_str.lstrip("0") or "0"
    if min_int_digits == 0 and int_str == "0":
        int_str = ""
    elif len(int_str) < min_int_digits:
        int_str = int_str.rjust(min_int_digits, "0")

    return int_str, frac_str


# =============================================================================
# Grouping separator insertion
# =============================================================================


def _insert_grouping(digits: str, offsets: list[int], primary_group_size: int, sep: str) -> str:
    """Inserts grouping separators into the integer-part digit string.

    If there is only one grouping separator in the picture, or all
    inter-separator gaps equal primary_group_size, the grouping repeats
    uniformly from the right. Otherwise the exact picture positions are
    used.
    """
    n = len(digits)
    if primary_group_size <= 0 or n <= primary_group_size:
        return digits

    insert_at: list[int] = []
    if len(offsets) <= 1:
        pos_from_right = primary_group_size
        while pos_from_right < n:
            insert_at.append(n - pos_from_right)
            pos_from_right += primary_group_size
    else:
        regular = all(offsets[k] == primary_group_size for k in range(1, len(offsets)))
        if regular:
            pos_from_right = primary_group_size
            while pos_from_right < n:
                insert_at.append(n - pos_from_right)
                pos_from_right += primary_group_size
        else:
            pos_from_right = primary_group_size
            pfl = n - pos_from_right
            if pfl > 0:
                insert_at.append(pfl)
            for k in range(len(offsets) - 1, 0, -1):
                pos_from_right += offsets[k]
                pfl = n - pos_from_right
                if pfl > 0:
                    insert_at.append(pfl)

    if not insert_at:
        return digits
    insert_at.sort()

    out: list[str] = []
    pos_idx = 0
    for j in range(n):
        if pos_idx < len(insert_at) and j == insert_at[pos_idx]:
            out.append(sep)
            pos_idx += 1
        out.append(digits[j])
    return "".join(out)


def _insert_grouping_frac(digits: str, offsets: list[int], sep: str) -> str:
    """Inserts grouping separators into the fractional-part digit string
    (left-to-right)."""
    n = len(digits)
    positions: list[int] = []
    cumulative = 0
    for offset in offsets:
        cumulative += offset
        if cumulative < n:
            positions.append(cumulative)
    if not positions:
        return digits

    out: list[str] = []
    pos_idx = 0
    for j in range(n):
        out.append(digits[j])
        if pos_idx < len(positions) and j + 1 == positions[pos_idx]:
            out.append(sep)
            pos_idx += 1
    return "".join(out)


# =============================================================================
# Digit-substitution helpers
# =============================================================================


def _append_numeric_part(out: list[str], s: str, custom_digits: bool, digit_base_cp: int) -> None:
    """Appends a string of (possibly ASCII) digit characters to out,
    substituting them for the custom digit family when needed."""
    for rc in s:
        if "0" <= rc <= "9":
            out.append(chr(ord(rc) - ord("0") + digit_base_cp) if custom_digits else rc)
        else:
            out.append(rc)


def _is_mandatory_digit(c: str, zero_digit: str) -> bool:
    """True if c is a mandatory-digit placeholder in the Unicode decimal
    digit family containing zero_digit."""
    base = _digit_base(zero_digit)
    return base <= ord(c) <= base + 9


def _digit_base(zero_digit: str) -> int:
    """Returns the codepoint of the "zero" character for the digit family
    that zero_digit belongs to."""
    c = zero_digit
    if c.isdigit():
        try:
            v = int(c)
        except ValueError:
            v = -1
        if 0 <= v <= 9:
            return ord(c) - v
    return ord(zero_digit)
