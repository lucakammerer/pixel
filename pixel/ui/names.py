"""Who a sender is, for display: the company and person a customer signs with, a carrier's listed name, or the
5U desk behind an address. Display only: links, search and filters stay keyed by the address.

Read from the customer's own mails (the signature), else from the address; nothing about a customer is stored in
code. Built once per new mail and cached."""

import re
from dataclasses import dataclass

from pixel.api.mailbox import RATEDESK, TRACKING
from pixel.core.context import Context

LEGAL = r"GmbH & Co\.? KG|GmbH|AG|KG|SE|AB|AS|A/S|Ltd\.?|Limited|Inc\.?|LLC|S\.?A\.?|S\.?r\.?l\.?|B\.?V\.?|Oy|plc"
LEGAL_RE = re.compile(rf"\b({LEGAL})(?=$|[\s,])")
LEGAL_WORDS = {w.lower().rstrip("."): w for w in ("GmbH", "AG", "KG", "SE", "AB", "Ltd", "Inc", "LLC", "BV", "plc")}
ROLE_WORDS = {
    "desk", "shipping", "logistics", "head", "export", "import", "versand", "manager", "purchasing", "coordinator",
    "team", "department", "operations", "assistant", "sales", "buyer", "regards", "thanks", "cheers", "best",
    "greetings", "gruesse", "grüße", "kind", "many", "viele",
}  # fmt: skip
MAILBOX_ROLES = {"ops", "logistics", "export", "shipping", "freight", "buyer", "info", "sales", "orders", "import"}
QUOTED = re.compile(r"^(>|On .+ wrote:|-----|From: )", re.MULTILINE)


@dataclass(frozen=True)
class Who:
    name: str  # what the UI shows
    email: str
    contact: str = ""  # the person who signs, if the signature names one
    city: str = ""
    kind: str = "customer"  # customer | carrier | desk


def _title(slug: str) -> str:
    return " ".join(LEGAL_WORDS.get(w, w.capitalize()) for w in re.split(r"[-_.]+", slug) if w)


def from_address(email: str) -> str:
    """`lindqvist-medtech@x` → Lindqvist Medtech; `buyer@acme-gmbh.x.y` → Acme GmbH (a role mailbox names the
    company in its domain)."""
    local, _, domain = email.lower().partition("@")
    labels = domain.split(".")
    if local in MAILBOX_ROLES and len(labels) > 2:
        return _title(labels[0])
    return _title(local)


def _person(line: str) -> bool:
    words = line.replace(",", "").split()
    return 1 <= len(words) <= 3 and all(w[:1].isupper() and w.isalpha() and w.lower() not in ROLE_WORDS for w in words)


def from_signature(body: str, email: str) -> tuple[str, str, str] | None:
    """(company, contact, city) from the signature at the end of the mail's own text, or None."""
    own = QUOTED.split(body, maxsplit=1)[0]
    lines = [ln.strip() for ln in own.strip().splitlines() if ln.strip()][-6:]
    slug = re.sub(r"[^a-z]", "", from_address(email).lower())
    for i in range(len(lines) - 1, -1, -1):
        line = lines[i]
        if "|" in line or any(c.isdigit() for c in line) or len(line.split()) > 8:
            continue
        company, city = "", ""
        for part in (p.strip() for p in line.split(",")):
            if not company and (LEGAL_RE.search(part) or (slug and re.sub(r"[^a-z]", "", part.lower()) == slug)):
                company = part
            elif company and not city and part.replace(" ", "").isalpha() and part[:1].isupper():
                city = part
        if company:
            contact = next((ln for ln in reversed(lines[max(0, i - 3) : i]) if _person(ln)), "")
            return company, contact, city
    return None


_cache: dict[int, tuple[int, dict[str, Who]]] = {}


def directory(ctx: Context) -> dict[str, Who]:
    """Every sender we know, by lower-case address. Rebuilt only when a new mail arrived."""
    newest = ctx.db.execute("SELECT MAX(id) FROM messages").fetchone()[0] or 0
    hit = _cache.get(id(ctx.db))
    if hit and hit[0] == newest:
        return hit[1]
    people: dict[str, Who] = {}
    rows = ctx.db.execute(
        "SELECT LOWER(sender) AS sender, body FROM messages WHERE direction = 'in' ORDER BY id DESC"
    ).fetchall()
    for row in rows:
        email = row["sender"]
        if email in people and people[email].contact:
            continue
        found = from_signature(row["body"] or "", email)
        if found:
            company, contact, city = found
            old = people.get(email)
            contact = contact or (old.contact if old else "")
            people[email] = Who(company, email, contact, city or (old.city if old else ""))
    for row in ctx.db.execute("SELECT name, email FROM carriers"):
        people[row["email"].lower()] = Who(row["name"], row["email"].lower(), kind="carrier")
    people[TRACKING] = Who("Tracking desk", TRACKING, kind="desk")
    people[RATEDESK] = Who("Rate desk", RATEDESK, kind="desk")
    _cache[id(ctx.db)] = (newest, people)
    return people


def who(ctx: Context, email: str | None) -> Who:
    email = (email or "").strip().lower()
    return directory(ctx).get(email) or Who(from_address(email) if email else "Unknown sender", email)
