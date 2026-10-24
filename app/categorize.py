"""Transaction enrichment: channel, category, counterparty, reversal / bounce pairing,
self-transfer detection and recurring-debit detection. Pure functions, no I/O.

Every row gets:  channel, category, party, reversal (bool), bounce (bool), self_xfer (bool),
                 recurring (bool), month ('YYYY-MM')
"""
from __future__ import annotations

import re
import statistics
from collections import defaultdict
from datetime import timedelta

from . import config

# ----------------------------------------------------------------------------- channel
_CHANNELS = [
    ("UPI", r"(^|[/\-\s])UPI([/\-\s]|$)|@(OK|YBL|PAYTM|IBL|AXL|SBI|HDFC|ICICI|APL|UPI)"),
    ("RTGS", r"\bRTGS\b"),
    ("NEFT", r"\bNEFT\b"),
    ("IMPS", r"\bIMPS\b"),
    ("CASH", r"\bATM\b|CASH (WDL|WITHDRAWAL|DEP)|\bNWD\b|\bCDM\b|BY CASH|CASH DEPOSIT|\bCASH\b"),
    ("CHEQUE", r"BY INST|\bCHQ\b|CHEQUE|\bCLG\b|\bMICR\b|\bCTS\b|\bINST\b"),
    ("CARD", r"CARDS? SETTL|TERMINAL|\bPOS\b|\bECOM\b|DEBIT CARD|\bVISA\b|\bRUPAY\b|\bMASTERCARD\b"),
    ("ECS", r"\bECS\b|\bNACH\b|\bACH\b|\bMANDATE\b|\bSI\b"),
]
CHANNEL_NAMES = ["UPI", "NEFT_RTGS", "IMPS", "CHEQUE", "CASH", "CARD", "ECS", "OTHER"]


def channel_of(narr: str) -> str:
    n = narr.upper()
    for name, rx in _CHANNELS:
        if re.search(rx, n):
            return "NEFT_RTGS" if name in ("NEFT", "RTGS") else name
    return "OTHER"


# ----------------------------------------------------------------------------- category
CHARGES_RX = re.compile(
    r"\bCHARGES?\b|\bSMS ALERT|\bANNUAL FEE|\bAMC\b|MIN(IMUM)? BAL|\bPENAL|GST ON|\b[CSI]GST\b|"
    r"SERVICE CHARGE|PROCESSING FEE|\bFEE\b|\bFEES\b|FOLIO CHG|\bCHGS?\b")
BOUNCE_CHARGE_RX = re.compile(r"INWARD RETURN|OUTWARD RETURN|RETURN CHARGE|CHQ RET|CHEQUE RET|ECS RET|NACH RET|DISHONOU?R")
TAX_RX = re.compile(r"\bGSTN?\b|\bCBIC\b|INCOME TAX|\bTDS\b|\bCBDT\b|ADVANCE TAX|NSDL|TAX PAYMENT|"
                    r"RESERVE BANK OF INDIA|GOVT OF INDIA|COMMERCIAL TAX|PROFESSIONAL TAX")
CARD_BILL_RX = re.compile(r"\bCRED\b|CREDIT CARD|\bCC (PAY|BILL)|CARD BILL|CARDS? PAYMENT")
CARD_SETTLE_RX = re.compile(r"CARDS? SETTL|TERMINAL|POS SETTL|MERCHANT SETTL|PAYU|RAZORPAY|CASHFREE|PINE ?LABS|EASEBUZZ|CCAVENUE|PAYTM SETTL|PHONEPE SETTL|BILLDESK SETTL")
EMI_RX = re.compile(r"\bEMI\b|\bNACH\b|\bECS\b|\bACH D\b|LOAN|INSTALMENT|INSTALLMENT|HOMEFIN|BAJAJ FIN|HDB FIN|FINSERV|"
                    r"CAPITAL FIRST|TATA CAPITAL|RELIANCE (CAPITAL|COMMERCIAL|HOME FIN)|\bLEASING|FINANCE LTD|\bFINANCE\b|MUTHOOT|MANAPPURAM|"
                    r"HOUSING FIN|FULLERTON|CHOLA|\bIIFL\b|LENDINGKART|CLIX|"
                    r"MAHINDRA FIN|SHRIRAM|L&T FIN|AXIS FINANCE|ADITYA BIRLA FIN|POONAWALLA|PIRAMAL")
SALARY_RX = re.compile(r"SALARY|\bSAL\b|PAYROLL|WAGES|STIPEND")
CASH_RX = re.compile(r"\bATM\b|CASH (WDL|WITHDRAWAL)|\bNWD\b|CASH DEP|\bCDM\b|BY CASH|CASH DEPOSIT|\bCASH\b")
INTEREST_CR_RX = re.compile(r"INT\.? ?PD|INTEREST (PAID|CREDIT)|INT CR|SB INT|CREDIT INTEREST|INT\.? ?ON")
INTEREST_DR_RX = re.compile(r"INT\.? ?COLL|INTEREST DEBIT|DEBIT INTEREST|OD INT|CC INT|INTEREST CHARGED")
UTILITY_RX = re.compile(r"ELECTRIC|BESCOM|MSEDCL|TATA POWER|\bBSES\b|WATER BILL|\bGAS\b|AIRTEL|\bJIO\b|VODAFONE|"
                        r"\bBSNL\b|BROADBAND|BILLDESK|\bBBPS\b|RECHARGE|\bDTH\b|TELECOM|MUNICIPAL|ELECTRICITY")
INSURANCE_RX = re.compile(r"\bLIC\b|INSURANCE|HDFC LIFE|ICICI PRU|PREMIUM|STAR HEALTH|\bNIVA\b|\bACKO\b|SBI LIFE|BAJAJ ALLIANZ")
RENT_RX = re.compile(r"\bRENT\b|LANDLORD|LEASE RENT")
INVEST_RX = re.compile(r"MUTUAL FUND|\bSIP\b|ZERODHA|GROWW|\bNSE\b|\bBSE\b|\bCAMS\b|\bKFIN|\bPPF\b|\bNPS\b|"
                       r"\bFD\b|\bRD\b|FIXED DEPOSIT|RECURRING DEPOSIT|SMALLCASE|UPSTOX")
REVERSAL_KW_RX = re.compile(r"\bREJECT|\bREVERSAL\b|\bREV\b|\bRETURN(ED)?\b|\bFAILED\b|\bUNPAID\b|\bREFUND(ED)?\b|\bBOUNCE")
BOUNCE_REASON_RX = re.compile(r"INSUFFICIENT|FUNDS|DRAWER|ALTERATION|SIGNATURE|STOP PAY|ACCOUNT CLOSED|EXCEEDS ARRANGEMENT|"
                              r"REFER TO DRAWER|PAYMENT STOPPED|UNPAID|DISHONOU?R|BOUNCE|ADVICE NOT RECEIVED|STALE")

CATEGORIES = [
    "REVERSAL", "BOUNCE_CHARGE", "CARD_SETTLEMENT", "CARD_BILL", "CHARGES", "GST_TAX", "LOAN_EMI", "SALARY", "CASH_WITHDRAWAL", "CASH_DEPOSIT",
    "INTEREST", "UTILITY", "INSURANCE", "RENT", "INVESTMENT", "REFUND", "SELF_TRANSFER", "BUSINESS_CREDIT",
    "BUSINESS_DEBIT",
]


def _norm_party(s: str) -> str | None:
    s = re.sub(r"\(.*?\)", " ", s)
    s = re.sub(r"[^A-Za-z& ]+", " ", s).upper()
    s = re.sub(r"\b(PVT|LTD|LIMITED|PRIVATE|THE|PAYMENT|PAYMENTS|INVOICE|TRANSFER|FROM|TO|FOR|ACH|UPI)\b", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    if len(s) < 3 or re.fullmatch(r"X+", s.replace(" ", "")) or s.startswith("EBANK"):
        return None  # masked names and net-banking reference codes are not counterparties
    return s[:30]


def party_of(narr: str, channel: str) -> str | None:
    n = narr.strip()
    u = n.upper()
    if re.search(r"^BY INST|MICR|\bCLG\b", u):
        return None  # cheque clearing never names the payer
    m = re.match(r"UPI-(.+?)\s+\d{8,}", n)            # HDFC style: UPI-NAME <ref> <date> ...
    if m:
        return _norm_party(m.group(1))
    m = re.match(r"\.?ACH D-?\s*(.+?)(?:-|\s\d{6,}|$)", n, re.I)   # ACH D- LENDER NAME-<umrn>
    if m:
        return _norm_party(m.group(1))
    m = re.search(r"([A-Za-z0-9._\-]+)@[A-Za-z]+", n)
    if channel == "UPI" and m:
        return _norm_party(m.group(1).replace(".", " ").replace("_", " ").replace("-", " "))
    if channel == "UPI":
        parts = [p for p in n.split("/") if p and not p.isdigit()]
        for p in parts[1:]:
            q = _norm_party(p)
            if q and q not in ("UPI", "PAYMENT"):
                return q
    if channel == "IMPS":
        parts = n.split("/")
        if len(parts) >= 4:
            return _norm_party(parts[3])
    if channel == "NEFT_RTGS":
        parts = re.split(r"-", re.sub(r"\(.*?\)", "", n))
        if len(parts) >= 3:
            return _norm_party(parts[2])
        if len(parts) == 2:
            return _norm_party(parts[1])
    if channel == "CHEQUE":
        return None
    return _norm_party(n)


def _holder_tokens(holder: str | None) -> list[str]:
    if not holder:
        return []
    return [t for t in re.sub(r"[^A-Za-z ]", " ", holder.upper()).split()
            if len(t) > 2 and t not in ("MR", "MRS", "MS", "SHRI", "SMT", "PVT", "LTD", "LIMITED", "PRIVATE", "AND", "THE",
                                        "TRADERS", "ENTERPRISES", "STORES", "AGENCIES")]


def is_self_transfer(narr: str, tokens: list[str]) -> bool:
    u = narr.upper()
    if re.search(r"\bSELF\b|OWN ACC|OWN A/C|\bTRF TO SELF", u):
        return True
    return len(tokens) >= 2 and sum(t in u for t in tokens) >= 2


# ----------------------------------------------------------------------------- reversal pairing
def pair_reversals(rows: list[dict]) -> None:
    """Marks pairs of (debit, credit) with identical amount that undo each other, so the credit is
    not counted as genuine income. A pair needs a reversal/reject keyword on at least one leg and
    sits within 7 days (same day for explicit REJECT legs)."""
    for r in rows:
        r["reversal"] = False
        r["bounce"] = False
        r["pair_id"] = None
    by_amt: dict[float, list[dict]] = defaultdict(list)
    for r in rows:
        by_amt[round(r["amount"], 2)].append(r)
    pid = 0
    for amt, grp in by_amt.items():
        if len(grp) < 2:
            continue
        creds = [r for r in grp if r["dir"] == "C" and REVERSAL_KW_RX.search(r["narration"].upper())]
        debs = [r for r in grp if r["dir"] == "D"]
        used = set()
        for c in sorted(creds, key=lambda x: x["idx"]):
            cand = [d for d in debs if d["idx"] not in used and abs((c["date"] - d["date"]).days) <= 7]
            # prefer debit that shares the reject reference / keyword, else nearest earlier
            same_ref = [d for d in cand if d["narration"][:14].upper() == c["narration"][:14].upper()]
            pick = (same_ref or sorted(cand, key=lambda d: (abs((c["date"] - d["date"]).days), abs(d["idx"] - c["idx"]))))[:1]
            if not pick:
                continue
            d = pick[0]
            used.add(d["idx"])
            pid += 1
            for x in (c, d):
                x["reversal"] = True
                x["pair_id"] = pid
            text = (c["narration"] + " " + d["narration"]).upper()
            if re.search(r"\bREJECT", text) and BOUNCE_REASON_RX.search(text):
                c["bounce"] = d["bounce"] = True
    # second pass: a debit and a credit of the same amount with the same narration within 3 days
    # (typical of a failed auto-debit that is debited and then credited back)
    key = lambda r: re.sub(r"[^A-Z]", "", r["narration"].upper())[:18]
    for amt, grp in by_amt.items():
        free_d = [r for r in grp if r["dir"] == "D" and not r["reversal"]]
        free_c = [r for r in grp if r["dir"] == "C" and not r["reversal"]]
        for c in free_c:
            for d in free_d:
                if d["reversal"] or not key(d) or key(d) != key(c) or abs((c["date"] - d["date"]).days) > 3:
                    continue
                if d["narration"].upper().startswith("UPI") or c["narration"].upper().startswith("UPI"):
                    continue  # two UPI payments with the same person are not a reversal; each has its own reference
                pid += 1
                for x in (c, d):
                    x["reversal"] = True
                    x["pair_id"] = pid
                if re.search(r"\bACH D\b|\bNACH\b|\bECS\b", (c["narration"] + d["narration"]).upper()):
                    c["bounce"] = d["bounce"] = True
                break


# ----------------------------------------------------------------------------- recurring debits
def detect_recurring(rows: list[dict]) -> list[dict]:
    groups: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        if r["dir"] == "D" and not r["reversal"] and r["party"] and r["category"] not in ("CHARGES", "CASH_WITHDRAWAL", "BOUNCE_CHARGE"):
            groups[r["party"]].append(r)
    out = []
    for party, g in groups.items():
        months = {x["month"] for x in g}
        if len(g) < 3 or len(months) < 3:
            continue
        amts = [x["amount"] for x in g]
        med = statistics.median(amts)
        if med <= 0 or (max(amts) - min(amts)) / med > 0.15:
            continue
        ds = sorted(x["date"] for x in g)
        gaps = [(b - a).days for a, b in zip(ds, ds[1:]) if (b - a).days > 0]
        if gaps and not (20 <= statistics.median(gaps) <= 40):
            continue
        for x in g:
            x["recurring"] = True
        out.append({"party": party, "count": len(g), "months": len(months), "median_amount": round(med, 2),
                    "total": round(sum(amts), 2), "category": g[0]["category"], "first": ds[0], "last": ds[-1]})
    return sorted(out, key=lambda x: -x["total"])


# ----------------------------------------------------------------------------- main entry
def annotate(rows: list[dict], holder: str | None = None) -> list[dict]:
    pair_reversals(rows)
    tokens = _holder_tokens(holder)
    for r in rows:
        narr = r["narration"]
        u = narr.upper()
        ch = channel_of(narr)
        r["channel"] = ch
        r["month"] = r["date"].strftime("%Y-%m")
        r["recurring"] = False
        r["self_xfer"] = False
        cr = r["dir"] == "C"
        if r["reversal"]:
            cat = "REVERSAL"
        elif BOUNCE_CHARGE_RX.search(u):
            cat = "BOUNCE_CHARGE"
        elif CHARGES_RX.search(u) and not cr:
            cat = "CHARGES"
        elif TAX_RX.search(u) and not cr:
            cat = "GST_TAX"
        elif cr and re.search(r"\bREFUND|CASHBACK|\bREVERSAL\b", u):
            cat = "REFUND"
        elif cr and INTEREST_CR_RX.search(u) and not re.search(r"NEFT|IMPS|UPI", u):
            cat = "INTEREST"
        elif not cr and INTEREST_DR_RX.search(u):
            cat = "CHARGES"
        elif cr and CARD_SETTLE_RX.search(u):
            cat = "CARD_SETTLEMENT"
        elif SALARY_RX.search(u):
            cat = "SALARY"
        elif CASH_RX.search(u) and ch == "CASH":
            cat = "CASH_DEPOSIT" if cr else "CASH_WITHDRAWAL"
        elif is_self_transfer(narr, tokens):
            cat = "SELF_TRANSFER"
            r["self_xfer"] = True
        elif not cr and CARD_BILL_RX.search(u):
            cat = "CARD_BILL"
        elif not cr and EMI_RX.search(u):
            cat = "LOAN_EMI"
        elif not cr and INSURANCE_RX.search(u):
            cat = "INSURANCE"
        elif not cr and RENT_RX.search(u):
            cat = "RENT"
        elif not cr and UTILITY_RX.search(u):
            cat = "UTILITY"
        elif not cr and INVEST_RX.search(u):
            cat = "INVESTMENT"
        else:
            cat = "BUSINESS_CREDIT" if cr else "BUSINESS_DEBIT"
        r["category"] = cat
        r["party"] = None if cat in ("CHARGES", "BOUNCE_CHARGE", "CASH_WITHDRAWAL", "CASH_DEPOSIT", "REVERSAL", "INTEREST", "CARD_SETTLEMENT") \
            else party_of(narr, ch)
    return rows


def round_trips(rows: list[dict]) -> list[dict]:
    """A large genuine credit followed within the window by debits of >= ratio of the amount."""
    out = []
    mn = config.ROUND_TRIP_MIN_AMOUNT
    win = timedelta(hours=config.ROUND_TRIP_WINDOW_HOURS)
    for c in rows:
        if c["dir"] != "C" or c["reversal"] or c["amount"] < mn or c["category"] in ("SALARY", "INTEREST", "SELF_TRANSFER"):
            continue
        out_amt = 0.0
        for d in rows:
            if d["idx"] <= c["idx"] or d["dir"] != "D" or d["reversal"]:
                continue
            if d["date"] - c["date"] > win:
                break
            if d["category"] in ("CHARGES", "BOUNCE_CHARGE", "GST_TAX", "LOAN_EMI"):
                continue
            out_amt += d["amount"]
        if out_amt >= config.ROUND_TRIP_OUT_RATIO * c["amount"]:
            out.append({"date": c["date"], "credit": c["amount"], "debited_within_window": round(out_amt, 2),
                        "party": c["party"], "narration": c["narration"][:80]})
    return out
