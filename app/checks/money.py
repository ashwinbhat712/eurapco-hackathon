"""EUR amounts as Decimal cents. Strict: an ambiguous or malformed amount is None, never a guess."""

import re
from decimal import ROUND_HALF_UP, Decimal

CENT = Decimal("0.01")
ZERO = Decimal("0.00")
_NOISE = re.compile(r"(?i)\s|euros?|eur|€")

# Accepted shapes and how to turn each into a plain decimal string. Anything else is refused, including a
# single thousands group without decimals ("1.925", "1,925"): it reads as 1925 or as a 3-decimal amount.
_SHAPES = (
    (re.compile(r"^\d+(\.\d{1,2})?$"), lambda text: text),                                # 1925 / 1925.5
    (re.compile(r"^\d+,\d{1,2}$"), lambda text: text.replace(",", ".")),                   # 1925,00
    (re.compile(r"^\d{1,3}(,\d{3})+\.\d{1,2}$"), lambda text: text.replace(",", "")),       # 1,925.00
    (re.compile(r"^\d{1,3}(\.\d{3})+,\d{1,2}$"), lambda text: text.replace(".", "").replace(",", ".")),  # 1.925,00
    (re.compile(r"^\d{1,3}(,\d{3}){2,}$"), lambda text: text.replace(",", "")),            # 1,925,000
    (re.compile(r"^\d{1,3}(\.\d{3}){2,}$"), lambda text: text.replace(".", "")),           # 1.925.000
)


def parse_money(value):
    """A non-negative EUR amount with at most two decimals, as Decimal cents; None if unreadable."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, Decimal)):
        text = str(value)
    elif isinstance(value, float):
        text = repr(value)
    else:
        text = _NOISE.sub("", str(value))
    plain = next((convert(text) for pattern, convert in _SHAPES if pattern.match(text)), None)
    return Decimal(plain).quantize(CENT) if plain is not None else None


def format_money(amount):
    """The tools' Money format: "1234.50"."""
    return str(amount.quantize(CENT, rounding=ROUND_HALF_UP))


def round_cents(amount):
    return amount.quantize(CENT, rounding=ROUND_HALF_UP)
