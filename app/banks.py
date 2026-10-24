"""Banks covered at launch (business plan: top 15 banks, 3+ statement layouts each).

`layouts` lists the statement layouts exercised by the automated tests for that bank. They are built from
each bank's published column format with fictional data, not from real customer statements. A file that does not
match a known layout is rejected before any charge.
"""
from __future__ import annotations

_COMMON = ("blank_cells", "dr_cr_suffix", "zero_cells")

# (display name, IFSC prefix, extra bank-specific layouts)
_BANKS = [
    ("State Bank of India", "SBIN", ("sbi",)),
    ("HDFC Bank", "HDFC", ("hdfc",)),
    ("ICICI Bank", "ICIC", ()),
    ("Axis Bank", "UTIB", ()),
    ("Kotak Mahindra Bank", "KKBK", ("kotak",)),
    ("Punjab National Bank", "PUNB", ()),
    ("Bank of Baroda", "BARB", ()),
    ("Canara Bank", "CNRB", ()),
    ("Union Bank of India", "UBIN", ()),
    ("Bank of India", "BKID", ()),
    ("IndusInd Bank", "INDB", ()),
    ("Yes Bank", "YESB", ()),
    ("IDFC FIRST Bank", "IDFB", ()),
    ("Indian Bank", "IDIB", ()),
    ("IDBI Bank", "IBKL", ()),
]

SUPPORTED_BANKS = [{"name": n, "ifsc": p, "layouts": _COMMON + extra} for n, p, extra in _BANKS]
BANK_COUNT = len(SUPPORTED_BANKS)
