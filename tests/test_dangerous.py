from pixel.agents.rfq import dangerous

# the lines our reader makes of the two attachments whose requests we lost: a packing specification (xlsx) and a
# shipper's letter of instruction (pdf)
XLSX = ["Packing Specification", "Destination airport:  GRU", "Total gross weight (kg):  715",
        "Dangerous goods:  UN1133, Class 3, Adhesives"]  # fmt: skip
PDF = ["Shipper's Letter of Instruction", "Total volume (cbm): 3.74",
       "Dangerous goods: UN1950, Class 2.1, Aerosols, flammable", "Pieces Length (cm) Height (cm)"]  # fmt: skip
BODY = "Hello,\n\nwe can offer 2187.10 EUR all-in.\n\nJust reply to book.\n\nBest regards\nLena"


def test_the_attachment_declares_the_goods_whatever_the_mail_says():
    assert dangerous.declared_in(XLSX) == "UN1133, Class 3, Adhesives"
    assert dangerous.declared_in(PDF) == "UN1950, Class 2.1, Aerosols, flammable"


def test_a_document_that_declares_none_adds_nothing():
    for line in ("Dangerous goods: No", "Dangerous goods:  none", "Dangerous goods: n/a", "Gefahrgut: nein"):
        assert dangerous.declared_in(["Packing Specification", line]) is None
    assert dangerous.declared_in(["Goods description: industrial adhesives"]) is None


def test_the_note_names_the_goods_and_asks_for_the_papers_before_the_closing():
    body = dangerous.with_note(BODY, "UN1950, Class 2.1, Aerosols, flammable", "en")
    paragraphs = body.split("\n\n")
    assert paragraphs[1] == "we can offer 2187.10 EUR all-in." and paragraphs[3:] == BODY.split("\n\n")[2:]
    assert "UN1950, Class 2.1" in paragraphs[2] and "DGD" in paragraphs[2] and "MSDS" in paragraphs[2]
    assert "Gefahrgut" in dangerous.with_note(BODY, "UN1133", "de") and "DGD" in dangerous.with_note(
        BODY, "UN1133", "de"
    )
