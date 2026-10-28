"""EUR amounts as Decimal cents. Strict: an ambiguous or malformed amount is None, never a guess."""

import re
from decimal import ROUND_HALF_UP, Decimal

class Money:
    """Parse, round and format amounts. Every method is stateless; the class only groups them."""

    CENT = Decimal("0.01")
    ZERO = Decimal("0.00")
    CURRENCY = "EUR"  # contracts/tools.schema.json Currency: the only one.
    _NOISE = re.compile(r"(?i)\s|euros?|eur|€")
    # Accepted shapes and how to turn each into a plain decimal string. Anything else is refused, including a
    # single thousands group without decimals ("1.925", "1,925"): it reads as 1925 or as a 3-decimal amount.
    _SHAPES = (
        (re.compile(r"^\d+(\.\d{1,2})?$"), lambda text: text),                                # 1925 / 1925.5
        (re.compile(r"^\d+,\d{1,2}$"), lambda text: text.replace(",", ".")),                   # 1925,00
        (re.compile(r"^\d{1,3}(,\d{3})+\.\d{1,2}$"), lambda text: text.replace(",", "")),       # 1,925.00
        (re.compile(r"^\d{1,3}(\.\d{3})+,\d{1,2}$"), lambda text: text.replace(".", "").replace(",", ".")),
        (re.compile(r"^\d{1,3}(,\d{3}){2,}$"), lambda text: text.replace(",", "")),            # 1,925,000
        (re.compile(r"^\d{1,3}(\.\d{3}){2,}$"), lambda text: text.replace(".", "")),           # 1.925.000
    )
    # Input type -> its text form. bool and None are refused before this lookup; floats use repr so that
    # binary noise (0.1 + 0.2 -> 0.30000000000000004) is refused instead of silently rounded.
    _TEXT = {int: str, Decimal: str, float: repr}

    @classmethod
    def parse(cls, value):
        """A non-negative EUR amount with at most two decimals, as Decimal cents; None if unreadable."""
        if isinstance(value, bool) or value is None:
            return None
        text = cls._TEXT.get(type(value), lambda raw: cls._NOISE.sub("", str(raw)))(value)
        plain = next((convert(text) for pattern, convert in cls._SHAPES if pattern.match(text)), None)
        return Decimal(plain).quantize(cls.CENT) if plain is not None else None

    @classmethod
    def round(cls, amount):
        return amount.quantize(cls.CENT, rounding=ROUND_HALF_UP)

    @classmethod
    def format(cls, amount):
        """The tools' Money format: "1234.50"."""
        return str(cls.round(amount))

    @classmethod
    def with_currency(cls, amount):
        """For people: "EUR 1234.50"."""
        return f"{cls.CURRENCY} {cls.format(amount)}"
