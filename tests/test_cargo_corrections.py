from decimal import Decimal

import pytest

from pixel.agents.rfq import extract

PACKING = "Total gross weight: 950 kg\nLength (cm) Width (cm) Height (cm) Pieces Gross weight (kg)\n100 80 50 2 950"


@pytest.mark.parametrize(
    "correction",
    [
        "Correction: the gross weight is now 700 kg, the attached list is outdated.",
        "Korrektur: Das Bruttogewicht beträgt jetzt 700 kg, die angehängte Liste ist veraltet.",
    ],
)
def test_explicit_correction_wins_over_a_corroborated_packing_weight(correction):
    facts, evidence = extract._cargo_sources([("mail", f"2 pallets, 950 kg, 0.8 cbm. {correction}"), ("pack", PACKING)])
    assert facts["weight_kg"] == Decimal(700)
    assert evidence["weight_kg"] == ["mail"]


def test_outdated_comment_without_an_explicit_correction_does_not_override_weight():
    facts, evidence = extract._cargo_sources(
        [("mail", "2 pallets, 700 kg, 0.8 cbm. The old invoice had a typo."), ("pack", PACKING)]
    )
    assert facts["weight_kg"] == Decimal(950)
    assert evidence["weight_kg"] == ["pack"]


def test_correction_of_volume_leaves_corroborated_weight_in_place():
    facts, evidence = extract._cargo_sources(
        [("mail", "2 pallets, 700 kg, 0.8 cbm. Korrektur: Das Volumen beträgt jetzt 1,5 cbm."), ("pack", PACKING)]
    )
    assert facts["weight_kg"] == Decimal(950)
    assert facts["volume_cbm"] == Decimal("1.5")
    assert evidence["volume_cbm"] == ["mail"]
