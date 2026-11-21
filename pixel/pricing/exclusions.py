"""Explicit customer airline exclusions, sourced from the stored customer mail."""

import re
from dataclasses import dataclass

from pixel.contracts import RateOption, Rfq
from pixel.core.context import Context

EXCLUDE = re.compile(
    r"\b(?:(?:do not|don['’]t|never)\s+(?:offer|quote|use|book|select)(?:\s+(?:us|with))?"
    r"|(?:please\s+)?(?:exclude|avoid)(?:\s+(?:the\s+airline|airlines?))?"
    r"|(?:bitte\s+)?(?:kein(?:e|en)?|ohne))\s+([^\n]+)",
    re.IGNORECASE,
)
FUTURE = re.compile(
    r"\b(?:future (?:inquir(?:y|ies)|requests|shipments|quotes)|going forward|from now on|ever again"
    r"|(?:zukünftige[nr]?|künftige[nr]?) (?:anfragen|sendungen)|in zukunft|ab sofort)\b",
    re.IGNORECASE,
)
QUOTED = re.compile(r"^\s*(?:>+|On .+ wrote:|Am .+ schrieb .+:|[- ]*Original Message[- ]*)", re.IGNORECASE)
SENTENCE = re.compile(r"[.!?](?:\s|$)")
CODE = re.compile(r"(?<!\w)(?:\(([A-Z0-9]{2})\)|(?:^|[,;]|\band\b|\bor\b|\bund\b|\boder\b)\s*([A-Z0-9]{2})(?!\w))")


def _words(text: str) -> str:
    return " ".join(re.findall(r"[^\W_]+", text.casefold()))


@dataclass(frozen=True)
class Exclusions:
    clauses: tuple[str, ...] = ()
    codes: frozenset[str] = frozenset()

    def names(self, carrier: str, code: str | None = None) -> bool:
        name = _words(carrier)
        return bool(code and code.upper() in self.codes) or bool(
            name and any(f" {name} " in clause for clause in self.clauses)
        )

    def allows(self, option: RateOption) -> bool:
        return option.trucker is not None or not self.names(option.carrier, option.carrier_code)


def for_request(ctx: Context, rfq: Rfq) -> Exclusions:
    """Current request and explicit standing instructions from the same sender, never carrier or quoted mail."""
    rows = ctx.db.execute(
        "SELECT id, body FROM messages WHERE direction='in' AND sender=? AND id<=? ORDER BY id",
        (rfq.customer, rfq.message_id),
    )
    clauses, codes = [], set()
    for row in rows:
        own_lines = []
        for line in row["body"].splitlines():
            if QUOTED.match(line):
                break
            own_lines.append(line)
        for sentence in SENTENCE.split("\n".join(own_lines)):
            for match in EXCLUDE.finditer(sentence):
                clause = match[1].split(";", 1)[0]
                if row["id"] != rfq.message_id and not FUTURE.search(clause):
                    continue
                clauses.append(f" {_words(clause)} ")
                codes.update(a or b for a, b in CODE.findall(clause))
    return Exclusions(tuple(clauses), frozenset(codes))
