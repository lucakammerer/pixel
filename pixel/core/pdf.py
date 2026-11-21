"""The quote as a PDF attachment: customers' purchasing departments file the offer and reject a quote without one
(round 76, `no_pdf_offer`). Plain text in Helvetica, written by hand: no dependency for one page of text."""

import textwrap

from pixel.contracts import Quote

WIDTH, HEIGHT, MARGIN, SIZE, LEADING, COLUMNS = 595, 842, 50, 10, 13, 95
ROWS = (HEIGHT - 2 * MARGIN) // LEADING


def _lines(quote: Quote) -> list[str]:
    """The mail's text; a language Helvetica cannot write keeps only the figures, which carry no language."""
    try:
        quote.body.encode("cp1252")
        text = quote.body
    except UnicodeEncodeError:
        o = quote.option
        text = f"{quote.id}\n{o.origin} - {o.destination}\n{o.carrier}\n{quote.total} {quote.currency}"
    return [part for line in text.splitlines() for part in (textwrap.wrap(line, COLUMNS) or [""])]


def _string(line: str) -> bytes:
    raw = line.encode("cp1252", "replace")
    return b"(" + raw.replace(b"\\", b"\\\\").replace(b"(", b"\\(").replace(b")", b"\\)") + b")"


def offer(quote: Quote) -> bytes:
    lines = _lines(quote)
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
