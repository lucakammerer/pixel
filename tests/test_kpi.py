from tools import kpi


def _row(request, eur, seconds=30.0):
    return {"request": request, "answered": eur is not None, "total": eur, "currency": "EUR", "eur": eur,
            "carrier": "Air One", "model_calls": 1, "usd": "0.001", "seconds": seconds}  # fmt: skip


def test_the_report_scores_cost_and_speed_and_lists_only_requests_whose_answer_differs():
    base = [_row("same", "100.00"), _row("cheaper", "200.00"), _row("lost", "50.00"), _row("unrouted", "70.00")]
    branch = [_row("same", "100.00"), _row("cheaper", "190.00", seconds=264.0), _row("lost", None)]

    text = kpi.report({"online": True, "requests": branch}, {"online": True, "requests": base})

    assert "| Accuracy: answered | 4 of 4 | 2 of 3 |" in text
    assert "| Accuracy: sum of the quoted prices | 420.00 EUR | 290.00 EUR |" in text
    # the spend of a missed request is paid by the quotes that went out
    assert "| Cost: spend per quote | 0.00100 USD, 39.6 of 40 points | 0.00150 USD, 39.4 of 40 points |" in text
    assert "| Speed: median time to the quote | 30 s, 15.0 of 15 points | 147 s, 10.5 of 15 points |" in text
    assert (
        "| cheaper | 200.00 EUR · Air One · 0.00100 USD · 30.0 s | 190.00 EUR · Air One · 0.00100 USD · 264.0 s | -5.0% |"
        in text
    )
    assert "| lost | 50.00 EUR · Air One · 0.00100 USD · 30.0 s | missed · 0.00100 USD |  |" in text
    assert "| unrouted | 70.00 EUR · Air One · 0.00100 USD · 30.0 s | — |  |" in text
    assert "| same |" not in text
