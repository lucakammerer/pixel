from decimal import Decimal

import pytest

from pixel.agents.rfq import extract


@pytest.mark.parametrize("suffix", ["each", "apiece", "per pallet", "je Stück"])
def test_in_before_a_piece_marker_is_inches(suffix):
    volume = extract._volume(f"5 pallets 24 x 16 x 51 in {suffix}", 5)
    assert volume.quantize(Decimal("0.00001")) == Decimal("1.60462")


@pytest.mark.parametrize("suffix", ["in total", "in the attached list"])
def test_in_as_a_preposition_is_not_inches(suffix):
    assert extract._volume(f"3 pallets 120 x 80 x 150 {suffix}", 3) == Decimal("4.32")
