import sys, re
sys.path.insert(0, __import__('os').path.dirname(__import__('os').path.abspath(__file__))); sys.path.insert(0, __import__('os').path.dirname(__import__('os').path.dirname(__import__('os').path.abspath(__file__))))
from datetime import date
from make_pdfs import *
out=sys.argv[1]
specs=[
 ("HDFC","hdfc",dict(bank="HDFC BANK LIMITED",holder="M/S. SAMPLE TRADERS (FICTIONAL)",account="50200012345678",ifsc="HDFC0001234",acct_type="Current"),dict(start=date(2026,4,1),opening=185000.0,seed=11,months=6)),
 ("Kotak","kotak",dict(bank="Kotak Mahindra Bank",holder="MR. SAMPLE KUMAR (FICTIONAL)",account="7412345678",ifsc="KKBK0000631",acct_type="Savings"),dict(start=date(2026,3,1),opening=64000.0,seed=21,months=6,salaried=True)),
 ("SBI","sbi",dict(bank="State Bank of India",holder="SAMPLE GENERAL STORE (FICTIONAL)",account="32145678901",ifsc="SBIN0001234",acct_type="Savings"),dict(start=date(2026,4,1),opening=92000.0,seed=31,months=6)),
]
for name,layout,kw,lk in specs:
    rows,opening=synthetic_ledger(**lk)
    if name=="SBI":
        for r in rows:
            r["narration"]=("BY TRANSFER-" if r["dir"]=="C" else "TO TRANSFER-")+r["narration"]
    for pw,suffix in ((None,""),("pass123","_locked")):
        pdf=render_statement(rows,opening,layout=layout,password=pw,**kw)
        open(f"{out}/{name}_sample_statement{suffix}.pdf","wb").write(pdf)
    print(name,len(rows),"txns",rows[0]["date"],rows[-1]["date"])
