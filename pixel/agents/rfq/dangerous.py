"""Dangerous goods a customer's attachment declares. A quote for them has to name them and ask for the papers:
without that the scorer rejects it (`dg_docs_missing`), whatever the mail itself says about the goods."""

import re

from pixel.agents.tariffs.agent import _lines
from pixel.contracts import Message
from pixel.core import events
from pixel.core.context import Context

DECLARED = re.compile(r"(?im)^\s*(?:dangerous goods|hazardous goods|gefahrgut|dg)\b\s*[:|]?\s*(.+?)\s*$")
NOT_DANGEROUS = re.compile(r"(?i)^(?:no|none|non|not|nil|n/?a|nein|kein\w*|-+|—)\b")
NOTE = {
    "en": "One point before we book: the document attached to your mail lists the goods as dangerous goods "
          "({goods}), while the mail describes them otherwise. We will handle the shipment as dangerous goods. "
          "Please send us the shipper's declaration for dangerous goods (DGD) and the safety data sheet (MSDS), "
          "so the airline can accept the cargo.",
    "de": "Ein Punkt vor der Buchung: Das Dokument im Anhang Ihrer Mail führt die Ware als Gefahrgut "
          "({goods}), die Mail selbst beschreibt sie anders. Wir behandeln die Sendung als Gefahrgut (dangerous "
          "goods). Bitte senden Sie uns die Versendererklärung für Gefahrgut (Shipper's Declaration for Dangerous "
          "Goods, DGD) und das Sicherheitsdatenblatt (MSDS), damit die Airline die Fracht annimmt.",
}  # fmt: skip


def declared_in(lines: list[str]) -> str | None:
    for match in DECLARED.finditer("\n".join(lines)):
        if not NOT_DANGEROUS.match(match.group(1)):
            return match.group(1)
    return None


async def declared(ctx: Context, message: Message) -> str | None:
    """What the mail's attachments declare as dangerous goods (UN number, class, name), or None."""
    for attachment in message.attachments:
        try:
            lines = _lines(attachment.filename, await ctx.mailbox.attachment(message.id, attachment.id))
        except Exception as exc:  # noqa: BLE001  an attachment we cannot read must not cost the quote
            events.emit(ctx, "error", "rfq", session_id=message.session_id, message_id=message.id,
                        payload={"where": "attachment", "file": attachment.filename, "error": type(exc).__name__})  # fmt: skip
            continue
        if goods := declared_in(lines):
            return goods
    return None


def with_note(body: str, goods: str, language: str) -> str:
    """The note goes before the closing line and the signature, the last two paragraphs of a quote."""
    paragraphs = body.split("\n\n")
    at = max(len(paragraphs) - 2, 1)
    return "\n\n".join([*paragraphs[:at], NOTE.get(language, NOTE["en"]).format(goods=goods), *paragraphs[at:]])
