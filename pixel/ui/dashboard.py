from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from pixel.crm import quotes, shipments
from pixel.ui.page import clock, esc, layout

router = APIRouter()


def tile(label: str, value, note: str = "") -> str:
    shown = "—" if value is None else value
    return f"<div class=card><div class=k>{label}</div><div class=v>{esc(shown)}</div><div class=k>{note}</div></div>"


@router.get("/dashboard", response_class=HTMLResponse)
async def dashboard(request: Request):
    """`crm.quotes.stats` plus booked shipments and the latest `score` events."""
    ctx = request.app.state.ctx
    s = quotes.stats(ctx)
    spend = s["spend_per_accepted_usd"]
    tiles = "".join((
        tile("Requests", s["requests"]),
        tile("Quoted", s["quoted"], f'{s["fallback"]} by fallback'),
        tile("Missed", s["missed"]),
        tile("Accepted by scorer", s["accepted"], f'{s["rejected"]} rejected · {s["pending"]} pending'),
        tile("Won with customer", s["won"], f'{s["lost"]} lost'),
        tile("Points", f'{float(s["points_total"]):.1f}'),
        tile("Spend per accepted quote", f"${spend:.4f}" if spend is not None else None,
             f'${float(s["spend_hour_usd"]):.4f} in the last hour'),
        tile("Median response", f'{s["median_response_s"]} s' if s["median_response_s"] is not None else None),
    ))  # fmt: skip
    points = "".join(tile(esc(k), f"{float(v):.1f}") for k, v in sorted(s["points"].items()))

    ship_rows = "".join(
        f'<tr><td class=mono><a href="/sessions/{x.session_id}">{esc(x.id)}</a></td><td>{esc(x.customer)}</td>'
        f"<td>{esc(x.origin)} → {esc(x.destination)}</td><td class=n>{esc(x.weight_kg)}</td><td>{esc(x.carrier)}</td>"
        f"<td class=mono>{esc(x.booking_reference or '—')}</td><td>{esc(x.status)}</td><td>{clock(x.updated_at)}</td></tr>"
        for x in shipments.recent(ctx)
    )
    score_rows = "".join(
        f"<tr><td>{esc(r.get('round'))}</td><td>{clock(r.get('sent_at'))}</td>"
        f"<td class='{'ok' if r.get('verdict') == 'accepted' else 'bad' if r.get('verdict') == 'rejected' else 'muted'}'>"
        f"{esc(r.get('verdict'))}</td><td>{esc(r.get('reason') or '')}</td>"
        + "".join(f"<td class=n>{esc((r.get('points') or {}).get(k, ''))}</td>" for k in ("accuracy", "cost", "time"))
        + f"<td class=n><b>{esc(r.get('total', ''))}</b></td></tr>"
        for r in quotes.latest_scores(ctx)[:30]
    )
    body = (
        f'<div id=refresh><div class=grid>{tiles}</div>'
        + (f"<h2>Points per kind</h2><div class=grid>{points}</div>" if points else "")
        + "<h2>Shipments</h2><table><thead><tr><th>Ours</th><th>Customer</th><th>Lane</th><th class=n>kg</th>"
        "<th>Carrier</th><th>Airline ref</th><th>Status</th><th>Updated</th></tr></thead>"
        f"<tbody>{ship_rows or '<tr><td colspan=8 class=muted>No shipment yet.</td></tr>'}</tbody></table>"
        "<h2>Scorer, per request</h2><table><thead><tr><th>Round</th><th>Sent</th><th>Verdict</th><th>Reason</th>"
        "<th class=n>Accuracy</th><th class=n>Cost</th><th class=n>Time</th><th class=n>Total</th></tr></thead>"
        f"<tbody>{score_rows or '<tr><td colspan=8 class=muted>No result from /v1/board/me yet.</td></tr>'}</tbody>"
        "</table></div>"
    )  # fmt: skip
    return layout("Dashboard", body, nav="dashboard", script="live('/events',refresh());")
