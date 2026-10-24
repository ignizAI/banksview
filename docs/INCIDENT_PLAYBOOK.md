# Incident playbook (draft, confirm with counsel)

**Trigger:** any suspected unauthorised access to, loss of or exposure of user or statement data, or a compromised credential.

| When | Action | Owner |
|---|---|---|
| Within 1 hour of noticing | Name an incident lead. Write down the time you noticed. Contain: disable affected accounts, rotate `SECRET_KEY` (signs out every user), revoke Razorpay and hosting keys, take the service offline if data is still leaking. | Founder |
| Within 6 hours of noticing | Report the incident to CERT-In (the plan notes a 6-hour reporting duty; confirm scope and form with counsel). | Founder, counsel |
| Within 24 hours | Establish what was exposed from `audit_log` and hosting logs: which accounts, which reports, which period. Preserve logs; do not delete anything. | Engineer |
| As the law requires | Notify the Data Protection Board and affected users as the DPDP Act and Rules require. Tell users to treat exposed borrower data as compromised. Counsel drafts the wording. | Counsel |
| Within 7 days | Fix the cause. Add a test that would have caught it. Write a short post-incident note: timeline, cause, impact, fix. | Engineer |

Contacts to fill in before launch: counsel, hosting support, payment gateway, insurance broker.

Do not write "fraud", "breach confirmed" or similar in public until counsel has reviewed the wording.
