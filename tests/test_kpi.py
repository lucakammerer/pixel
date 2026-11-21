from tools import kpi


def _row(request, eur, calls=1):
    return {"request": request, "answered": eur is not None, "total": eur, "currency": "EUR", "eur": eur,
            "carrier": "Air One", "model_calls": calls}  # fmt: skip


def test_the_report_lists_only_requests_whose_answer_differs_with_the_price_change():
    base = [_row("same", "100.00"), _row("cheaper", "200.00"), _row("lost", "50.00"), _row("unrouted", "70.00")]
    rows = [_row("same", "100.00"), _row("cheaper", "190.00"), _row("lost", None, calls=2)]

    text = kpi.report(rows, base)

    assert "| Answered | 4 of 4 | 2 of 3 |" in text and "| Model calls | 4 | 4 |" in text
    assert "| cheaper | 200.00 EUR · Air One · 1 model calls | 190.00 EUR · Air One · 1 model calls | -5.0% |" in text
    assert "| lost | 50.00 EUR · Air One · 1 model calls | missed · 2 model calls |  |" in text
    assert "| unrouted | 70.00 EUR · Air One · 1 model calls | — |  |" in text
    assert "| same |" not in text
