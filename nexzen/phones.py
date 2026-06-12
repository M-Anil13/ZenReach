"""Phone normalization to E.164 + light country/timezone derivation.

libphonenumber is the production-grade choice; to stay dependency-light this
uses calling-code heuristics that cover the spec's target countries and any
number already in +<cc> form.
"""
import re

# calling code -> (ISO country, representative IANA timezone)
CC_INFO = {
    "91": ("IN", "Asia/Kolkata"), "1": ("US", "America/New_York"),
    "44": ("GB", "Europe/London"), "971": ("AE", "Asia/Dubai"),
    "65": ("SG", "Asia/Singapore"), "234": ("NG", "Africa/Lagos"),
    "49": ("DE", "Europe/Berlin"), "52": ("MX", "America/Mexico_City"),
    "61": ("AU", "Australia/Sydney"), "55": ("BR", "America/Sao_Paulo"),
    "86": ("CN", "Asia/Shanghai"), "81": ("JP", "Asia/Tokyo"),
    "92": ("PK", "Asia/Karachi"), "880": ("BD", "Asia/Dhaka"),
}
SORTED_CCS = sorted(CC_INFO, key=len, reverse=True)


def clean(raw):
    p = re.sub(r"[^\d+]", "", str(raw or "").strip())
    if p.startswith("00"):
        p = "+" + p[2:]
    return p


def to_e164(raw, default_cc="91"):
    p = clean(raw)
    if p.startswith("+"):
        return p
    d = re.sub(r"\D", "", p)
    if not d:
        return ""
    if len(d) <= 10 and default_cc:
        d = str(default_cc) + d
    return "+" + d


def is_valid(raw):
    d = re.sub(r"\D", "", clean(raw))
    return 8 <= len(d) <= 15


def country_tz(e164):
    d = e164.lstrip("+")
    for cc in SORTED_CCS:
        if d.startswith(cc):
            return CC_INFO[cc]
    return ("", "UTC")
