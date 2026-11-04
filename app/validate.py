"""Input checks for the sign-up form. Everything the browser enforces is enforced again here, because a script can skip the browser."""
import re
import unicodedata

NAME_MAX = 25
ORG_MAX = 80
EMAIL_MAX = 254
_LABEL = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
_LOCAL = re.compile(r"^[a-z0-9._%+\-]+$")


def has_control(s: str) -> bool:
    return any(unicodedata.category(ch).startswith("C") for ch in s)


def person_name(label: str, raw: str) -> tuple[str, str | None]:
    """Letters only, 1 to 25 of them: no digits, spaces, hyphens or symbols."""
    s = unicodedata.normalize("NFKC", (raw or "").strip())
    if not s:
        return s, f"Please enter your {label}."
    if len(s) > NAME_MAX:
        return s, f"Your {label} can have at most {NAME_MAX} letters."
    if not all(ch.isalpha() for ch in s):
        return s, f"Your {label} can contain letters only, with no numbers, spaces or special characters."
    return s, None


def company(raw: str) -> tuple[str, str | None]:
    s = unicodedata.normalize("NFKC", (raw or "").strip())
    if len(s) > ORG_MAX:
        return s, f"The company name can have at most {ORG_MAX} characters."
    if has_control(s) or "<" in s or ">" in s:
        return s, "The company name contains characters that are not allowed."
    return re.sub(r"\s+", " ", s), None


def email(raw: str) -> tuple[str, str | None]:
    """A practical, strict check: one @, a sane local part, a real-looking domain with a letters-only ending."""
    s = (raw or "").strip().lower()
    bad = "Please enter a valid email address, for example name@company.com."
    if not s:
        return s, "Please enter your email address."
    if len(s) > EMAIL_MAX or not s.isascii() or any(c.isspace() for c in s) or has_control(s):
        return s, bad
    if s.count("@") != 1:
        return s, bad
    local, domain = s.split("@")
    if not local or len(local) > 64 or not _LOCAL.match(local) or local[0] == "." or local[-1] == "." or ".." in local:
        return s, bad
    labels = domain.split(".")
    if len(domain) > 253 or len(labels) < 2 or not all(_LABEL.match(x) for x in labels):
        return s, bad
    tld = labels[-1]
    if not (2 <= len(tld) <= 24 and tld.isalpha()):
        return s, bad
    return s, None
