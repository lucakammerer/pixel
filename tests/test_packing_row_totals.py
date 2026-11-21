from decimal import Decimal

import pytest

from pixel.agents.rfq import extract

EN = "Length (cm) Width (cm) Height (cm) Pieces Gross weight (kg)"
DE = "Länge (mm) Breite (mm) Höhe (mm) Stück Bruttogewicht (kg)"
ROWS = "120 80 60 2 300\n100 80 50 1 108"


@pytest.mark.parametrize("table", [f"{EN}\n{ROWS}", f"{DE}\n1200 800 600 2 300\n1000 800 500 1 108"])
def test_table_rows_supply_missing_totals_without_reading_header_as_weight(table):
    facts, evidence = extract._cargo_sources([("mail", "Cargo figures are in the attachment."), ("packing", table)])
    assert facts == {"weight_kg": Decimal(408), "volume_cbm": Decimal("1.552"), "pieces": 3}
    assert all(names == ["packing"] for names in evidence.values())


def test_an_uncorroborated_table_does_not_override_explicit_mail_weight_or_volume():
    facts, evidence = extract._cargo_sources([("mail", "3 pieces, 500 kg, 2.5 cbm."), ("packing", f"{EN}\n{ROWS}")])
    assert facts == {"weight_kg": Decimal(500), "volume_cbm": Decimal("2.5"), "pieces": 3}
    assert all(names == ["mail"] for names in evidence.values())


def test_ambiguous_weight_columns_are_not_read_as_totals():
    table = f"{EN} Net weight (kg)\n120 80 60 2 300 270"
    facts, _ = extract._cargo_sources([("mail", "Figures in the attachment."), ("packing", table)])
    assert "weight_kg" not in facts
    assert facts["volume_cbm"] == Decimal("1.152")


def test_an_incomplete_numeric_row_does_not_become_a_partial_total():
    table = f"{EN}\n120 80 60 2 300\n100 80 50 1"
    facts, _ = extract._cargo_sources([("mail", "Figures in the attachment."), ("packing", table)])
    assert not facts


def test_german_labelled_totals_are_read_and_can_be_corroborated():
    table = f"Bruttogewicht gesamt (kg): 408\nVolumen gesamt (cbm): 1,55\n{DE}\n1200 800 600 2 300\n1000 800 500 1 108"
    facts, _ = extract._cargo_sources([("mail", "3 pieces, 500 kg, 2.5 cbm."), ("packing", table)])
    assert facts == {"weight_kg": Decimal(408), "volume_cbm": Decimal("1.55"), "pieces": 3}
