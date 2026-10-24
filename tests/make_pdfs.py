"""Builds test PDFs: fully synthetic statements in several bank-like layouts, plus
non-statement files the validator must reject. Used by the tests and by scripts/demo_data.py."""
from __future__ import annotations

import io
import random
from datetime import date, timedelta
from pathlib import Path

import pikepdf
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

SMALL = ParagraphStyle("s", fontName="Helvetica", fontSize=7, leading=8.5)


def inr(x: float) -> str:
    return f"{x:,.2f}"


# ---------------------------------------------------------------------------------------
# synthetic ledgers (fictional parties only)
# ---------------------------------------------------------------------------------------
def synthetic_ledger(months: int = 6, start=date(2026, 4, 1), opening=125000.0, seed=7, salaried=False):
    rnd = random.Random(seed)
    rows, bal, d = [], opening, start
    end = start + timedelta(days=30 * months)
    customers = ["ASHOK TRADERS", "SUNRISE STORES", "METRO GARMENTS", "GREEN LEAF FOODS", "KRISHNA AGENCIES", "ROYAL FOOTWEAR"]
    suppliers = ["PRIME TEXTILES", "ALPHA PACKAGING", "NOVA LOGISTICS", "VISION POLYMERS"]
    n = 0
    while d < end:
        day_events = []
        if salaried and d.day == 1:
            day_events.append(("C", 62000 + rnd.choice([0, 0, 500]), f"NEFT-HDFCN{rnd.randint(10**9, 10**10-1)}-ACME SOLUTIONS PVT LTD SALARY"))
        if not salaried:
            for _ in range(rnd.choice([0, 1, 1, 2, 3])):
                amt = rnd.choice([12500, 18400, 25000, 33200, 41750, 58900, 76000, 91250])
                kind = rnd.choice(["UPI", "NEFT", "IMPS", "CHQ"])
                cust = rnd.choice(customers)
                if kind == "UPI":
                    day_events.append(("C", amt, f"UPI/{rnd.randint(10**11, 10**12-1)}/{rnd.randint(100000, 235959)}/UPI/{cust.lower().replace(' ', '')}@okaxis/payment"))
                elif kind == "NEFT":
                    day_events.append(("C", amt, f"NEFT-SBIN{rnd.randint(10**9, 10**10-1)}-{cust}"))
                elif kind == "IMPS":
                    day_events.append(("C", amt, f"IMPS/P2A/{rnd.randint(10**11, 10**12-1)}/{cust}/invoice"))
                else:
                    day_events.append(("C", amt, f"BY INST {rnd.randint(100000, 999999)} - MICR CLG CTS"))
            for _ in range(rnd.choice([0, 1, 1, 2])):
                amt = rnd.choice([8000, 15000, 22000, 36500, 48000, 61000])
                day_events.append(("D", amt, f"NEFT-ICIC{rnd.randint(10**9, 10**10-1)}-{rnd.choice(suppliers)}"))
        if rnd.random() < 0.25:
            day_events.append(("D", rnd.choice([2000, 5000, 10000, 15000]), f"ATM/CASH/{rnd.randint(1000, 9999)}/XXXXXXXXXXXX6696"))
        if d.day == 5:
            day_events.append(("D", 18500, "ECS/EMI/HOMEFIN LOAN 4471"))
        if d.day == 20:
            day_events.append(("D", 4200 + rnd.choice([0, 600]), "NEFT-BARB26-GST-RESERVE BANK OF INDIA"))
        if d.day == 28:
            day_events.append(("D", 31.0, "CHARGES FOR SMS ALERTS"))
        for kind, amt, narr in day_events:
            if kind == "D" and bal - amt < 2000:
                continue
            bal = round(bal + amt if kind == "C" else bal - amt, 2)
            n += 1
            rows.append({"date": d, "narration": narr, "amount": float(amt), "dir": kind, "balance": bal})
        d += timedelta(days=1)
    return rows, opening


def ledger_from_perfios_txt(path: str):
    """Rebuilds a ledger from the Xns1 sheet of the user's sample (kept out of the shipped package)."""
    txt = Path(path).read_text(encoding="utf-8", errors="ignore").splitlines()
    out, on = [], False
    for ln in txt:
        if ln.startswith("## Sheet: Xns1"):
            on = True
            continue
        if on and ln.startswith("## Sheet:"):
            break
        if on and ln.strip() and not ln.startswith("Sl. No."):
            p = ln.split("\t")
            if len(p) < 7:
                continue
            serial = float(p[1])
            d = date(1899, 12, 30) + timedelta(days=int(serial))
            amt = float(p[4])
            out.append({"date": d, "narration": p[3].strip(), "amount": abs(amt), "dir": "C" if amt > 0 else "D",
                        "balance": float(p[6]), "cheque": p[2].strip().rstrip(".0") if p[2].strip() not in ("", "0.0") else ""})
    first = out[0]
    opening = round(first["balance"] - first["amount"] if first["dir"] == "C" else first["balance"] + first["amount"], 2)
    return out, opening


# ---------------------------------------------------------------------------------------
# rendering
# ---------------------------------------------------------------------------------------
LAYOUTS = ("blank_cells", "zero_cells", "dr_cr_suffix", "newest_first", "hdfc", "sbi", "kotak")


def render_statement(rows, opening, layout="blank_cells", bank="Bank of Baroda", holder="DEMO TRADERS",
                     account="12345678903324", ifsc="BARB0DEMO01", acct_type="Current", password=None,
                     producer=None, summary_credit_delta=0.0) -> bytes:
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, leftMargin=18, rightMargin=18, topMargin=24, bottomMargin=24)
    first, last = rows[0]["date"], rows[-1]["date"]
    fmt = ((lambda d: d.strftime("%d %b %Y")) if layout in ("dr_cr_suffix", "sbi")
           else (lambda d: d.strftime("%d-%m-%Y")) if layout == "kotak" else (lambda d: d.strftime("%d/%m/%Y")))
    el = [
        Paragraph(f"<b>{bank}</b>", ParagraphStyle("h", fontName="Helvetica-Bold", fontSize=13)),
        Paragraph("Statement of Account", ParagraphStyle("h2", fontName="Helvetica", fontSize=10)),
        Spacer(1, 6),
        Paragraph(f"Account Name : {holder}", SMALL),
        Paragraph(f"Account Number : {account}", SMALL),
        Paragraph(f"IFSC Code : {ifsc}", SMALL),
        Paragraph(f"Account Type : {acct_type} Account", SMALL),
        Paragraph(f"Statement Period : From {first.strftime('%d/%m/%Y')} to {last.strftime('%d/%m/%Y')}", SMALL),
    ] + ([] if layout == "hdfc" else [Paragraph(f"Opening Balance : {inr(opening)}", SMALL)]) + [Spacer(1, 8)]
    data_rows = list(rows)
    if layout == "newest_first":
        data_rows = list(reversed(rows))

    if layout == "blank_cells":
        head = ["Date", "Narration", "Chq No", "Withdrawal", "Deposit", "Balance"]
        body = [[fmt(r["date"]), Paragraph(r["narration"], SMALL), r.get("cheque", ""),
                 inr(r["amount"]) if r["dir"] == "D" else "", inr(r["amount"]) if r["dir"] == "C" else "",
                 inr(r["balance"])] for r in data_rows]
        widths = [52, 235, 40, 62, 62, 70]
    elif layout == "zero_cells":
        head = ["Txn Date", "Value Date", "Description", "Debit", "Credit", "Balance"]
        body = [[fmt(r["date"]), fmt(r["date"]), Paragraph(r["narration"], SMALL),
                 inr(r["amount"] if r["dir"] == "D" else 0), inr(r["amount"] if r["dir"] == "C" else 0),
                 inr(r["balance"])] for r in data_rows]
        widths = [50, 50, 200, 60, 60, 70]
    elif layout == "dr_cr_suffix":
        head = ["Date", "Particulars", "Amount", "Balance"]
        body = [[fmt(r["date"]), Paragraph(r["narration"], SMALL),
                 f"{inr(r['amount'])} {'Dr' if r['dir'] == 'D' else 'Cr'}", f"{inr(r['balance'])} Cr"] for r in data_rows]
        widths = [62, 300, 90, 90]
    elif layout == "sbi":
        head = ["Txn Date", "Value Date", "Description", "Ref No./Cheque No.", "Debit", "Credit", "Balance"]
        body = [[fmt(r["date"]), fmt(r["date"]), Paragraph(r["narration"], SMALL), "",
                 inr(r["amount"]) if r["dir"] == "D" else "", inr(r["amount"]) if r["dir"] == "C" else "", inr(r["balance"])] for r in data_rows]
        widths = [48, 48, 200, 60, 58, 58, 66]
    elif layout == "kotak":
        head = ["Date", "Narration", "Chq/Ref No", "Withdrawal (Dr)", "Deposit (Cr)", "Balance"]
        body = [[fmt(r["date"]), Paragraph(r["narration"], SMALL), "",
                 inr(r["amount"]) if r["dir"] == "D" else "", inr(r["amount"]) if r["dir"] == "C" else "", inr(r["balance"]) + "(Cr)"] for r in data_rows]
        widths = [52, 215, 60, 66, 66, 70]
    elif layout == "hdfc":
        head = ["Date", "Narration", "Chq./Ref.No.", "ValueDt", "WithdrawalAmt.", "DepositAmt.", "ClosingBalance"]
        body = [[fmt(r["date"]), Paragraph(r["narration"], SMALL), "000000" + str(r["idx"] if "idx" in r else 0), fmt(r["date"]),
                 inr(r["amount"]) if r["dir"] == "D" else "", inr(r["amount"]) if r["dir"] == "C" else "", inr(r["balance"])]
                for r in data_rows]
        widths = [44, 190, 62, 44, 60, 60, 66]
    else:  # newest_first, same columns as blank_cells
        head = ["Date", "Narration", "Chq No", "Withdrawal", "Deposit", "Balance"]
        body = [[fmt(r["date"]), Paragraph(r["narration"], SMALL), r.get("cheque", ""),
                 inr(r["amount"]) if r["dir"] == "D" else "", inr(r["amount"]) if r["dir"] == "C" else "",
                 inr(r["balance"])] for r in data_rows]
        widths = [52, 235, 40, 62, 62, 70]
    t = Table([head] + body, colWidths=widths, repeatRows=1)
    t.setStyle(TableStyle([("FONT", (0, 0), (-1, -1), "Helvetica", 7), ("FONT", (0, 0), (-1, 0), "Helvetica-Bold", 7),
                           ("GRID", (0, 0), (-1, -1), 0.2, colors.lightgrey), ("VALIGN", (0, 0), (-1, -1), "TOP")]))
    el.append(t)
    el.append(Spacer(1, 8))
    if layout == "hdfc":
        cr = [r for r in rows if r["dir"] == "C"]
        dr = [r for r in rows if r["dir"] == "D"]
        el += [Paragraph("STATEMENT SUMMARY :-", SMALL),
               Paragraph("Opening Balance Dr Count Cr Count Debits Credits Closing Bal", SMALL),
               Paragraph(f"{inr(opening)} {len(dr)} {len(cr)} {inr(sum(r['amount'] for r in dr))} "
                         f"{inr(sum(r['amount'] for r in cr) + summary_credit_delta)} {inr(rows[-1]['balance'])}", SMALL)]
    el.append(Paragraph("This is a computer generated statement and does not require a signature.", SMALL))

    def furniture(canvas, _doc):
        canvas.setFont("Helvetica", 6)
        for i, line in enumerate(["DEMO BANK LIMITED", "Contents of this statement will be considered correct if no error is reported within 30 days of receipt.",
                                  "Registered Office Address: Demo House, Mumbai 400013"]):
            canvas.drawString(18, 22 - i * 7, line)

    doc.build(el, onFirstPage=furniture, onLaterPages=furniture)
    return _finish(buf.getvalue(), password, producer)


def _finish(data: bytes, password: str | None, producer: str | None = None) -> bytes:
    if not password and not producer:
        return data
    out = io.BytesIO()
    with pikepdf.open(io.BytesIO(data)) as pdf:
        if producer:
            pdf.docinfo["/Producer"] = producer
        if password:
            pdf.save(out, encryption=pikepdf.Encryption(user=password, owner=password + "-owner"))
        else:
            pdf.save(out)
    return out.getvalue()


# ---------------------------------------------------------------------------------------
# files that must be rejected
# ---------------------------------------------------------------------------------------
def invoice_pdf() -> bytes:
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4)
    st = ParagraphStyle("x", fontName="Helvetica", fontSize=11)
    doc.build([Paragraph("TAX INVOICE", st), Paragraph("GSTIN 27ABCDE1234F1Z5", st),
               Paragraph("Invoice No INV-0042 dated 12/04/2026", st)]
              + [Paragraph(f"Item {i} widgets quantity {i} rate 1,200.00 amount {i*1200:,}.00", st) for i in range(1, 40)])
    return buf.getvalue()


def card_statement_pdf() -> bytes:
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4)
    st = ParagraphStyle("x", fontName="Helvetica", fontSize=10)
    lines = ["HDFC Bank Credit Card Statement", "Card Number XXXX XXXX XXXX 4411", "Total Amount Due 12,340.00",
             "Minimum Amount Due 1,234.00", "Payment Due Date 05/05/2026", "Credit Limit 150,000.00", "Reward Points 1,204"]
    lines += [f"{i:02d}/04/2026 SWIGGY ORDER {i} {i*130:,}.00 Dr" for i in range(1, 30)]
    doc.build([Paragraph(l, st) for l in lines])
    return buf.getvalue()


def scanned_pdf() -> bytes:
    """A PDF page containing only an image, so there is no text to read."""
    from PIL import Image
    from reportlab.pdfgen import canvas
    img = Image.new("RGB", (800, 1000), "white")
    ibuf = io.BytesIO()
    img.save(ibuf, "PNG")
    ibuf.seek(0)
    from reportlab.lib.utils import ImageReader
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    c.drawImage(ImageReader(ibuf), 0, 0, width=595, height=842)
    c.save()
    return buf.getvalue()


def plain_text_pdf(text="Hello world. This is a letter and not a bank statement. " * 40) -> bytes:
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4)
    doc.build([Paragraph(text, ParagraphStyle("x", fontName="Helvetica", fontSize=10))])
    return buf.getvalue()


def tampered_balance_statement(rows, opening, **kw) -> bytes:
    """Statement where many balances have been edited, so it cannot reconcile."""
    bad = [dict(r) for r in rows]
    for i in range(0, len(bad), 3):
        bad[i]["balance"] = round(bad[i]["balance"] + 1000, 2)
    return render_statement(bad, opening, **kw)
