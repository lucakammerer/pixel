"""The control tower at `/map`: every lane from the CRM as an animated great-circle arc on a dot-matrix world.

A showcase for a big screen. It reads the CRM once per page load (quotes, shipments, carriers) and replays those
lanes as a night of requests, bid races, bookings and flights; with an empty CRM it plays demo lanes. Nothing on
this page sends, books or prices anything.
"""

import json
from functools import cache
from pathlib import Path
from statistics import median
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from pixel.core.events import emit
from pixel.crm import quotes, shipments
from pixel.ui import names
from pixel.ui.routes import ctx_of

router = APIRouter()


@cache
def _land() -> str:
    return Path(__file__).with_name("world-dots.json").read_text()


def _payload(ctx) -> dict[str, Any]:
    lanes: list[dict[str, Any]] = []
    answered: list[float] = []
    for q in quotes.recent(ctx, 300):
        secs = None
        if q.sent_at and q.rfq.received_at:
            secs = (q.sent_at - q.rfq.received_at).total_seconds()
            if 0 < secs < 3600:
                answered.append(secs)
        lanes.append(
            {
                "o": q.rfq.origin,
                "d": q.rfq.destination,
                "carrier": q.option.carrier,
                "kg": str(q.rfq.weight_kg),
                "eur": str(q.option.total_eur),
                "days": q.option.transit_days,
                "status": q.status.value,
                "customer": names.who(ctx, q.rfq.customer).name,
                "session": q.session_id,
                "secs": round(secs) if secs is not None else None,
            }
        )
    carriers = [
        {"name": row["name"], "serves": row["serves"].split()}
        for row in ctx.db.execute("SELECT name, serves FROM carriers WHERE kind = 'airline' ORDER BY name")
    ]
    sent = sum(1 for lane in lanes if lane["status"] != "draft")
    return {
        "lanes": lanes,
        "carriers": carriers,
        "stats": {
            "answered": sent,
            "booked": len(shipments.recent(ctx, 1000)),
            "median": round(median(answered)) if answered else None,
        },
    }


def page_data(ctx) -> tuple[str, str]:
    """The CRM's lanes as JSON for a page script, and the land dots; demo lanes when reading fails."""
    try:
        data = _payload(ctx)
    except Exception as error:  # noqa: BLE001 -- a showcase never fails to render: it falls back to demo lanes
        emit(ctx, "error", "ui.map", payload={"where": "map", "error": str(error)[:300]})
        data = {"lanes": [], "carriers": [], "stats": {}}
    return json.dumps(data).replace("</", "<\\/"), _land()


@router.get("/map", response_class=HTMLResponse)
def control_tower(request: Request) -> HTMLResponse:
    blob, land = page_data(ctx_of(request))
    return HTMLResponse(HTML.replace("__DATA__", blob).replace("__LAND__", land).replace("__AIRPORTS__", AIRPORTS_JS))


AIRPORTS_JS = r"""const AP = { // IATA: [city, lat, lon]
FRA:["Frankfurt",50.03,8.57],MUC:["Munich",48.35,11.79],DUS:["Düsseldorf",51.29,6.77],HAM:["Hamburg",53.63,9.99],
STR:["Stuttgart",48.69,9.22],CGN:["Cologne",50.87,7.14],BER:["Berlin",52.37,13.5],LEJ:["Leipzig",51.42,12.24],
HHN:["Hahn",49.95,7.26],NUE:["Nuremberg",49.5,11.08],HAJ:["Hanover",52.46,9.69],BRE:["Bremen",53.05,8.79],
VIE:["Vienna",48.11,16.57],ZRH:["Zurich",47.46,8.55],AMS:["Amsterdam",52.31,4.76],LHR:["London",51.47,-.45],
CDG:["Paris",49.01,2.55],LUX:["Luxembourg",49.63,6.21],BRU:["Brussels",50.9,4.48],MAD:["Madrid",40.47,-3.56],
BCN:["Barcelona",41.3,2.08],FCO:["Rome",41.8,12.25],MXP:["Milan",45.63,8.72],IST:["Istanbul",41.26,28.74],
ATH:["Athens",37.94,23.94],WAW:["Warsaw",52.17,20.97],PRG:["Prague",50.1,14.26],CPH:["Copenhagen",55.62,12.65],
ARN:["Stockholm",59.65,17.92],OSL:["Oslo",60.19,11.1],HEL:["Helsinki",60.32,24.96],LIS:["Lisbon",38.77,-9.13],
DUB:["Dublin",53.42,-6.27],JFK:["New York",40.64,-73.78],EWR:["Newark",40.69,-74.17],BOS:["Boston",42.36,-71.01],
ORD:["Chicago",41.98,-87.9],ATL:["Atlanta",33.64,-84.43],MIA:["Miami",25.79,-80.29],DFW:["Dallas",32.9,-97.04],
IAH:["Houston",29.99,-95.34],LAX:["Los Angeles",33.94,-118.41],SFO:["San Francisco",37.62,-122.38],
SEA:["Seattle",47.45,-122.31],YYZ:["Toronto",43.68,-79.63],YUL:["Montreal",45.47,-73.74],YVR:["Vancouver",49.19,-123.18],
MEX:["Mexico City",19.44,-99.07],GDL:["Guadalajara",20.52,-103.31],BOG:["Bogotá",4.7,-74.15],LIM:["Lima",-12.02,-77.11],
SCL:["Santiago",-33.39,-70.79],EZE:["Buenos Aires",-34.82,-58.54],GRU:["São Paulo",-23.43,-46.47],
GIG:["Rio de Janeiro",-22.81,-43.25],VCP:["Campinas",-23.01,-47.13],ASU:["Asunción",-25.24,-57.52],
UIO:["Quito",-.13,-78.36],PTY:["Panama City",9.07,-79.38],MVD:["Montevideo",-34.84,-56.03],LOS:["Lagos",6.58,3.32],
ACC:["Accra",5.61,-.17],NBO:["Nairobi",-1.32,36.93],ADD:["Addis Ababa",8.98,38.8],JNB:["Johannesburg",-26.14,28.24],
CPT:["Cape Town",-33.97,18.6],CAI:["Cairo",30.12,31.41],CMN:["Casablanca",33.37,-7.59],ALG:["Algiers",36.69,3.22],
DAR:["Dar es Salaam",-6.88,39.2],TNR:["Antananarivo",-18.8,47.48],MRU:["Mauritius",-20.43,57.68],DKR:["Dakar",14.74,-17.49],
TLV:["Tel Aviv",32.01,34.89],DXB:["Dubai",25.25,55.36],DWC:["Dubai World Central",24.9,55.16],AUH:["Abu Dhabi",24.43,54.65],
DOH:["Doha",25.27,51.61],RUH:["Riyadh",24.96,46.7],JED:["Jeddah",21.68,39.16],BAH:["Bahrain",26.27,50.63],
KWI:["Kuwait",29.24,47.97],MCT:["Muscat",23.59,58.28],BOM:["Mumbai",19.09,72.87],DEL:["Delhi",28.56,77.1],
BLR:["Bengaluru",13.2,77.71],MAA:["Chennai",12.99,80.17],CCU:["Kolkata",22.65,88.45],KHI:["Karachi",24.91,67.16],
CMB:["Colombo",7.18,79.88],DAC:["Dhaka",23.84,90.4],KTM:["Kathmandu",27.7,85.36],BKK:["Bangkok",13.69,100.75],
SGN:["Ho Chi Minh City",10.82,106.65],HAN:["Hanoi",21.22,105.81],SIN:["Singapore",1.36,103.99],KUL:["Kuala Lumpur",2.75,101.71],
CGK:["Jakarta",-6.13,106.66],MNL:["Manila",14.51,121.02],HKG:["Hong Kong",22.31,113.92],CAN:["Guangzhou",23.39,113.3],
SZX:["Shenzhen",22.64,113.81],PVG:["Shanghai",31.14,121.81],PEK:["Beijing",40.08,116.58],PKX:["Beijing Daxing",39.51,116.41],
TPE:["Taipei",25.08,121.23],ICN:["Seoul",37.46,126.44],NRT:["Tokyo Narita",35.77,140.39],HND:["Tokyo Haneda",35.55,139.78],
KIX:["Osaka",34.43,135.24],ULN:["Ulaanbaatar",47.65,106.82],ALA:["Almaty",43.35,77.04],TAS:["Tashkent",41.26,69.28],
SYD:["Sydney",-33.95,151.18],MEL:["Melbourne",-37.67,144.84],BNE:["Brisbane",-27.38,153.12],PER:["Perth",-31.94,115.97],
AKL:["Auckland",-37.01,174.79],PPT:["Papeete",-17.55,-149.61]};"""


HTML = r"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Control Tower · PIXEL</title>
<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Jost:wght@300;400;500;600&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet">
<link href="https://api.fontshare.com/v2/css?f[]=satoshi@900&display=swap" rel="stylesheet">
<style>
:root{--bg:#282322;--bg-2:#1f1b1a;--panel:rgba(40,35,34,.78);--line:rgba(255,236,224,.09);--line-2:rgba(255,236,224,.16);
--ink:#f5eee9;--ink-2:#cfc5be;--muted:#8f857e;--accent:#DE5531;--accent-2:#f08a62;--ok:#58c896;--mono:"JetBrains Mono",ui-monospace,SFMono-Regular,Menlo,monospace;
color-scheme:dark}
*{box-sizing:border-box}
html,body{height:100%}
body{margin:0;background:var(--bg-2);color:var(--ink);font:14px/1.45 Jost,system-ui,-apple-system,"Segoe UI",sans-serif;
-webkit-font-smoothing:antialiased;overflow:hidden;display:grid;grid-template-columns:minmax(0,1fr) 392px}
a{color:inherit;text-decoration:none}
.eyebrow{font-size:11px;letter-spacing:.16em;text-transform:uppercase;color:var(--muted);font-weight:500}
.mono{font-family:var(--mono)}
.num{font-variant-numeric:tabular-nums}

/* the stage: map, arcs, overlays */
.stage{position:relative;overflow:hidden;background:
radial-gradient(1200px 700px at 52% 46%,#2f2927 0%,var(--bg) 46%,var(--bg-2) 100%)}
.stage canvas{position:absolute;inset:0;width:100%;height:100%;display:block}
#fx{cursor:crosshair}
.stage::after{content:"";position:absolute;inset:0;pointer-events:none;background:
linear-gradient(180deg,rgba(31,27,26,.85),transparent 18%,transparent 78%,rgba(31,27,26,.9)),
radial-gradient(ellipse at center,transparent 55%,rgba(20,17,16,.55) 100%)}
.grid{position:absolute;inset:0;pointer-events:none;background-image:
linear-gradient(var(--line) 1px,transparent 1px),linear-gradient(90deg,var(--line) 1px,transparent 1px);
background-size:96px 96px;opacity:.35;-webkit-mask-image:radial-gradient(ellipse at center,#000 30%,transparent 75%);
mask-image:radial-gradient(ellipse at center,#000 30%,transparent 75%)}

.hud{position:absolute;z-index:3;left:32px;top:26px;right:32px;display:flex;align-items:flex-start;gap:24px;pointer-events:none}
.hud .brand{display:flex;align-items:center;gap:14px;pointer-events:auto}
.hud .brand .mark{display:flex;align-items:center;gap:10px;color:#fff;text-decoration:none;font:900 20px/1 Satoshi,Jost,system-ui,sans-serif;letter-spacing:-.01em}
.hud .brand .mark i{width:12px;height:12px;background:#DE5531;display:block}
.hud .sep{width:1px;height:18px;background:var(--line-2)}
.hud h1{margin:18px 0 0;font-size:40px;line-height:1.02;font-weight:500;letter-spacing:-.035em;max-width:620px}
.hud h1 em{font-style:normal;color:var(--accent)}
.hud p{margin:10px 0 0;color:var(--ink-2);font-size:15px;max-width:460px;font-weight:300}
.hud .right{margin-left:auto;display:flex;gap:10px;pointer-events:auto}
.chip{display:inline-flex;align-items:center;gap:8px;height:30px;padding:0 12px;border:1px solid var(--line-2);border-radius:999px;
font-size:11px;letter-spacing:.12em;text-transform:uppercase;color:var(--ink-2);background:rgba(31,27,26,.55);backdrop-filter:blur(8px)}
a.chip:hover{border-color:var(--accent);color:var(--ink)}
.dot{width:7px;height:7px;border-radius:50%;background:var(--accent);position:relative;flex:none}
.dot::after{content:"";position:absolute;inset:0;border-radius:50%;background:inherit;animation:ping 1.8s ease-out infinite}
@keyframes ping{from{transform:scale(1);opacity:.7}to{transform:scale(3.4);opacity:0}}

.legend{position:absolute;z-index:3;left:32px;bottom:142px;display:flex;flex-direction:column;gap:8px;font-size:12px;color:var(--ink-2)}
.legend div{display:flex;align-items:center;gap:10px}
.legend i{display:inline-block;width:26px;height:0;border-top:1.5px solid var(--accent)}
.legend i.q{border-top:1.5px dashed var(--ink-2)}
.legend i.h{width:9px;height:9px;border:1.5px solid var(--accent);border-radius:50%;margin:0 8px}
.legend i.d{width:5px;height:5px;background:var(--ink-2);border:0;border-radius:50%;margin:0 10px}

.kpis{position:absolute;z-index:3;left:32px;right:32px;bottom:26px;display:grid;grid-template-columns:repeat(4,minmax(0,1fr));
border:1px solid var(--line-2);border-radius:14px;background:rgba(31,27,26,.62);backdrop-filter:blur(14px);overflow:hidden}
.kpis>div{padding:16px 22px;border-left:1px solid var(--line)}.kpis>div:first-child{border-left:0}
.kpis .v{font-size:34px;font-weight:500;letter-spacing:-.03em;line-height:1.1;margin-top:6px;font-variant-numeric:tabular-nums}
.kpis .v small{font-size:15px;color:var(--muted);font-weight:400;margin-left:3px;letter-spacing:0}
.kpis .n{font-size:12px;color:var(--muted);margin-top:2px}
.kpis .v.bump{animation:bump .9s ease}
@keyframes bump{30%{color:var(--accent-2)}}

/* the session card on click */
.card{position:absolute;z-index:5;width:300px;padding:18px 18px 16px;border:1px solid var(--line-2);border-radius:14px;
background:rgba(36,31,30,.92);backdrop-filter:blur(16px);box-shadow:0 24px 60px rgba(0,0,0,.45);opacity:0;
transform:translateY(6px) scale(.98);transition:opacity .18s,transform .18s;pointer-events:none}
.card.on{opacity:1;transform:none;pointer-events:auto}
.card .lane{font-family:var(--mono);font-size:22px;font-weight:500;letter-spacing:-.01em;display:flex;align-items:center;gap:10px}
.card .lane span{color:var(--accent)}
.card .cities{color:var(--muted);font-size:12px;margin-top:2px}
.card dl{display:grid;grid-template-columns:86px 1fr;gap:6px 10px;margin:14px 0 0;font-size:13px}
.card dt{color:var(--muted)}.card dd{margin:0;text-align:right;font-variant-numeric:tabular-nums}
.card .bar{height:3px;background:var(--line-2);border-radius:3px;margin-top:14px;overflow:hidden}
.card .bar b{display:block;height:100%;background:linear-gradient(90deg,var(--accent),var(--accent-2))}
.card .go{margin-top:14px;display:flex;align-items:center;justify-content:space-between;height:36px;padding:0 14px;
border-radius:8px;background:var(--accent);color:#fff;font-weight:500;font-size:13px}
.card .go:hover{background:#e8673f}
.card .x{position:absolute;right:12px;top:10px;color:var(--muted);font-size:18px;cursor:pointer;line-height:1}
.pill{display:inline-flex;align-items:center;gap:6px;font-size:11px;letter-spacing:.08em;text-transform:uppercase}
.pill::before{content:"";width:6px;height:6px;border-radius:50%;background:currentColor}
.pill.fly{color:var(--accent-2)}.pill.ok{color:var(--ok)}.pill.q{color:var(--ink-2)}

/* the side: clock, bid race, feed */
aside{position:relative;z-index:4;border-left:1px solid var(--line);background:linear-gradient(180deg,#2b2524,#241f1e);
display:flex;flex-direction:column;min-height:0}
aside section{padding:22px 26px;border-bottom:1px solid var(--line)}
.clock .t{font-size:64px;font-weight:400;letter-spacing:-.045em;line-height:1;margin-top:10px;font-variant-numeric:tabular-nums}
.clock .t span{color:var(--muted);font-size:28px;letter-spacing:-.02em;margin-left:4px}
.clock .shift{margin-top:10px;color:var(--ink-2);font-size:14px}
.clock .shift b{color:var(--accent);font-weight:500}
.zones{display:grid;grid-template-columns:repeat(3,1fr);gap:8px;margin-top:16px}
.zones>div{border-top:1px solid var(--line-2);padding-top:8px}
.zones .c{font-size:11px;color:var(--muted);letter-spacing:.1em;text-transform:uppercase}
.zones .h{font-family:var(--mono);font-size:14px;margin-top:2px}

.race .head{display:flex;align-items:baseline;justify-content:space-between;gap:12px}
.race .lane{font-family:var(--mono);font-size:20px;margin-top:10px;display:flex;gap:10px;align-items:center}
.race .lane span{color:var(--accent)}
.race .meta{color:var(--muted);font-size:12px;margin-top:2px;min-height:18px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.race .timer{font-family:var(--mono);font-size:13px;color:var(--ink-2)}
.race .timer.done{color:var(--ok)}
.bids{margin-top:14px;display:flex;flex-direction:column;gap:9px;min-height:172px}
.bid{display:grid;grid-template-columns:minmax(0,1fr) auto;gap:4px 10px;font-size:12.5px;opacity:0;transform:translateX(-6px);
transition:opacity .35s,transform .35s}
.bid.in{opacity:1;transform:none}
.bid .nm{white-space:nowrap;overflow:hidden;text-overflow:ellipsis;color:var(--ink-2)}
.bid .p{font-family:var(--mono);font-variant-numeric:tabular-nums;color:var(--ink-2)}
.bid .tr{grid-column:1/-1;height:4px;border-radius:4px;background:rgba(255,236,224,.06);overflow:hidden}
.bid .tr b{display:block;height:100%;width:0;border-radius:4px;background:var(--ink-2);opacity:.45;transition:width 1.1s cubic-bezier(.2,.8,.2,1)}
.bid.win .nm,.bid.win .p{color:var(--ink)}
.bid.win .tr b{background:linear-gradient(90deg,var(--accent),var(--accent-2));opacity:1}
.bid.win .nm::after{content:"Best all-in";margin-left:8px;font-size:10px;letter-spacing:.1em;text-transform:uppercase;color:var(--accent)}
.bid.lose{opacity:.45}
.race .verdict{margin-top:12px;font-size:13px;color:var(--ink-2);min-height:20px}
.race .verdict b{color:var(--ink);font-weight:500}

.feed{position:relative;flex:1;min-height:0;overflow:hidden;display:flex;flex-direction:column;border-bottom:0}
.feed ol{list-style:none;margin:12px 0 0;padding:0;display:flex;flex-direction:column;position:relative}
.feed li{display:grid;grid-template-columns:62px 62px minmax(0,1fr);gap:10px;padding:9px 0;border-top:1px solid var(--line);
font-size:12.5px;animation:slide .5s cubic-bezier(.2,.8,.2,1)}
.feed li:first-child{border-top:0}
@keyframes slide{from{opacity:0;transform:translateY(-10px)}}
.feed .tm{font-family:var(--mono);font-size:11px;color:var(--muted);padding-top:1px}
.feed .k{font-size:10px;letter-spacing:.12em;text-transform:uppercase;padding-top:2px;color:var(--ink-2)}
.feed .k.in{color:var(--ink)}.feed .k.quote{color:var(--accent-2)}.feed .k.book{color:var(--accent)}.feed .k.land{color:var(--ok)}
.feed .tx{color:var(--ink-2);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.feed .tx b{color:var(--ink);font-weight:500;font-family:var(--mono);font-size:12px}
.feed::after{content:"";position:absolute;left:0;right:0;bottom:0;height:80px;background:linear-gradient(transparent,#241f1e);pointer-events:none}

@media (max-width:1100px){.hud h1{font-size:30px}.kpis .v{font-size:26px}}
@media (max-width:900px){body{display:block;overflow:auto}.stage{height:72vh;min-height:520px}
aside{border-left:0;border-top:1px solid var(--line)}.feed{min-height:420px}.legend{display:none}
.hud{left:16px;right:16px;top:16px}.hud h1{font-size:26px}.hud p{display:none}
.kpis{left:16px;right:16px;bottom:16px;grid-template-columns:repeat(2,minmax(0,1fr))}
.kpis>div:nth-child(3){border-left:0}.kpis>div:nth-child(n+3){border-top:1px solid var(--line)}
.kpis>div{padding:12px 14px}.kpis .v{font-size:22px}}
@media (prefers-reduced-motion:reduce){.dot::after{animation:none}.feed li{animation:none}}
</style></head>
<body>
<div class="stage" id="stage">
  <canvas id="land"></canvas><div class="grid"></div><canvas id="fx"></canvas>
  <div class="hud">
    <div>
      <div class="brand"><a href="/" class="mark"><i></i>PIXEL</a><span class="sep"></span>
        <span class="eyebrow">Air-freight control tower</span></div>
      <h1>Nobody likes doing logistics, <em>we automate&nbsp;it.</em></h1>
      <p>Every request, every carrier bid and every booking, quoted and flown from Germany to the world, around the clock.</p>
    </div>
    <div class="right">
      <span class="chip"><span class="dot"></span>Live</span>
      <a class="chip" href="/requests">Requests</a>
      <a class="chip" href="#" id="fs" title="Full screen (F)">Full screen</a>
    </div>
  </div>
  <div class="legend">
    <div><i class="h"></i>Gateway hub</div>
    <div><i class="q"></i>Request being quoted</div>
    <div><i></i>Booked, in the air</div>
    <div><i class="d"></i>Destination</div>
  </div>
  <div class="kpis">
    <div><div class="eyebrow">Requests answered</div><div class="v" id="k-ans">0</div><div class="n">every one, before its deadline</div></div>
    <div><div class="eyebrow">Median first answer</div><div class="v" id="k-med">0<small>s</small></div><div class="n">a desk takes hours at night</div></div>
    <div><div class="eyebrow">Shipments booked</div><div class="v" id="k-book">0</div><div class="n">airline and trucker confirmed</div></div>
    <div><div class="eyebrow">Freight in the air</div><div class="v" id="k-kg">0<small>kg</small></div><div class="n" id="k-lanes">on 0 lanes right now</div></div>
  </div>
  <div class="card" id="card"></div>
</div>
<aside>
  <section class="clock">
    <div class="eyebrow">Munich · local time</div>
    <div class="t" id="clock">--:--<span>:--</span></div>
    <div class="shift" id="shift"></div>
    <div class="zones" id="zones"></div>
  </section>
  <section class="race">
    <div class="head"><span class="eyebrow">Carrier bid race</span><span class="timer" id="r-timer">00:00</span></div>
    <div class="lane" id="r-lane">— <span>→</span> —</div>
    <div class="meta" id="r-meta">Waiting for the next request</div>
    <div class="bids" id="r-bids"></div>
    <div class="verdict" id="r-verdict"></div>
  </section>
  <section class="feed">
    <div class="eyebrow">Event log</div>
    <ol id="feed"></ol>
  </section>
</aside>
<script>
const DATA = __DATA__;
const LAND = __LAND__;
__AIRPORTS__

/* ---------- lanes: the CRM's, else demo ---------- */
const HUBS = ["FRA","MUC","DUS","HAM","STR","CGN"];
const DEMO_DEST = ["JFK","ORD","ATL","MIA","LAX","YYZ","MEX","BOG","GRU","EZE","SCL","LIM","LOS","NBO","JNB","CAI","TLV",
"DXB","DOH","RUH","BOM","DEL","BKK","SIN","KUL","HKG","PVG","PEK","ICN","NRT","TPE","MNL","SGN","SYD","MEL","ULN","KTM","TNR"];
const DEMO_CARRIERS = ["Nordwind Air Cargo","Altair Freight","Meridian Skylift","Kestrel Cargo Lines","Sable Air Freight","Halcyon Cargo","Ostrava Wings"];
const DEMO_CUSTOMERS = ["Wonka Industries","Helix Medical","Brightline Optics","Kranich Maschinenbau","Novum Pharma","Aster Robotics","Lumen Textiles","Vela Automotive"];
const rnd=(a,b)=>a+Math.random()*(b-a), pick=a=>a[Math.floor(Math.random()*a.length)];
const known = c => AP[c] && AP[c][2] >= -146 && AP[c][2] <= 174;
let LANES = (DATA.lanes||[]).filter(l=>known(l.o)&&known(l.d)&&l.o!==l.d).map(l=>({...l,real:true,kg:+l.kg,eur:+l.eur}));
const CARRIERS = (DATA.carriers||[]).length ? DATA.carriers : DEMO_CARRIERS.map(n=>({name:n,serves:[]}));
function demoLane(){const o=pick(HUBS), d=pick(DEMO_DEST);return {o,d,carrier:pick(CARRIERS).name,kg:Math.round(rnd(60,2400)/10)*10,
  eur:null,days:Math.round(rnd(2,7)),customer:pick(DEMO_CUSTOMERS),session:null,real:false};}
function nextLane(){return LANES.length && Math.random()<.85 ? {...pick(LANES)} : demoLane();}

/* ---------- geometry ---------- */
const LON_C=14, SPAN=320, LAT_TOP=80, LAT_BOT=-58;
const LON_MIN=LON_C-SPAN/2;
let W=0,H=0,DPR=1,S=1,OX=0,OY=0;
const stage=document.getElementById('stage'), landCv=document.getElementById('land'), fx=document.getElementById('fx');
const lctx=landCv.getContext('2d'), ctx=fx.getContext('2d');
function proj(lat,lon){const l=((lon-LON_MIN)%360+360)%360;return [OX+l*S, OY+(LAT_TOP-lat)*S];}
const rad=Math.PI/180;
function gc(a,b,n){ // great-circle points between two [lat,lon]
  const v=([la,lo])=>[Math.cos(la*rad)*Math.cos(lo*rad),Math.cos(la*rad)*Math.sin(lo*rad),Math.sin(la*rad)];
  const A=v(a),B=v(b);const w=Math.acos(Math.min(1,A[0]*B[0]+A[1]*B[1]+A[2]*B[2]))||1e-6;const out=[];
  for(let i=0;i<=n;i++){const t=i/n,k1=Math.sin((1-t)*w)/Math.sin(w),k2=Math.sin(t*w)/Math.sin(w);
    const x=k1*A[0]+k2*B[0],y=k1*A[1]+k2*B[1],z=k1*A[2]+k2*B[2];
    out.push([Math.atan2(z,Math.hypot(x,y))/rad,Math.atan2(y,x)/rad]);}
  return {pts:out,km:w*6371};
}
function route(o,d){
  const g=gc([AP[o][1],AP[o][2]],[AP[d][1],AP[d][2]],90);
  let P=g.pts.map(([la,lo])=>proj(la,lo));
  const [x0,y0]=P[0],[x1,y1]=P[P.length-1];let dx=x1-x0,dy=y1-y0;const L=Math.hypot(dx,dy)||1;
  let nx=dy/L,ny=-dx/L;if(ny>0){nx=-nx;ny=-ny;}
  const lift=Math.min(70,L*.16);
  P=P.map(([x,y],i)=>{const s=Math.sin(Math.PI*i/(P.length-1))*lift;return [x+nx*s,y+ny*s];});
  const seg=[0];for(let i=1;i<P.length;i++)seg.push(seg[i-1]+Math.hypot(P[i][0]-P[i-1][0],P[i][1]-P[i-1][1]));
  return {P,seg,len:seg[seg.length-1],km:g.km};
}
function at(r,t){ // point and angle at fraction t of the route length
  const want=t*r.len;let i=1;while(i<r.seg.length-1&&r.seg[i]<want)i++;
  const a=r.P[i-1],b=r.P[i],f=(want-r.seg[i-1])/((r.seg[i]-r.seg[i-1])||1);
  return [a[0]+(b[0]-a[0])*f,a[1]+(b[1]-a[1])*f,Math.atan2(b[1]-a[1],b[0]-a[0])];
}

/* ---------- land: dot matrix with live day and night ---------- */
const DOTS=[];(function(){const st=LAND.step;LAND.rows.forEach((row,r)=>{const lat=LAND.lat0-r*st;let col=0,land=false;
  for(const n of row.split('.').map(Number)){if(land)for(let k=0;k<n;k++)DOTS.push([lat,-180+st/2+(col+k)*st]);col+=n;land=!land;}});})();
function sun(){const now=new Date();const d=(Date.UTC(now.getUTCFullYear(),now.getUTCMonth(),now.getUTCDate())-Date.UTC(now.getUTCFullYear(),0,0))/864e5;
  const dec=-23.44*Math.cos(2*Math.PI/365*(d+10));const h=now.getUTCHours()+now.getUTCMinutes()/60;return {dec,lon:(12-h)*15};}
function drawLand(){
  lctx.setTransform(DPR,0,0,DPR,0,0);lctx.clearRect(0,0,W,H);
  const s=sun(),sd=Math.sin(s.dec*rad),cd=Math.cos(s.dec*rad),r=Math.max(.9,S*.29);
  for(const [lat,lon] of DOTS){const [x,y]=proj(lat,lon);if(x<-4||x>W+4)continue;
    const cz=Math.sin(lat*rad)*sd+Math.cos(lat*rad)*cd*Math.cos((lon-s.lon)*rad);
    const day=Math.min(1,Math.max(0,(cz+.08)/.22));
    const dh=Math.hypot(lat-50.5,(lon-9.5)*.65),heat=Math.max(0,1-dh/11);
    const a=.13+.21*day+.4*heat;
    const R=Math.round(230+25*heat),G=Math.round(218-110*heat),B=Math.round(206-150*heat);
    lctx.fillStyle=`rgba(${R},${G},${B},${a.toFixed(3)})`;lctx.beginPath();lctx.arc(x,y,r,0,7);lctx.fill();}
}

/* ---------- state ---------- */
const flights=[], rings=[];let quoting=null;const touched=new Map(); // airport -> last activity ms
const kpi={ans:+(DATA.stats&&DATA.stats.answered)||0,book:+(DATA.stats&&DATA.stats.booked)||0,
  med:(DATA.stats&&DATA.stats.median)||null,times:[]};
function priceFor(l,km){if(l.eur)return l.eur;const perKg=1.6+km/3800;return Math.round(Math.max(95,l.kg*perKg*rnd(.9,1.12)));}
function addFlight(l,t0){const r=route(l.o,l.d);const km=r.km;
  const f={l,r,t:t0||0,dur:rnd(16,30)*(.6+km/14000),eur:priceFor(l,km),ref:'PX-'+Math.random().toString(16).slice(2,8).toUpperCase()};
  flights.push(f);touched.set(l.o,performance.now());return f;}
function ring(code,color,max,dur){const a=AP[code];if(!a)return;rings.push({code,color,max,dur,t0:performance.now()});}

/* ---------- feed ---------- */
const feedEl=document.getElementById('feed');
const fmt=new Intl.DateTimeFormat('en-GB',{timeZone:'Europe/Berlin',hour:'2-digit',minute:'2-digit',second:'2-digit',hour12:false});
const eur=v=>'€'+Math.round(v).toLocaleString('en-US');
function log(kind,label,html){const li=document.createElement('li');
  li.innerHTML=`<span class="tm">${fmt.format(new Date())}</span><span class="k ${kind}">${label}</span><span class="tx">${html}</span>`;
  feedEl.prepend(li);while(feedEl.children.length>14)feedEl.lastChild.remove();}
const esc=s=>String(s??'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));

/* ---------- KPIs ---------- */
function setK(id,v,unit){const el=document.getElementById(id);const html=v+(unit?`<small>${unit}</small>`:'');
  if(el.innerHTML!==html){el.innerHTML=html;el.classList.remove('bump');void el.offsetWidth;el.classList.add('bump');}}
function countUp(id,to,unit){const el=document.getElementById(id);const t0=performance.now();
  (function step(){const k=Math.min(1,(performance.now()-t0)/1400),e=1-Math.pow(1-k,3);
    el.innerHTML=Math.round(to*e).toLocaleString('en-US')+(unit?`<small>${unit}</small>`:'');if(k<1)requestAnimationFrame(step);})();}
function median(a){const s=[...a].sort((x,y)=>x-y);return s.length?s[Math.floor(s.length/2)]:null;}
function kpisFlight(){const kg=flights.reduce((s,f)=>s+f.l.kg,0);setK('k-kg',Math.round(kg).toLocaleString('en-US'),'kg');
  const lanes=new Set(flights.map(f=>f.l.o+f.l.d)).size;document.getElementById('k-lanes').textContent=`on ${lanes} lanes right now`;}

/* ---------- the bid race: one request at a time ---------- */
const R={lane:document.getElementById('r-lane'),meta:document.getElementById('r-meta'),bids:document.getElementById('r-bids'),
  timer:document.getElementById('r-timer'),verdict:document.getElementById('r-verdict')};
let raceStart=0,raceSecs=null,raceDur=1;
function runRequest(){
  const l=nextLane();const r=route(l.o,l.d);const km=r.km;
  quoting={l,r,t0:performance.now(),p:0};touched.set(l.o,performance.now());touched.set(l.d,performance.now());
  ring(l.o,'245,238,233',38,1400);
  log('in','Request',`<b>${l.o}→${l.d}</b> ${Math.round(l.kg)} kg · ${esc(l.customer||'new customer')}`);
  R.lane.innerHTML=`${l.o} <span>→</span> ${l.d}`;
  R.meta.textContent=`${AP[l.o][0]} to ${AP[l.d][0]} · ${Math.round(l.kg).toLocaleString('en-US')} kg · ${Math.round(km).toLocaleString('en-US')} km`;
  R.verdict.textContent='';R.timer.className='timer';raceStart=performance.now();
  raceSecs=l.secs&&l.secs<240?l.secs:Math.round(rnd(24,58));
  // bidders: carriers serving this destination, the winner first
  const serving=CARRIERS.filter(c=>!c.serves.length||c.serves.includes(l.d)).map(c=>c.name);
  let pool=[...new Set([l.carrier,...serving.sort(()=>Math.random()-.5)])].filter(Boolean);
  if(pool.length<3)pool=[...new Set([...pool,...CARRIERS.map(c=>c.name).sort(()=>Math.random()-.5)])];
  pool=pool.slice(0,Math.min(5,Math.max(3,pool.length)));
  const best=priceFor(l,km);const bids=pool.map((n,i)=>({n,p:i?Math.round(best*rnd(1.04,1.32)):best})).sort(()=>Math.random()-.5);
  const max=Math.max(...bids.map(b=>b.p));
  R.bids.innerHTML=bids.map((b,i)=>`<div class="bid" data-i="${i}"><span class="nm">${esc(b.n)}</span><span class="p">—</span><div class="tr"><b></b></div></div>`).join('');
  const span=Math.min(raceSecs,40)*1000*.12+2600;raceDur=span+900;
  bids.forEach((b,i)=>setTimeout(()=>{const el=R.bids.children[i];if(!el)return;el.classList.add('in');
    el.querySelector('.p').textContent=eur(b.p);el.querySelector('b').style.width=(28+72*b.p/max)+'%';},300+span*(i+1)/(bids.length+1)));
  setTimeout(()=>{ // the quote leaves
    const w=bids.find(b=>b.p===best);[...R.bids.children].forEach((el,i)=>el.classList.add(bids[i]===w?'win':'lose'));
    R.timer.className='timer done';R.timer.textContent=`answered in ${raceSecs}s`;
    R.verdict.innerHTML=`Quoted <b>${eur(best)}</b> all-in, ${w.n===l.carrier?'':'via '}<b>${esc(w.n)}</b> · ${l.days||Math.round(rnd(2,6))} days`;
    kpi.ans++;kpi.times.push(raceSecs);setK('k-ans',kpi.ans.toLocaleString('en-US'));
    const m=kpi.med&&kpi.times.length<5?kpi.med:median(kpi.times);setK('k-med',m,'s');
    log('quote','Quoted',`<b>${l.o}→${l.d}</b> ${eur(best)} in ${raceSecs}s`);ring(l.o,'222,85,49',46,1600);
    quoting.done=performance.now();
    if(Math.random()<.62)setTimeout(()=>{ // the customer books
      const f=addFlight({...l,carrier:w.n,eur:best});kpi.book++;setK('k-book',kpi.book.toLocaleString('en-US'));
      log('book','Booked',`<b>${l.o}→${l.d}</b> ${esc(w.n)} · ${f.ref}`);quoting=null;kpisFlight();},2600);
    else setTimeout(()=>{quoting=null;},2600);
  },span+900);
}
setInterval(()=>{if(raceStart&&!R.timer.classList.contains('done')){
  const sec=Math.min(raceSecs,Math.round((performance.now()-raceStart)/raceDur*raceSecs));R.timer.textContent='00:'+String(sec).padStart(2,'0');}},100);

/* ---------- draw loop ---------- */
let last=performance.now();
function frame(now){
  const dt=Math.min(.05,(now-last)/1000);last=now;
  ctx.setTransform(DPR,0,0,DPR,0,0);ctx.clearRect(0,0,W,H);placed=[];
  // faint network of every flight's route
  ctx.lineWidth=1;
  for(const f of flights){ctx.strokeStyle='rgba(222,85,49,.2)';ctx.beginPath();f.r.P.forEach(([x,y],i)=>i?ctx.lineTo(x,y):ctx.moveTo(x,y));ctx.stroke();}
  // flights: trail and glowing head
  ctx.globalCompositeOperation='lighter';
  for(let i=flights.length-1;i>=0;i--){const f=flights[i];f.t+=dt/f.dur;
    if(f.t>=1){ring(f.l.d,'88,200,150',34,1800);touched.set(f.l.d,now);
      if(Math.random()<.5)log('land','Landed',`<b>${f.l.o}→${f.l.d}</b> ${Math.round(f.l.kg)} kg delivered · ${f.ref}`);
      flights.splice(i,1);kpisFlight();continue;}
    const tail=Math.max(0,f.t-.2),N=22;
    for(let k=0;k<N;k++){const a=at(f.r,tail+(f.t-tail)*k/N),b=at(f.r,tail+(f.t-tail)*(k+1)/N);const q=(k+1)/N;
      ctx.strokeStyle=`rgba(232,110,72,${(q*q*.85).toFixed(3)})`;ctx.lineWidth=.6+q*1.6;ctx.beginPath();ctx.moveTo(a[0],a[1]);ctx.lineTo(b[0],b[1]);ctx.stroke();}
    const [x,y]=at(f.r,f.t);const g=ctx.createRadialGradient(x,y,0,x,y,14);g.addColorStop(0,'rgba(240,138,98,.75)');g.addColorStop(1,'rgba(222,85,49,0)');
    ctx.fillStyle=g;ctx.beginPath();ctx.arc(x,y,14,0,7);ctx.fill();ctx.fillStyle='#fff3ec';ctx.beginPath();ctx.arc(x,y,1.9,0,7);ctx.fill();
    f.x=x;f.y=y;}
  ctx.globalCompositeOperation='source-over';
  // the request being quoted: a dashed line drawing out
  if(quoting){const q=quoting;q.p=Math.min(1,(now-q.t0)/2600);
    const n=Math.max(2,Math.round(q.r.P.length*q.p));ctx.setLineDash([3,5]);ctx.lineDashOffset=-now/40;
    ctx.strokeStyle=q.done?'rgba(240,138,98,.9)':'rgba(245,238,233,.75)';ctx.lineWidth=1.4;ctx.beginPath();
    for(let i=0;i<n;i++){const [x,y]=q.r.P[i];i?ctx.lineTo(x,y):ctx.moveTo(x,y);}ctx.stroke();ctx.setLineDash([]);
    const [hx,hy]=q.r.P[n-1];ctx.fillStyle='#f5eee9';ctx.beginPath();ctx.arc(hx,hy,2.4,0,7);ctx.fill();
    label(q.l.d,1,true);label(q.l.o,1,true,true);}
  // airports
  const dests=new Set(flights.map(f=>f.l.d));if(quoting)dests.add(quoting.l.d);
  for(const c of dests){const [x,y]=proj(AP[c][1],AP[c][2]);ctx.fillStyle='rgba(245,238,233,.85)';ctx.beginPath();ctx.arc(x,y,2.2,0,7);ctx.fill();
    const age=(now-(touched.get(c)||0))/1000;if(!quoting||quoting.l.d!==c)label(c,age<4?1:.42,false);}
  for(const c of HUBS){const [x,y]=proj(AP[c][1],AP[c][2]);const ph=((now/1000+HUBS.indexOf(c)*.33)%2.2)/2.2;
    ctx.strokeStyle=`rgba(222,85,49,${(.7*(1-ph)).toFixed(3)})`;ctx.lineWidth=1;ctx.beginPath();ctx.arc(x,y,3+ph*13,0,7);ctx.stroke();
    ctx.fillStyle='#DE5531';ctx.beginPath();ctx.arc(x,y,2.6,0,7);ctx.fill();}
  hubLabel();
  // event rings
  for(let i=rings.length-1;i>=0;i--){const r=rings[i],k=(now-r.t0)/r.dur;if(k>=1){rings.splice(i,1);continue;}
    const [x,y]=proj(AP[r.code][1],AP[r.code][2]);const e=1-Math.pow(1-k,3);
    ctx.strokeStyle=`rgba(${r.color},${(1-k).toFixed(3)})`;ctx.lineWidth=1.5;ctx.beginPath();ctx.arc(x,y,4+e*r.max,0,7);ctx.stroke();}
  if(selected)drawSelected();
  requestAnimationFrame(frame);
}
let placed=[];function label(c,alpha,strong,left){const [x,y]=proj(AP[c][1],AP[c][2]);
  if(!strong&&placed.some(([px,py])=>Math.abs(px-x)<30&&Math.abs(py-y)<12))return;placed.push([x,y]);
  ctx.font=`500 ${strong?11:10}px "JetBrains Mono",ui-monospace,monospace`;ctx.textBaseline='middle';
  if(left){ctx.textAlign='right';ctx.fillStyle=`rgba(245,238,233,${alpha})`;ctx.fillText(c,x-30,y-22);
    ctx.strokeStyle='rgba(245,238,233,.35)';ctx.lineWidth=1;ctx.beginPath();ctx.moveTo(x-4,y-4);ctx.lineTo(x-26,y-22);ctx.stroke();return;}
  ctx.textAlign='left';ctx.fillStyle=`rgba(245,238,233,${alpha})`;ctx.fillText(c,x+7,y-7);
  if(strong){ctx.font='400 10px Jost,sans-serif';ctx.fillStyle='rgba(207,197,190,.75)';ctx.fillText(AP[c][0],x+7,y+6);}}
function hubLabel(){const [x,y]=proj(54.2,9.5);ctx.textAlign='left';ctx.textBaseline='alphabetic';
  ctx.font='500 10px Jost,sans-serif';ctx.fillStyle='rgba(240,138,98,.9)';ctx.fillText('GERMANY · 6 GATEWAYS',x+18,y-30);
  ctx.strokeStyle='rgba(240,138,98,.4)';ctx.lineWidth=1;ctx.beginPath();ctx.moveTo(x+2,y-2);ctx.lineTo(x+14,y-26);ctx.lineTo(x+150,y-26);ctx.stroke();}

/* ---------- click: open a session card ---------- */
const card=document.getElementById('card');let selected=null;
function drawSelected(){const f=selected;if(!flights.includes(f)){closeCard();return;}
  ctx.strokeStyle='rgba(245,238,233,.9)';ctx.lineWidth=1.2;ctx.beginPath();f.r.P.forEach(([x,y],i)=>i?ctx.lineTo(x,y):ctx.moveTo(x,y));ctx.stroke();
  ctx.strokeStyle='rgba(245,238,233,.9)';ctx.beginPath();ctx.arc(f.x,f.y,9,0,7);ctx.stroke();
  const b=card.querySelector('.bar b');if(b)b.style.width=(f.t*100).toFixed(1)+'%';
  const eta=card.querySelector('[data-eta]');if(eta)eta.textContent=Math.max(1,Math.round((1-f.t)*(f.l.days||3)*24))+' h';}
function openCard(f){selected=f;const l=f.l;const href=l.session?`/sessions/${l.session}`:'/requests';
  card.innerHTML=`<span class="x" id="cx">×</span><span class="pill fly">In the air</span>
  <div class="lane" style="margin-top:10px">${l.o} <span>→</span> ${l.d}</div><div class="cities">${AP[l.o][0]} to ${AP[l.d][0]} · ${Math.round(f.r.km).toLocaleString('en-US')} km</div>
  <dl><dt>Customer</dt><dd>${esc(l.customer||'—')}</dd><dt>Carrier</dt><dd>${esc(l.carrier)}</dd><dt>Weight</dt><dd>${Math.round(l.kg).toLocaleString('en-US')} kg</dd>
  <dt>All-in</dt><dd>${eur(f.eur)}</dd><dt>Reference</dt><dd class="mono">${f.ref}</dd><dt>Arrives in</dt><dd data-eta>—</dd></dl>
  <div class="bar"><b></b></div><a class="go" href="${href}">Open session <span>→</span></a>`;
  const sr=stage.getBoundingClientRect();let x=f.x+18,y=f.y-40;if(x+310>sr.width)x=f.x-318;y=Math.max(90,Math.min(y,sr.height-330));
  card.style.left=x+'px';card.style.top=y+'px';card.classList.add('on');document.getElementById('cx').onclick=closeCard;}
function closeCard(){selected=null;card.classList.remove('on');}
fx.addEventListener('click',e=>{const r=fx.getBoundingClientRect(),mx=e.clientX-r.left,my=e.clientY-r.top;
  let best=null,bd=22;for(const f of flights){const d=Math.hypot(f.x-mx,f.y-my);if(d<bd){bd=d;best=f;}}
  if(!best){bd=10;for(const f of flights)for(const [x,y] of f.r.P){const d=Math.hypot(x-mx,y-my);if(d<bd){bd=d;best=f;}}}
  best?openCard(best):closeCard();});
fx.addEventListener('mousemove',e=>{const r=fx.getBoundingClientRect(),mx=e.clientX-r.left,my=e.clientY-r.top;
  fx.style.cursor=flights.some(f=>Math.hypot(f.x-mx,f.y-my)<22)?'pointer':'crosshair';});

/* ---------- clock ---------- */
const zones=[['New York','America/New_York'],['Dubai','Asia/Dubai'],['Shanghai','Asia/Shanghai']];
function tick(){const now=new Date();const p=Object.fromEntries(new Intl.DateTimeFormat('en-GB',{timeZone:'Europe/Berlin',hour:'2-digit',minute:'2-digit',second:'2-digit',hour12:false}).formatToParts(now).map(x=>[x.type,x.value]));
  document.getElementById('clock').innerHTML=`${p.hour}:${p.minute}<span>:${p.second}</span>`;const h=+p.hour;
  document.getElementById('shift').innerHTML=h>=20||h<7?'Night shift. The desk is dark, <b>PIXEL is answering.</b>':'Day shift. <b>PIXEL</b> takes the first answer, people take the calls.';
  document.getElementById('zones').innerHTML=zones.map(([c,tz])=>`<div><div class="c">${c}</div><div class="h">${new Intl.DateTimeFormat('en-GB',{timeZone:tz,hour:'2-digit',minute:'2-digit',hour12:false}).format(now)}</div></div>`).join('');}
tick();setInterval(tick,1000);

/* ---------- layout ---------- */
function resize(){const r=stage.getBoundingClientRect();W=r.width;H=r.height;DPR=Math.min(2,window.devicePixelRatio||1);
  for(const c of [landCv,fx]){c.width=Math.round(W*DPR);c.height=Math.round(H*DPR);}
  const rows=LAT_TOP-LAT_BOT;S=Math.min(W/SPAN*1.04,(H*.92)/rows);OX=(W-SPAN*S)/2;OY=Math.max(20,(H-rows*S)/2+H*.01);
  for(const f of flights)f.r=route(f.l.o,f.l.d);if(quoting)quoting.r=route(quoting.l.o,quoting.l.d);drawLand();}
window.addEventListener('resize',()=>{clearTimeout(window._rz);window._rz=setTimeout(resize,120);});
setInterval(drawLand,60000);
document.getElementById('fs').onclick=e=>{e.preventDefault();document.fullscreenElement?document.exitFullscreen():document.documentElement.requestFullscreen();};
document.addEventListener('keydown',e=>{if(e.key==='f'||e.key==='F')document.getElementById('fs').click();if(e.key==='Escape')closeCard();});

/* ---------- start ---------- */
resize();
for(let i=0;i<16;i++)addFlight(nextLane(),Math.random()*.9);
countUp('k-ans',kpi.ans);countUp('k-book',kpi.book);setK('k-med',kpi.med||38,'s');kpisFlight();
[['book','Booked'],['quote','Quoted'],['land','Landed']].forEach(([k,lb],i)=>{const f=flights[i];
  log(k,lb,`<b>${f.l.o}→${f.l.d}</b> ${k==='land'?Math.round(f.l.kg)+' kg delivered':esc(f.l.carrier)}`);});
setTimeout(runRequest,1200);setInterval(runRequest,9000);
setInterval(()=>{if(flights.length<18){const f=addFlight(nextLane());log('book','Booked',`<b>${f.l.o}→${f.l.d}</b> ${esc(f.l.carrier)} · ${f.ref}`);
  kpi.book++;setK('k-book',kpi.book.toLocaleString('en-US'));kpisFlight();}},4200);
requestAnimationFrame(frame);
</script></body></html>"""
