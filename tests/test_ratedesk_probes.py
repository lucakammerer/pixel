from tools.benchmark_generalization import audit_desk_probes


def test_rate_desk_rows_keep_their_currency_units_and_validity():
    readings = audit_desk_probes()
    assert any(row.outcome == "right" for row in readings)
    assert [
        (row.layout, row.fmt, row.detail) for row in readings if (row.outcome == "wrong") != bool(row.known_gap)
    ] == []
