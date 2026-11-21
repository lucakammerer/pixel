"""The quote's answer to dangerous goods. `extract.py` `dangerous_goods` finds what a mail or its attachment declares
and marks the request (`DG_DOCUMENTS` in `Rfq.missing`, an `rfq.dangerous_goods` event). The quote then has to name
the goods and ask for the papers: without that the scorer rejects it (`dg_docs_missing`), at an unchanged price."""

import json

from pixel.agents.rfq.compose import COMPANY
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
    "fr": "Un point avant la réservation : le document joint à votre mail déclare la marchandise comme dangereuse "
          "({goods}), alors que le mail la décrit autrement. Nous traiterons l'envoi comme marchandise dangereuse. "
          "Merci de nous envoyer la déclaration de l'expéditeur pour marchandises dangereuses (DGD) et la fiche de "
          "données de sécurité (MSDS), afin que la compagnie accepte la marchandise.",
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
    "fr": "Un point avant la réservation : comme vous l'indiquez, il s'agit de marchandises dangereuses ({goods}), et "
          "nous traiterons l'envoi comme tel. Merci de nous envoyer la déclaration de l'expéditeur pour marchandises "
          "dangereuses (DGD) et la fiche de données de sécurité (MSDS), afin que la compagnie accepte la marchandise.",
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
    """The note goes before the paragraph above the signature (the booking line or the returned wish); a P.S. may
    follow the signature, so the signature is found by our company line, not by its place."""
    notes = NOTE_MAIL if source == "mail" else NOTE
    paragraphs = body.split("\n\n")
    signed = next((i for i, p in enumerate(paragraphs) if COMPANY in p), len(paragraphs) - 1)
    at = max(signed - 1, 1)
    return "\n\n".join([*paragraphs[:at], notes.get(language, notes["en"]).format(goods=goods), *paragraphs[at:]])
