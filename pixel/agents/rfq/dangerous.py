"""The quote's answer to dangerous goods. `extract.py` `dangerous_goods` finds what a mail or its attachment declares
and marks the request (`DG_DOCUMENTS` in `Rfq.missing`, an `rfq.dangerous_goods` event). The quote then has to name
the goods and ask for the papers: without that the scorer rejects it (`dg_docs_missing`), at an unchanged price."""

import json

from pixel.agents.rfq.extract import DG_DOCUMENTS
from pixel.contracts import Rfq
from pixel.core.context import Context

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
# The mail itself declared them: there is no attached document to point to.
NOTE_MAIL = {
    "en": "One point before we book: as you wrote, these are dangerous goods ({goods}), and we will handle the "
          "shipment as such. Please send us the shipper's declaration for dangerous goods (DGD) and the safety data "
          "sheet (MSDS), so the airline can accept the cargo.",
    "de": "Ein Punkt vor der Buchung: Wie Sie schreiben, handelt es sich um Gefahrgut ({goods}), und wir behandeln "
          "die Sendung entsprechend. Bitte senden Sie uns die Versendererklärung für Gefahrgut (Shipper's "
          "Declaration for Dangerous Goods, DGD) und das Sicherheitsdatenblatt (MSDS), damit die Airline die "
          "Fracht annimmt.",
}  # fmt: skip


def declared(ctx: Context, rfq: Rfq) -> tuple[str, str] | None:
    """What the request declares and where ("mail" or the attachment's name), when `extract` marked it; None for
    ordinary goods."""
    if DG_DOCUMENTS not in rfq.missing:
        return None
    row = ctx.db.execute(
        "SELECT payload FROM events WHERE session_id = ? AND kind = 'rfq.dangerous_goods' ORDER BY id DESC LIMIT 1",
        (rfq.session_id,),
    ).fetchone()
    payload = json.loads(row["payload"]) if row else {}
    return payload.get("declared") or "see your document", payload.get("source") or ""


def with_note(body: str, goods: str, language: str, source: str = "") -> str:
    """The note goes before the closing line and the signature, the last two paragraphs of a quote."""
    notes = NOTE_MAIL if source == "mail" else NOTE
    paragraphs = body.split("\n\n")
    at = max(len(paragraphs) - 2, 1)
    return "\n\n".join([*paragraphs[:at], notes.get(language, notes["en"]).format(goods=goods), *paragraphs[at:]])
