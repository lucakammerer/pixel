"""The quote as a PDF attachment: customers' purchasing departments file the offer and reject a quote without one
(round 76, `no_pdf_offer`). Plain text in Helvetica, written by hand: no dependency for one page of text."""

import re
import textwrap
from decimal import ROUND_HALF_UP, Decimal

from pixel.contracts import Quote

WIDTH, HEIGHT, MARGIN, SIZE, LEADING, COLUMNS = 595, 842, 50, 10, 13, 95
ROWS = (HEIGHT - 2 * MARGIN) // LEADING
CENT = Decimal("0.01")
# The customer's own reference: a word for it, then a code with a digit. "Your reference" is ours, not theirs.
REFERENCE = re.compile(
    r"(?i)(?<!your )(?<!ihre )(?<!votre )\b(?:ref(?:erence|erenz)?|réf(?:érence)?|po(?: number| no)?|order (?:no|number)"
    r"|bestellnummer|auftragsnummer)\b\.?\s*[:#]?\s*(?P<ref>(?=[\w/-]*\d)[A-Z0-9][\w/-]{2,24})"
)
LABELS = {
    "en": ("Offer", "Your reference", "Routing", "{days} days in transit", "Chargeable weight", "billed on",
           "Air freight", "Fuel surcharge", "Security surcharge", "Pickup", "All-in total"),
    "de": ("Angebot", "Ihre Referenz", "Routing", "Laufzeit {days} Tage", "Frachtpflichtiges Gewicht", "berechnet auf",
           "Luftfracht", "Treibstoffzuschlag", "Sicherheitszuschlag", "Abholung", "Gesamtpreis all-in"),
    "fr": ("Offre", "Votre référence", "Routage", "{days} jours de transit", "Poids taxable", "facturé sur",
           "Fret aérien", "Surcharge carburant", "Surcharge sûreté", "Enlèvement", "Total tout compris"),
}  # fmt: skip


def reference(text: str) -> str | None:
    match = REFERENCE.search(text or "")
    return match["ref"] if match else None


def _amount(value: Decimal) -> str:
    return f"{value.quantize(CENT, rounding=ROUND_HALF_UP):.2f}"


def details(quote: Quote, ref: str | None = None) -> list[str]:
    """What a purchasing department files besides the mail: offer number, their reference, routing, weight basis
    and the all-in breakdown. Only in a language we have labels for, and the breakdown only when it adds up to the
    quoted total in the quoted currency."""
    labels = LABELS.get(quote.rfq.language)
    if not labels:
        return []
    offer_, yours, routing, transit, chargeable, billed, *parts, total = labels
    o, cur = quote.option, quote.currency
    kg = f"{chargeable}: {o.chargeable_kg.normalize():f} kg"
    if o.billed_kg != o.chargeable_kg:
        kg += f", {billed} {o.billed_kg.normalize():f} kg"
    lines = ["", f"{offer_}: {quote.id}"] + ([f"{yours}: {ref}"] if ref else [])
    lines += [f"{routing}: {o.origin} - {o.destination}, {o.carrier}, {transit.format(days=o.transit_days)}", kg]
    amounts = (o.freight, o.fuel, o.security, o.pickup)
    if o.currency == cur and _amount(sum(amounts, Decimal(0))) == _amount(quote.total):
        lines += [
            f"{name}: {_amount(a)} {cur}" for name, a in zip(parts, amounts, strict=True) if a or name == parts[0]
        ]
    return [*lines, f"{total}: {_amount(quote.total)} {cur}"]


def _lines(quote: Quote, ref: str | None) -> list[str]:
    """The mail's text and its `details`; a language Helvetica cannot write keeps only the figures, which carry no
    language."""
    try:
        text = "\n".join([quote.body, *details(quote, ref)])
        text.encode("cp1252")
    except UnicodeEncodeError:
        o = quote.option
        text = f"{quote.id}\n{o.origin} - {o.destination}\n{o.carrier}\n{quote.total} {quote.currency}"
    return [part for line in text.splitlines() for part in (textwrap.wrap(line, COLUMNS) or [""])]


def _string(line: str) -> bytes:
    raw = line.encode("cp1252", "replace")
    return b"(" + raw.replace(b"\\", b"\\\\").replace(b"(", b"\\(").replace(b")", b"\\)") + b")"


def offer(quote: Quote, ref: str | None = None) -> bytes:
    lines = _lines(quote, ref)
    pages = [lines[i : i + ROWS] for i in range(0, len(lines), ROWS)] or [[]]
    font = 3 + 2 * len(pages)
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [%s] /Count %d >>"
        % (b" ".join(b"%d 0 R" % (3 + 2 * i) for i in range(len(pages))), len(pages)),
    ]
    for i, page in enumerate(pages):
        stream = b"BT /F1 %d Tf %d TL %d %d Td " % (SIZE, LEADING, MARGIN, HEIGHT - MARGIN)
        stream += b" ".join(_string(line) + b" Tj T*" for line in page) + b" ET"
        objects.append(
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 %d %d] /Resources << /Font << /F1 %d 0 R >> >> "
            b"/Contents %d 0 R >>" % (WIDTH, HEIGHT, font, 4 + 2 * i)
        )
        objects.append(b"<< /Length %d >>\nstream\n%s\nendstream" % (len(stream), stream))
    objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>")
    out, offsets = bytearray(b"%PDF-1.4\n"), []
    for number, body in enumerate(objects, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n%s\nendobj\n" % (number, body)
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    out += b"".join(b"%010d 00000 n \n" % offset for offset in offsets)
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objects) + 1, xref)
    return bytes(out)
