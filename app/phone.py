"""Phone numbers: a country code plus digits only. (Sign-in uses email and password; no codes are sent.)"""
from __future__ import annotations

import re

MOBILE_RX = re.compile(r"^[6-9]\d{9}$")


# Dial codes offered on the forms. India first and the default; the number is stored without the code, the code in its own column.
COUNTRIES = [("+91", "India"), ("+971", "UAE"), ("+1", "US / Canada"), ("+44", "UK"), ("+65", "Singapore"),
             ("+966", "Saudi Arabia"), ("+974", "Qatar"), ("+968", "Oman"), ("+973", "Bahrain"), ("+965", "Kuwait"), ("+61", "Australia"),
             ("+64", "New Zealand"), ("+977", "Nepal"), ("+880", "Bangladesh"), ("+94", "Sri Lanka"), ("+49", "Germany"), ("+33", "France")]
_CODES = {c for c, _ in COUNTRIES}


class PhoneError(ValueError):
    pass


def parse_phone(country: str, raw: str) -> tuple[str, str]:
    """(country code, number) from a form. The number is digits only: spaces, dashes and brackets are dropped, so "98765 43210" and
    "9876543210" are the same number. Indian numbers are exactly 10 digits starting 6 to 9; other countries 6 to 14 digits. A number typed with
    a leading + and a known code (for example +971 50 123 4567) overrides the dropdown."""
    code = (country or "+91").strip()
    if not code.startswith("+"):
        code = "+" + code
    if code not in _CODES:
        raise PhoneError("Choose the country code from the list.")
    txt = (raw or "").strip()
    digits = re.sub(r"\D", "", txt)
    if txt.startswith("+"):
        known = sorted(_CODES, key=len, reverse=True)
        hit = next((c for c in known if digits.startswith(c[1:])), None)
        if not hit:
            raise PhoneError("That country code is not supported. Choose a code from the list and type the number without it.")
        code, digits = hit, digits[len(hit) - 1:]
    if code == "+91":
        if len(digits) == 12 and digits.startswith("91"):
            digits = digits[2:]
        elif len(digits) == 11 and digits.startswith("0"):
            digits = digits[1:]
        if not MOBILE_RX.match(digits):
            raise PhoneError("Enter a valid 10-digit Indian mobile number.")
    else:
        digits = digits.lstrip("0") if digits.startswith("0") and len(digits) > 7 else digits
        if not 6 <= len(digits) <= 14:
            raise PhoneError("Enter a valid mobile number for that country: 6 to 14 digits, without the country code.")
    return code, digits
