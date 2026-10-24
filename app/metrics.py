"""Metrics engine. One function computes every data point for any date window, and it is run once
per calendar month and once for the whole statement, so totals are recomputed from the raw
transactions and never produced by adding up monthly ratios.

Registry:  REGISTRY = [(key, group, label, unit, description, scope)]   scope 'win' = per month + total,
           'tot' = total only.
"""
from __future__ import annotations

import calendar
import statistics
from collections import defaultdict
from datetime import date, timedelta

from . import categorize as cz
from . import config

# ----------------------------------------------------------------------------- registry
REGISTRY: list[tuple] = []


def reg(key, group, label, unit, desc, scope="win"):
    REGISTRY.append((key, group, label, unit, desc, scope))


G1, G2, G3, G4, G5, G6, G7 = ("Credits (money in)", "Debits (money out)", "Balances", "Cash flow & activity",
                              "Obligations", "Risk indicators", "Statement-level")

# --- credits
reg("credit_gross", G1, "Total credits (as printed)", "INR", "Every credit in the statement, including reversals of the account holder's own debits.")
reg("credit_count", G1, "Credit transactions", "count", "Number of credit entries.")
reg("credit_reversal", G1, "Reversal / bounce credits", "INR", "Credits that reverse an earlier debit (returned cheques, failed payments). Excluded from income.")
reg("credit_adj", G1, "Adjusted credits", "INR", "Total credits minus reversal credits.")
reg("credit_adj_count", G1, "Adjusted credit transactions", "count", "Credit entries excluding reversals.")
reg("inflow_genuine", G1, "Net business inflow", "INR", "Adjusted credits excluding own-account transfers, refunds and bank interest.")
reg("credit_avg", G1, "Average credit size", "INR", "Adjusted credits divided by their count.")
reg("credit_max", G1, "Largest credit", "INR", "Biggest single adjusted credit.")
reg("credit_min", G1, "Smallest credit", "INR", "Smallest single adjusted credit.")
reg("credit_median", G1, "Median credit size", "INR", "Middle adjusted credit; less distorted by one-off large receipts.")
reg("credit_max_share", G1, "Largest credit as % of credits", "%", "Concentration in a single receipt.")
reg("credit_days", G1, "Days with a credit", "days", "Distinct dates on which money came in.")
reg("credit_parties", G1, "Distinct paying parties", "count", "Unique counterparties in net business inflow.")
reg("credit_top1_pct", G1, "Top payer share", "%", "Share of net business inflow from the largest payer.")
reg("credit_top5_pct", G1, "Top-5 payers share", "%", "Share of net business inflow from the five largest payers.")
reg("credit_top1_amt", G1, "Top payer amount", "INR", "Amount received from the biggest payer.")
reg("salary_credit", G1, "Salary credits", "INR", "Credits identified as salary / payroll.")
reg("salary_credit_count", G1, "Salary credit count", "count", "Number of salary credits.")
reg("interest_credit", G1, "Interest credited", "INR", "Savings / deposit interest from the bank.")
reg("refund_credit", G1, "Refunds & cashbacks", "INR", "Refund-type credits that are not income.")
reg("selfxfer_credit", G1, "Own-account transfers in", "INR", "Credits that appear to come from the account holder.")
reg("card_settlement_credit", G1, "Card / POS settlements", "INR", "Merchant acquirer settlements from card machines and payment gateways.")
reg("card_settlement_pct", G1, "Card settlements share of inflow", "%", "Card and gateway settlements as % of net business inflow.")
reg("cash_deposit", G1, "Cash deposited", "INR", "Cash deposits at branch / CDM.")
reg("cash_deposit_count", G1, "Cash deposit count", "count", "Number of cash deposits.")
reg("cash_deposit_hv_count", G1, "High-value cash deposits (>= 50,000)", "count", "Cash deposits of INR 50,000 or more.")
reg("credit_ge_1l_count", G1, "Credits of INR 1 lakh or more", "count", "Number of large credits.")
reg("credit_round_count", G1, "Round-amount credits", "count", "Credits that are exact multiples of 10,000 and at least 10,000.")
reg("credit_round_pct", G1, "Round-amount credits share", "%", "Round-amount credits as a share of adjusted credits by value.")
reg("credit_weekend_pct", G1, "Weekend credits share", "%", "Share of adjusted credits received on Saturday or Sunday.")
reg("credit_last3_pct", G1, "Month-end credits share", "%", "Share of adjusted credits in the last 3 days of the month (window dressing check).")
reg("credit_last3_max", G1, "Largest credit in last 3 days of month", "INR", "Biggest receipt in the last 3 days of a month.")
CH = {"UPI": "UPI", "NEFT_RTGS": "NEFT / RTGS", "IMPS": "IMPS", "CHEQUE": "Cheque", "CASH": "Cash", "CARD": "Card", "ECS": "ECS / NACH", "OTHER": "Other"}
for k, v in CH.items():
    reg(f"credit_ch_{k.lower()}", G1, f"Credits via {v}", "INR", f"Adjusted credits received through {v}.")
# --- debits
reg("debit_gross", G2, "Total debits (as printed)", "INR", "Every debit in the statement, including debits later reversed.")
reg("debit_count", G2, "Debit transactions", "count", "Number of debit entries.")
reg("debit_reversal", G2, "Debits later reversed", "INR", "Debits that were undone by a reversal credit.")
reg("debit_adj", G2, "Adjusted debits", "INR", "Total debits minus reversed debits.")
reg("debit_adj_count", G2, "Adjusted debit transactions", "count", "Debit entries excluding reversed ones.")
reg("debit_avg", G2, "Average debit size", "INR", "Adjusted debits divided by their count.")
reg("debit_max", G2, "Largest debit", "INR", "Biggest single adjusted debit.")
reg("debit_median", G2, "Median debit size", "INR", "Middle adjusted debit.")
reg("debit_days", G2, "Days with a debit", "days", "Distinct dates on which money went out.")
reg("debit_parties", G2, "Distinct paid parties", "count", "Unique counterparties paid.")
reg("debit_top1_pct", G2, "Top payee share", "%", "Share of payments (excluding bank charges/cash) going to the biggest payee.")
reg("debit_top5_pct", G2, "Top-5 payees share", "%", "Share of payments going to the five biggest payees.")
reg("debit_top1_amt", G2, "Top payee amount", "INR", "Amount paid to the biggest payee.")
reg("selfxfer_debit", G2, "Own-account transfers out", "INR", "Debits that appear to go to the account holder.")
for k, v in CH.items():
    reg(f"debit_ch_{k.lower()}", G2, f"Debits via {v}", "INR", f"Adjusted debits paid through {v}.")
reg("cash_withdrawal", G2, "Cash withdrawn", "INR", "ATM and counter cash withdrawals.")
reg("cash_withdrawal_count", G2, "Cash withdrawal count", "count", "Number of cash withdrawals.")
reg("utility_debit", G2, "Utilities & telecom", "INR", "Electricity, phone, broadband and similar bills.")
reg("insurance_debit", G2, "Insurance premiums", "INR", "Insurance premium payments.")
reg("rent_debit", G2, "Rent", "INR", "Payments identified as rent.")
reg("invest_debit", G2, "Investments", "INR", "Mutual funds, SIPs, deposits, brokerage.")
reg("salary_paid", G2, "Salary / payroll paid", "INR", "Debits identified as salary or payroll.")
reg("gst_tax_debit", G2, "GST & tax payments", "INR", "GST, income tax, TDS and government payments.")
reg("gst_tax_count", G2, "GST & tax payment count", "count", "Number of tax payments.")
reg("charges_debit", G2, "Bank charges & fees", "INR", "SMS, service, IMPS and other fees.")
reg("charges_count", G2, "Bank charge entries", "count", "Number of fee entries.")
reg("bounce_charge_debit", G2, "Return / bounce charges", "INR", "Penalty fees for returned cheques and mandates.")
reg("bounce_charge_count", G2, "Return charge entries", "count", "Number of return-charge entries.")
# --- balances
reg("bal_open", G3, "Opening balance", "INR", "Balance at the start of the period.")
reg("bal_close", G3, "Closing balance", "INR", "Balance at the end of the period.")
reg("bal_change", G3, "Balance change", "INR", "Closing minus opening balance.")
reg("bal_avg", G3, "Average daily balance", "INR", "Mean of end-of-day balances for every calendar day in the period (one definition, used everywhere).")
reg("bal_min", G3, "Lowest end-of-day balance", "INR", "Minimum end-of-day balance.")
reg("bal_max", G3, "Highest end-of-day balance", "INR", "Maximum end-of-day balance.")
reg("bal_median", G3, "Median end-of-day balance", "INR", "Middle end-of-day balance.")
reg("bal_sd", G3, "Balance standard deviation", "INR", "Spread of end-of-day balances.")
reg("bal_cv_pct", G3, "Balance volatility", "%", "Standard deviation divided by average daily balance.")
reg("bal_days_negative", G3, "Days overdrawn", "days", "Days on which the end-of-day balance was below zero.")
reg("bal_days_lt_10k", G3, "Days below INR 10,000", "days", "Days with a very thin balance.")
reg("bal_days_lt_50k", G3, "Days below INR 50,000", "days", "Days with a low balance.")
reg("bal_days_ge_1l", G3, "Days at INR 1 lakh or more", "days", "Days with a comfortable balance.")
for d in (5, 10, 15, 20, 25):
    reg(f"bal_on_{d}", G3, f"Balance on the {d}th", "INR", f"End-of-day balance on the {d}th (average across months in the total column).")
# --- flow & activity
reg("window_days", G4, "Days covered", "days", "Calendar days in the period.")
reg("txn_count", G4, "Total transactions", "count", "All entries in the period.")
reg("txn_per_day", G4, "Transactions per day", "ratio", "Entries divided by days covered.")
reg("active_days", G4, "Active days", "days", "Days with at least one entry.")
reg("active_days_pct", G4, "Active days share", "%", "Active days divided by days covered.")
reg("net_flow", G4, "Net cash flow", "INR", "Adjusted credits minus adjusted debits.")
reg("net_flow_pct", G4, "Net flow as % of credits", "%", "Net cash flow divided by adjusted credits.")
reg("credit_daily_avg", G4, "Average daily credit", "INR", "Adjusted credits divided by days covered.")
reg("debit_daily_avg", G4, "Average daily debit", "INR", "Adjusted debits divided by days covered.")
reg("credit_debit_ratio", G4, "Credit to debit ratio", "ratio", "Adjusted credits divided by adjusted debits.")
reg("debit_credit_pct", G4, "Debits as % of credits", "%", "How much of what comes in goes out.")
reg("same_day_turnover_pct", G4, "Same-day pass-through", "%", "Money that left the account on the day it arrived, as % of adjusted credits.")
reg("cheque_issued_count", G4, "Cheques issued", "count", "Debits through cheque clearing.")
reg("cheque_deposit_count", G4, "Cheques deposited", "count", "Credits through cheque clearing.")
# --- obligations
reg("card_bill_debit", G5, "Credit card bill payments", "INR", "Payments to credit card bills (including via CRED); not counted in EMIs.")
reg("emi_debit", G5, "Loan EMIs & mandates", "INR", "EMIs, NACH/ECS mandates and loan or lease repayments.")
reg("emi_count", G5, "EMI debit count", "count", "Number of EMI-type debits.")
reg("recurring_debit_count", G5, "Recurring debits", "count", "Debits that repeat at a similar amount every month.")
reg("recurring_debit_amount", G5, "Recurring debit amount", "INR", "Value of recurring debits.")
reg("fixed_obligation", G5, "Fixed obligations", "INR", "EMIs plus rent plus insurance.")
reg("fixed_obligation_pct", G5, "Fixed obligations % of inflow", "%", "Fixed obligations divided by net business inflow.")
reg("emi_to_inflow_pct", G5, "EMI % of inflow", "%", "EMIs divided by net business inflow.")
# --- risk
reg("bounce_count", G6, "Cheque / mandate bounces", "count", "Rejected items (pairs of debit and reversal with a reject reason).")
reg("bounce_amount", G6, "Bounced amount", "INR", "Total value of bounced items.")
reg("reversal_pairs", G6, "Reversal pairs", "count", "Debits fully undone by a reversal credit.")
reg("round_trip_count", G6, "Round-trip candidates", "count", "Large credits mostly paid out again within 48 hours.")
reg("round_trip_amount", G6, "Round-trip credit value", "INR", "Value of those credits.")
# --- total-only
reg("months_covered", G7, "Months covered", "ratio", "Statement days divided by 30.44.", "tot")
reg("statement_days", G7, "Statement days", "days", "First to last transaction date, inclusive.", "tot")
reg("avg_monthly_credit", G7, "Average monthly adjusted credits", "INR", "Adjusted credits divided by months covered.", "tot")
reg("avg_monthly_inflow", G7, "Average monthly net inflow", "INR", "Net business inflow divided by months covered.", "tot")
reg("avg_monthly_debit", G7, "Average monthly adjusted debits", "INR", "Adjusted debits divided by months covered.", "tot")
reg("credit_best_month", G7, "Best month (adjusted credits)", "INR", "Highest monthly adjusted credits among full months.", "tot")
reg("credit_worst_month", G7, "Weakest month (adjusted credits)", "INR", "Lowest monthly adjusted credits among full months.", "tot")
reg("credit_cv_pct", G7, "Monthly credit volatility", "%", "Standard deviation of monthly adjusted credits divided by the mean.", "tot")
reg("credit_trend_pct", G7, "Credit trend", "%", "Average of the last 3 full months versus the first 3, as % change.", "tot")
reg("est_monthly_income", G7, "Estimated monthly income", "INR", "Average salary credits if salaried, otherwise median monthly net business inflow.", "tot")
reg("turnover_annualised", G7, "Annualised turnover", "INR", "Average monthly net inflow times 12.", "tot")
reg("abb_months_avg", G7, "Average of monthly average balances", "INR", "Mean across full months of each month's average daily balance.", "tot")
reg("foir_pct", G7, "FOIR", "%", "EMIs divided by estimated monthly income (per month).", "tot")
reg("months_with_bounce", G7, "Months with a bounce", "count", "Calendar months containing at least one bounce.", "tot")
reg("months_negative_flow", G7, "Months with negative net flow", "count", "Months where debits exceeded credits.", "tot")
reg("reversal_inflation_pct", G7, "Reversal credits share", "%", "Reversal credits as a percentage of total credits as printed.", "tot")
reg("recurring_parties", G7, "Recurring payee count", "count", "Distinct payees with recurring debits.", "tot")
reg("recon_break_count", G7, "Balance mismatches", "count", "Rows where the printed balance does not follow from the previous one.", "tot")
reg("recon_break_pct", G7, "Balance mismatch rate", "%", "Mismatching rows as a share of rows checked.", "tot")
reg("doc_flag_count", G7, "PDF integrity flags", "count", "Signals from the PDF file itself (editing software, modified after creation).", "tot")
reg("risk_score", G7, "Risk score (0-100)", "count", "Higher is riskier. Built from the flags on the Flags sheet.", "tot")

WIN_KEYS = [r[0] for r in REGISTRY if r[5] == "win"]
ALL_KEYS = [r[0] for r in REGISTRY]


# ----------------------------------------------------------------------------- helpers
def _days(a: date, b: date):
    d = a
    while d <= b:
        yield d
        d += timedelta(days=1)


def _pct(a, b):
    return round(a / b * 100, 2) if b else 0.0


def _med(xs):
    return round(statistics.median(xs), 2) if xs else 0.0


def _sd(xs):
    return round(statistics.pstdev(xs), 2) if len(xs) > 1 else 0.0


def _r(x):
    return round(x, 2)


def build_eod(rows: list[dict], opening: float, start: date, end: date) -> dict:
    last = {}
    for r in rows:
        last[r["date"]] = r["balance"]
    eod, cur = {}, opening
    for d in _days(start, end):
        cur = last.get(d, cur)
        eod[d] = cur
    return eod


def _party_stats(rows, key_amount_total):
    agg = defaultdict(float)
    for r in rows:
        if r["party"]:
            agg[r["party"]] += r["amount"]
    ranked = sorted(agg.values(), reverse=True)
    top1 = ranked[0] if ranked else 0.0
    top5 = sum(ranked[:5])
    return len(agg), top1, top5


# ----------------------------------------------------------------------------- window calc
def calc_window(rows: list[dict], eod: dict, start: date, end: date, stmt_opening: float, stmt_start: date) -> dict:
    w = [r for r in rows if start <= r["date"] <= end]
    ndays = (end - start).days + 1
    cr = [r for r in w if r["dir"] == "C"]
    dr = [r for r in w if r["dir"] == "D"]
    acr = [r for r in cr if not r["reversal"]]
    adr = [r for r in dr if not r["reversal"]]
    s = lambda xs: _r(sum(r["amount"] for r in xs))
    cat = lambda xs, *c: [r for r in xs if r["category"] in c]
    m: dict = {}

    m["credit_gross"], m["credit_count"] = s(cr), len(cr)
    m["credit_reversal"] = s([r for r in cr if r["reversal"]])
    m["credit_adj"], m["credit_adj_count"] = s(acr), len(acr)
    gen = [r for r in acr if r["category"] not in ("SELF_TRANSFER", "REFUND", "INTEREST")]
    m["inflow_genuine"] = s(gen)
    amts = [r["amount"] for r in acr]
    m["credit_avg"] = _r(sum(amts) / len(amts)) if amts else 0.0
    m["credit_max"], m["credit_min"], m["credit_median"] = (max(amts) if amts else 0.0), (min(amts) if amts else 0.0), _med(amts)
    m["credit_max_share"] = _pct(m["credit_max"], m["credit_adj"])
    m["credit_days"] = len({r["date"] for r in acr})
    n, t1, t5 = _party_stats(gen, None)
    m["credit_parties"], m["credit_top1_amt"] = n, _r(t1)
    m["credit_top1_pct"], m["credit_top5_pct"] = _pct(t1, m["inflow_genuine"]), _pct(t5, m["inflow_genuine"])
    m["card_settlement_credit"] = s(cat(acr, "CARD_SETTLEMENT"))
    m["card_settlement_pct"] = _pct(m["card_settlement_credit"], m["inflow_genuine"])
    m["salary_credit"], m["salary_credit_count"] = s(cat(acr, "SALARY")), len(cat(acr, "SALARY"))
    m["interest_credit"], m["refund_credit"] = s(cat(acr, "INTEREST")), s(cat(acr, "REFUND"))
    m["selfxfer_credit"] = s(cat(acr, "SELF_TRANSFER"))
    cd = cat(acr, "CASH_DEPOSIT")
    m["cash_deposit"], m["cash_deposit_count"] = s(cd), len(cd)
    m["cash_deposit_hv_count"] = sum(1 for r in cd if r["amount"] >= 50000)
    m["credit_ge_1l_count"] = sum(1 for r in acr if r["amount"] >= 100000)
    rnd = [r for r in acr if r["amount"] >= 10000 and r["amount"] % 10000 == 0]
    m["credit_round_count"], m["credit_round_pct"] = len(rnd), _pct(sum(r["amount"] for r in rnd), m["credit_adj"])
    m["credit_weekend_pct"] = _pct(sum(r["amount"] for r in acr if r["date"].weekday() >= 5), m["credit_adj"])
    last3 = [r for r in acr if r["date"].day >= calendar.monthrange(r["date"].year, r["date"].month)[1] - 2]
    m["credit_last3_pct"], m["credit_last3_max"] = _pct(sum(r["amount"] for r in last3), m["credit_adj"]), max([r["amount"] for r in last3], default=0.0)
    for k in CH:
        m[f"credit_ch_{k.lower()}"] = s([r for r in acr if r["channel"] == k])

    m["debit_gross"], m["debit_count"] = s(dr), len(dr)
    m["debit_reversal"] = s([r for r in dr if r["reversal"]])
    m["debit_adj"], m["debit_adj_count"] = s(adr), len(adr)
    damts = [r["amount"] for r in adr]
    m["debit_avg"] = _r(sum(damts) / len(damts)) if damts else 0.0
    m["debit_max"], m["debit_median"] = (max(damts) if damts else 0.0), _med(damts)
    m["debit_days"] = len({r["date"] for r in adr})
    pay = [r for r in adr if r["category"] not in ("CHARGES", "BOUNCE_CHARGE", "CASH_WITHDRAWAL", "SELF_TRANSFER")]
    n, t1, t5 = _party_stats(pay, None)
    tot_pay = sum(r["amount"] for r in pay)
    m["debit_parties"], m["debit_top1_amt"] = n, _r(t1)
    m["debit_top1_pct"], m["debit_top5_pct"] = _pct(t1, tot_pay), _pct(t5, tot_pay)
    m["selfxfer_debit"] = s(cat(adr, "SELF_TRANSFER"))
    for k in CH:
        m[f"debit_ch_{k.lower()}"] = s([r for r in adr if r["channel"] == k])
    cw = cat(adr, "CASH_WITHDRAWAL")
    m["cash_withdrawal"], m["cash_withdrawal_count"] = s(cw), len(cw)
    m["utility_debit"], m["insurance_debit"] = s(cat(adr, "UTILITY")), s(cat(adr, "INSURANCE"))
    m["rent_debit"], m["invest_debit"] = s(cat(adr, "RENT")), s(cat(adr, "INVESTMENT"))
    m["salary_paid"] = s(cat(adr, "SALARY"))
    tx = cat(adr, "GST_TAX")
    m["gst_tax_debit"], m["gst_tax_count"] = s(tx), len(tx)
    ch = cat(adr, "CHARGES")
    m["charges_debit"], m["charges_count"] = s(ch), len(ch)
    bc = cat(adr, "BOUNCE_CHARGE")
    m["bounce_charge_debit"], m["bounce_charge_count"] = s(bc), len(bc)

    eods = [eod[d] for d in _days(start, end)]
    before = start - timedelta(days=1)
    m["bal_open"] = _r(eod[before] if before in eod else stmt_opening)
    m["bal_close"] = _r(eods[-1])
    m["bal_change"] = _r(m["bal_close"] - m["bal_open"])
    avg = sum(eods) / len(eods)
    m["bal_avg"], m["bal_min"], m["bal_max"] = _r(avg), _r(min(eods)), _r(max(eods))
    m["bal_median"], m["bal_sd"] = _med(eods), _sd(eods)
    m["bal_cv_pct"] = _pct(m["bal_sd"], avg) if avg > 0 else 0.0
    m["bal_days_negative"] = sum(1 for b in eods if b < 0)
    m["bal_days_lt_10k"] = sum(1 for b in eods if b < 10000)
    m["bal_days_lt_50k"] = sum(1 for b in eods if b < 50000)
    m["bal_days_ge_1l"] = sum(1 for b in eods if b >= 100000)
    for dd in (5, 10, 15, 20, 25):
        vals = [eod[d] for d in _days(start, end) if d.day == dd]
        m[f"bal_on_{dd}"] = _r(sum(vals) / len(vals)) if vals else None

    m["window_days"], m["txn_count"] = ndays, len(w)
    m["txn_per_day"] = round(len(w) / ndays, 2)
    m["active_days"] = len({r["date"] for r in w})
    m["active_days_pct"] = _pct(m["active_days"], ndays)
    m["net_flow"] = _r(m["credit_adj"] - m["debit_adj"])
    m["net_flow_pct"] = _pct(m["net_flow"], m["credit_adj"])
    m["credit_daily_avg"], m["debit_daily_avg"] = _r(m["credit_adj"] / ndays), _r(m["debit_adj"] / ndays)
    m["credit_debit_ratio"] = round(m["credit_adj"] / m["debit_adj"], 2) if m["debit_adj"] else 0.0
    m["debit_credit_pct"] = _pct(m["debit_adj"], m["credit_adj"])
    per_day_c, per_day_d = defaultdict(float), defaultdict(float)
    for r in gen:
        per_day_c[r["date"]] += r["amount"]
    for r in adr:
        if r["category"] not in ("CHARGES", "BOUNCE_CHARGE"):
            per_day_d[r["date"]] += r["amount"]
    m["same_day_turnover_pct"] = _pct(sum(min(v, per_day_d.get(d, 0)) for d, v in per_day_c.items()), m["credit_adj"])
    m["cheque_issued_count"] = sum(1 for r in adr if r["channel"] == "CHEQUE")
    m["cheque_deposit_count"] = sum(1 for r in acr if r["channel"] == "CHEQUE")

    em = cat(adr, "LOAN_EMI")
    m["emi_debit"], m["emi_count"] = s(em), len(em)
    m["card_bill_debit"] = s(cat(adr, "CARD_BILL"))
    rec = [r for r in adr if r["recurring"]]
    m["recurring_debit_count"], m["recurring_debit_amount"] = len(rec), s(rec)
    m["fixed_obligation"] = _r(m["emi_debit"] + m["rent_debit"] + m["insurance_debit"])
    m["fixed_obligation_pct"] = _pct(m["fixed_obligation"], m["inflow_genuine"])
    m["emi_to_inflow_pct"] = _pct(m["emi_debit"], m["inflow_genuine"])

    bnc = [r for r in w if r["bounce"] and r["dir"] == "D"]
    m["bounce_count"], m["bounce_amount"] = len(bnc), s(bnc)
    m["reversal_pairs"] = len({r["pair_id"] for r in w if r["pair_id"]})
    return m


# ----------------------------------------------------------------------------- statement level
def month_windows(first: date, last: date):
    y, mth = first.year, first.month
    out = []
    while (y, mth) <= (last.year, last.month):
        ms = date(y, mth, 1)
        me = date(y, mth, calendar.monthrange(y, mth)[1])
        out.append((f"{y}-{mth:02d}", max(ms, first), min(me, last), (min(me, last) - max(ms, first)).days + 1 >= (me - ms).days + 1))
        mth += 1
        if mth == 13:
            y, mth = y + 1, 1
    return out


def analyze(parsed) -> dict:
    rows = parsed.rows
    meta = dict(parsed.meta)
    cz.annotate(rows, meta.get("holder"))
    recurring = cz.detect_recurring(rows)
    trips = cz.round_trips(rows)
    first, last = rows[0]["date"], rows[-1]["date"]
    opening = parsed.opening_balance if parsed.opening_balance is not None else (
        rows[0]["balance"] - rows[0]["amount"] if rows[0]["dir"] == "C" else rows[0]["balance"] + rows[0]["amount"])
    eod = build_eod(rows, opening, first, last)
    trip_dates = defaultdict(list)
    for t in trips:
        trip_dates[t["date"]].append(t)

    wins = month_windows(first, last)
    monthly, partial = {}, {}
    for key, a, b, full in wins:
        mm = calc_window(rows, eod, a, b, opening, first)
        tr = [t for t in trips if a <= t["date"] <= b]
        mm["round_trip_count"], mm["round_trip_amount"] = len(tr), _r(sum(t["credit"] for t in tr))
        monthly[key] = mm
        partial[key] = not full
    total = calc_window(rows, eod, first, last, opening, first)
    total["round_trip_count"], total["round_trip_amount"] = len(trips), _r(sum(t["credit"] for t in trips))
    # average-of-same-day balances across months for the total column
    for dd in (5, 10, 15, 20, 25):
        vals = [m_[f"bal_on_{dd}"] for m_ in monthly.values() if m_[f"bal_on_{dd}"] is not None]
        total[f"bal_on_{dd}"] = _r(sum(vals) / len(vals)) if vals else None

    days = total["window_days"]
    months_eq = max(days / 30.4375, 0.1)
    full_keys = [k for k in monthly if not partial[k]] or list(monthly)
    mc = [monthly[k]["credit_adj"] for k in full_keys]
    total["statement_days"], total["months_covered"] = days, round(months_eq, 2)
    total["avg_monthly_credit"] = _r(total["credit_adj"] / months_eq)
    total["avg_monthly_inflow"] = _r(total["inflow_genuine"] / months_eq)
    total["avg_monthly_debit"] = _r(total["debit_adj"] / months_eq)
    total["credit_best_month"], total["credit_worst_month"] = (max(mc) if mc else 0.0), (min(mc) if mc else 0.0)
    total["credit_cv_pct"] = _pct(_sd(mc), sum(mc) / len(mc)) if len(mc) > 1 and sum(mc) else 0.0
    if len(mc) >= 4:
        n3 = min(3, len(mc) // 2)
        a_, b_ = sum(mc[:n3]) / n3, sum(mc[-n3:]) / n3
        total["credit_trend_pct"] = _pct(b_ - a_, a_)
    else:
        total["credit_trend_pct"] = 0.0
    if total["salary_credit"] > 0:
        total["est_monthly_income"] = _r(total["salary_credit"] / months_eq)
    else:
        total["est_monthly_income"] = _med([monthly[k]["inflow_genuine"] for k in full_keys])
    total["turnover_annualised"] = _r(total["avg_monthly_inflow"] * 12)
    total["abb_months_avg"] = _r(sum(monthly[k]["bal_avg"] for k in full_keys) / len(full_keys))
    total["foir_pct"] = _pct(total["emi_debit"] / months_eq, total["est_monthly_income"])
    total["months_with_bounce"] = sum(1 for m_ in monthly.values() if m_["bounce_count"])
    total["months_negative_flow"] = sum(1 for m_ in monthly.values() if m_["net_flow"] < 0)
    total["reversal_inflation_pct"] = _pct(total["credit_reversal"], total["credit_gross"])
    total["recurring_parties"] = len(recurring)
    total["recon_break_count"] = len(parsed.recon_breaks)
    total["recon_break_pct"] = _pct(len(parsed.recon_breaks), parsed.recon_checked)
    total["doc_flag_count"] = len(parsed.doc_flags)

    flags = build_flags(total, monthly, partial, parsed, trips, recurring)
    score = min(100, sum(f["points"] for f in flags))
    total["risk_score"] = score
    band = "Low" if score < 25 else ("Medium" if score < 50 else "High")

    return {
        "meta": meta, "rows": rows, "months": list(monthly), "partial": partial, "monthly": monthly, "total": total,
        "flags": flags, "risk": {"score": score, "band": band}, "recurring": recurring, "round_trips": trips,
        "eod": sorted(eod.items()), "opening": opening,
        "parties_credit": party_table([r for r in rows if r["dir"] == "C" and not r["reversal"] and r["category"] not in ("SELF_TRANSFER", "REFUND", "INTEREST")]),
        "parties_debit": party_table([r for r in rows if r["dir"] == "D" and not r["reversal"] and r["category"] not in ("CHARGES", "BOUNCE_CHARGE", "CASH_WITHDRAWAL", "SELF_TRANSFER")]),
        "categories": category_table(rows), "doc_flags": parsed.doc_flags,
    }


def party_table(rows, top=20):
    agg = {}
    for r in rows:
        if not r["party"]:
            continue
        a = agg.setdefault(r["party"], {"party": r["party"], "amount": 0.0, "count": 0, "months": set(), "first": r["date"], "last": r["date"]})
        a["amount"] += r["amount"]
        a["count"] += 1
        a["months"].add(r["month"])
        a["first"], a["last"] = min(a["first"], r["date"]), max(a["last"], r["date"])
    tot = sum(a["amount"] for a in agg.values()) or 1
    out = sorted(agg.values(), key=lambda a: -a["amount"])[:top]
    for a in out:
        a["amount"] = _r(a["amount"])
        a["share_pct"] = round(a["amount"] / tot * 100, 2)
        a["months"] = len(a["months"])
    return out


def category_table(rows):
    agg = defaultdict(lambda: {"cr_amt": 0.0, "cr_n": 0, "dr_amt": 0.0, "dr_n": 0})
    for r in rows:
        a = agg[r["category"]]
        if r["dir"] == "C":
            a["cr_amt"] += r["amount"]
            a["cr_n"] += 1
        else:
            a["dr_amt"] += r["amount"]
            a["dr_n"] += 1
    return [{"category": k, **{kk: (_r(v) if isinstance(v, float) else v) for kk, v in a.items()}} for k, a in sorted(agg.items())]


# ----------------------------------------------------------------------------- flags
def _flag(code, sev, title, detail, points):
    return {"code": code, "severity": sev, "title": title, "detail": detail, "points": points}


def inr(x):
    return config.rupees(int(round(x * 100)))


def build_flags(t, monthly, partial, parsed, trips, recurring):
    f = []
    if t["bounce_count"]:
        f.append(_flag("BOUNCE", "High", f"{t['bounce_count']} bounced cheque(s)/mandate(s)",
                       f"Rejected items worth {inr(t['bounce_amount'])} in {t['months_with_bounce']} month(s). Each was debited and then reversed with a reject reason; the reversal credits are excluded from income.",
                       min(30, 8 * t["bounce_count"])))
    if t["bounce_charge_count"] and not t["bounce_count"]:
        f.append(_flag("RETURN_CHARGE", "Medium", "Return charges present", f"{t['bounce_charge_count']} return-charge entries ({inr(t['bounce_charge_debit'])}).", 8))
    if t["bal_days_negative"]:
        f.append(_flag("OVERDRAWN", "High", "Account overdrawn", f"End-of-day balance was negative on {t['bal_days_negative']} day(s).", 10))
    if t["reversal_inflation_pct"] >= 10:
        f.append(_flag("REVERSAL_INFLATION", "Medium", "Reversals form a material share of credits",
                       f"{t['reversal_inflation_pct']}% of printed credits ({inr(t['credit_reversal'])}) are reversals and are excluded from income. Adjusted credits are the relevant measure.", 6))
    if t["round_trip_count"]:
        f.append(_flag("ROUND_TRIP", "High", "Possible round-tripping",
                       f"{t['round_trip_count']} large credit(s) worth {inr(t['round_trip_amount'])} were mostly paid out again within {config.ROUND_TRIP_WINDOW_HOURS} hours.",
                       min(15, 5 * t["round_trip_count"])))
    cash_share = _pct(t["cash_deposit"], t["credit_adj"])
    if cash_share >= 40:
        f.append(_flag("CASH_HEAVY", "Medium", "Cash-heavy credits", f"{cash_share}% of credits are cash deposits.", 8))
    if t["credit_top1_pct"] >= 60 and t["credit_parties"] >= 1:
        f.append(_flag("CONCENTRATION", "Medium", "Dependent on one payer", f"Top payer supplies {t['credit_top1_pct']}% of net business inflow.", 8))
    if t["credit_last3_pct"] >= 40:
        f.append(_flag("WINDOW_DRESSING", "Medium", "Credits bunched at month end", f"{t['credit_last3_pct']}% of credits land in the last 3 days of a month.", 6))
    if t["emi_to_inflow_pct"] >= 50:
        f.append(_flag("HIGH_EMI", "High", "EMIs consume most of the inflow", f"EMIs are {t['emi_to_inflow_pct']}% of net business inflow.", 10))
    elif t["emi_to_inflow_pct"] >= 35:
        f.append(_flag("ELEVATED_EMI", "Medium", "High EMI burden", f"EMIs are {t['emi_to_inflow_pct']}% of net business inflow.", 5))
    if t["credit_trend_pct"] <= -30:
        f.append(_flag("DECLINING", "Medium", "Credits are falling", f"Recent months are {abs(t['credit_trend_pct'])}% below the first months.", 6))
    if t["net_flow"] < 0:
        f.append(_flag("NEG_FLOW", "Medium", "More money out than in", f"Net flow {inr(t['net_flow'])} over the period.", 6))
    if t["credit_cv_pct"] >= 60:
        f.append(_flag("VOLATILE", "Low", "Volatile monthly credits", f"Monthly credits vary by {t['credit_cv_pct']}% around the average.", 3))
    if t["same_day_turnover_pct"] >= 70:
        f.append(_flag("PASS_THROUGH", "Medium", "Pass-through account", f"{t['same_day_turnover_pct']}% of inflow leaves on the day it arrives.", 5))
    if t["bal_days_lt_10k"] >= 0.5 * t["window_days"]:
        f.append(_flag("THIN_BALANCE", "Low", "Balance very thin", f"Balance under INR 10,000 on {t['bal_days_lt_10k']} of {t['window_days']} days.", 4))
    if t["recon_break_count"]:
        f.append(_flag("RECON", "High" if t["recon_break_pct"] >= 2 else "Medium", "Balance mismatches",
                       f"{t['recon_break_count']} row(s) ({t['recon_break_pct']}%) do not follow the printed running balance; the statement may be incomplete or edited.",
                       min(12, 4 * t["recon_break_count"])))
    sc = parsed.meta.get("summary_check")
    if sc and not sc["verified"]:
        f.append(_flag("SUMMARY_MISMATCH", "High", "Totals differ from the bank's printed summary",
                       f"Printed: {inr(sc['printed']['credits'])} credits / {inr(sc['printed']['debits'])} debits. "
                       f"Read from transactions: {inr(sc['computed']['credits'])} / {inr(sc['computed']['debits'])}. Pages may be missing or edited.", 15))
    for d in parsed.doc_flags:
        f.append(_flag(d.get("code", "PDF_FLAG"), "High" if d.get("severity") == "high" else "Medium", "PDF integrity signal",
                       d.get("text", ""), 15 if d.get("severity") == "high" else 8))
    if any(partial.values()) and len(monthly) <= 2:
        f.append(_flag("SHORT", "Low", "Short statement", f"Only {t['statement_days']} days covered; most lenders ask for 6-12 months.", 3))
    sev = {"High": 0, "Medium": 1, "Low": 2}
    return sorted(f, key=lambda x: (sev[x["severity"]], -x["points"]))


def headline(a: dict) -> dict:
    """Small summary stored with the report row and shown on the dashboard page."""
    t = a["total"]
    return {k: t[k] for k in ("credit_gross", "credit_adj", "credit_reversal", "inflow_genuine", "debit_adj", "bal_avg", "bal_close",
                              "bal_min", "avg_monthly_credit", "avg_monthly_inflow", "emi_debit", "foir_pct", "bounce_count",
                              "bal_days_negative", "txn_count", "months_covered", "risk_score", "credit_top1_pct", "cash_deposit",
                              "gst_tax_debit", "est_monthly_income", "turnover_annualised")} | {
        "risk_band": a["risk"]["band"], "flags": [{"severity": f["severity"], "title": f["title"]} for f in a["flags"][:8]],
        "data_points": len(WIN_KEYS) * (len(a["months"]) + 1) + sum(1 for r in REGISTRY if r[5] == "tot"),
    }
