"""Night shift: how the service repairs itself. A round is lost, `core.feedback` says why, a change that names the
round (or its reason) is merged, the host's deploy job tests it and puts it live in a safe window, the next round is
accepted. Built from `feedback`, `quote.stuck`, `error` and `quote.checked` events, the deploy job's log and the
first-parent history of the live checkout; reads only, and the log and git run off the event loop."""

import asyncio
import json
import re
import subprocess
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from statistics import median

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from pixel.core.context import Context
from pixel.ui.page import clock, esc, layout

router = APIRouter()

CD_LOG = Path("/opt/pixel/cd/cd.log")  # deploy/cd.sh writes it as root, readable by the service's user
PULL = "https://github.com/lucakammerer/pixel/pull/"
MOVE = re.compile(r"^(\d\d-\d\d \d\d:\d\d:\d\d) (?:deployed (\w+)\.\.(\w+), restarted|(\w+)\.\.(\w+) no restart)")
GREEN = re.compile(r"^(\d\d-\d\d \d\d:\d\d:\d\d) (\w+) green, waiting for the window( \(urgent\))?")
NAMED = re.compile(r"\brounds? (\d+)(?:(?: to |[–-])(\d+))?", re.IGNORECASE)
MERGE = re.compile(r"^Merge pull request #(\d+)")
SIGNAL_LEAD = timedelta(minutes=6)  # a round's signals start with its request, a round before its verdict


def _at(ts: str) -> datetime:
    at = datetime.fromisoformat(ts)
    return at if at.tzinfo else at.replace(tzinfo=UTC)


def rounds(ctx: Context) -> list[dict]:
    """The latest verdict per scored round, oldest first. Lost: not accepted, or accepted with no points."""
    rows = ctx.db.execute("SELECT ts, session_id, payload FROM events WHERE kind = 'feedback' ORDER BY id").fetchall()
    found: dict[int, dict] = {}
    for row in rows:
        p = json.loads(row["payload"])
        if p.get("round") is None:
            continue
        points = p.get("points", p.get("hidden"))
        lost = p.get("verdict") != "accepted" or (points is not None and Decimal(str(points)) == 0)
        reason = p.get("reason") or (p.get("verdict") if p.get("verdict") != "accepted" else "0 points")
        found[int(p["round"])] = {"round": int(p["round"]), "at": _at(row["ts"]), "session": row["session_id"],
                                  "lost": lost, "reason": reason if lost else None, "points": points}  # fmt: skip
    return sorted(found.values(), key=lambda r: r["round"])


def signals(ctx: Context) -> list[tuple[datetime, str]]:
    """What the service raised on its own: watchdog alerts, errors in our code, quotes the check doubted."""
    rows = ctx.db.execute(
        "SELECT ts, kind, payload FROM events WHERE kind IN ('quote.stuck', 'error', 'quote.checked')"
    ).fetchall()
    found = []
    for row in rows:
        p = json.loads(row["payload"] or "{}")
        if row["kind"] == "quote.stuck":
            found.append((_at(row["ts"]), "watchdog alert"))
        elif row["kind"] == "error" and p.get("where") not in ("alert", "poll"):
            found.append((_at(row["ts"]), "error"))
        elif row["kind"] == "quote.checked" and p.get("blocking"):
            found.append((_at(row["ts"]), "doubtful quote"))
    return found


def _log_time(stamp: str, now: datetime) -> datetime:
    at = datetime.strptime(f"{now.year}-{stamp}", "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC)
    return at.replace(year=now.year - 1) if at > now + timedelta(days=1) else at


def deploy_log(text: str, now: datetime) -> tuple[list[dict], dict[str, tuple[datetime, bool]]]:
    """Moves of the live checkout (time, commit) and, per tested commit, when it was green and whether URGENT."""
    moves, green = [], {}
    for line in text.splitlines():
        if m := MOVE.match(line):
            moves.append({"at": _log_time(m[1], now), "to": m[3] or m[5]})
        elif m := GREEN.match(line):
            green.setdefault(m[2], (_log_time(m[1], now), bool(m[3])))
    return moves, green


def history() -> list[dict]:
    """First-parent commits of the live checkout, oldest first: hash, merge time, pull request, title, message."""
    out = subprocess.run(
        ["git", "log", "--first-parent", "-n", "500", "--format=%H%x1f%cI%x1f%s%x1f%b%x1e"],
        check=False, capture_output=True, text=True, timeout=5,
    ).stdout  # fmt: skip
    found = []
    for record in out.split("\x1e"):
        if record.strip():
            sha, at, subject, body = record.strip("\n").split("\x1f")
            pr = MERGE.match(subject)
            title = body.strip().splitlines()[0] if pr and body.strip() else subject
            found.append({"sha": sha, "at": _at(at), "pr": pr and pr[1], "title": title, "text": f"{subject}\n{body}"})
    return found[::-1]


def live_at(commits: list[dict], moves: list[dict]) -> None:
    """Sets `live` (and `deploy`, the commit the job moved to) on every commit a move of the live checkout carried."""
    index = {c["sha"][:7]: i for i, c in enumerate(commits)}
    for move in sorted(moves, key=lambda m: m["at"]):
        end = index.get(move["to"][:7])
        for c in commits[: end + 1] if end is not None else ():
            c.setdefault("live", move["at"])
            c.setdefault("deploy", move["to"][:7])


def named(text: str) -> set[int]:
    return {n for a, b in NAMED.findall(text) for n in range(int(a), int(b or a) + 1)}


def incidents(found: list[dict], commits: list[dict], sigs: list[tuple[datetime, str]]) -> list[dict]:
    """Runs of lost rounds, newest first. A change merged after the first loss is about the run when it names one of
    its rounds, or names its reason and no round at all. The fix is the first such change live after the run's last
    loss; one live before that is an attempt that did not hold. Time to repair: first loss to fix live."""
    runs: list[list[dict]] = []
    for r in found:
        if r["lost"] and runs and runs[-1][-1]["round"] == r["round"] - 1:
            runs[-1].append(r)
        elif r["lost"]:
            runs.append([r])
    result = []
    for run in runs:
        first, last, nums = run[0], run[-1], {r["round"] for r in run}
        reasons = [x for x in {r["reason"] for r in run} if "_" in x]  # the scorer's codes, not "no answer"
        about = [
            c for c in commits
            if c.get("live") and c["at"] > first["at"]
            and (named(c["text"]) & nums or not named(c["text"]) and any(x in c["text"] for x in reasons))
        ]  # fmt: skip
        fix = next((c for c in about if c["live"] > last["at"]), None)
        item = {"run": run, "first": first, "fix": fix, "tried": [c for c in about if c["live"] <= last["at"]]}
        if fix:
            item["with"] = [c for c in commits if c.get("deploy") == fix["deploy"] and c is not fix and c.get("pr")]
            item["back"] = next((r for r in found if not r["lost"] and r["at"] > fix["live"]), None)
            item["repair"] = fix["live"] - first["at"]
            item["signals"] = [k for at, k in sigs if first["at"] - SIGNAL_LEAD <= at <= fix["live"]]
        result.append(item)
    return result[::-1]


def _read_log() -> str:
    try:
        return CD_LOG.read_text()
    except OSError:  # no deploy job here (a laptop): the page shows the rounds only
        return ""


def _deploys() -> tuple[list[dict], dict]:
    now = datetime.now(UTC)
    moves, green = deploy_log(_read_log(), now)
    commits = history()
    live_at(commits, moves)
    return commits, green


def _span(d: timedelta) -> str:
    m = round(d.total_seconds() / 60)
    return f"{m} min" if m < 90 else f"{m // 60} h {m % 60:02d} min"


def _rounds_label(run: list[dict]) -> str:
    a, b = run[0]["round"], run[-1]["round"]
    return f"Round {a}" if a == b else f"Rounds {a}–{b}"


def _reasons(run: list[dict]) -> str:
    counts: dict[str, int] = {}
    for r in run:
        counts[r["reason"]] = counts.get(r["reason"], 0) + 1
    return " ".join(
        f"<span class=why>{esc(k.replace('_', ' '))}{f' ×{n}' if n > 1 else ''}</span>" for k, n in counts.items()
    )


def _hm(at: datetime | None) -> str:
    return clock(at)[:5] if at else "—"


def _round_link(r: dict) -> str:
    return f"<a href='/sessions/{r['session']}'>{r['round']}</a>" if r["session"] else str(r["round"])


CSS = """<style>
.ns-hero{padding:36px 0 8px}
.ns-hero h1{font-size:44px;line-height:1.05;letter-spacing:-.035em;font-weight:500;margin:0 0 12px}
.ns-hero h1 em{font-style:normal;background:linear-gradient(90deg,var(--accent),var(--accent-2));-webkit-background-clip:text;
background-clip:text;color:transparent}
.ns-hero p{font-size:17px;color:var(--ink-2);max-width:780px;margin:0;text-wrap:pretty}
.ns-kpis{display:grid;grid-template-columns:repeat(5,minmax(0,1fr));gap:12px;margin:28px 0 0}
.ns-kpis>div{background:var(--surface);border:1px solid var(--line);border-radius:14px;padding:16px 18px;box-shadow:var(--shadow-sm)}
.ns-kpis .k{font-size:13px;color:var(--muted)}
.ns-kpis .v{font-size:34px;font-weight:500;letter-spacing:-.03em;line-height:1.15;font-variant-numeric:tabular-nums}
.ns-kpis .v.ok{color:var(--ok)}.ns-kpis .v.bad{color:var(--bad)}.ns-kpis .v.fix{color:var(--accent)}
.ns-kpis .note{font-size:12px;color:var(--muted)}
.ribbon{background:var(--surface);border:1px solid var(--line);border-radius:16px;padding:18px 20px 14px;margin-top:16px;
box-shadow:var(--shadow-sm)}
.ribbon .cells{display:flex;gap:3px;align-items:stretch;height:46px}
.ribbon .c{flex:1;min-width:3px;border-radius:3px;background:var(--ok);opacity:.85;animation:rise .5s both;
animation-delay:calc(var(--i)*12ms)}
.ribbon .c.lost{background:var(--bad);opacity:1}
.ribbon .c:hover{opacity:1;transform:scaleY(1.08)}
.ribbon .fx{flex:none;width:4px;margin:-6px 2px;border-radius:2px;background:var(--accent);position:relative;
box-shadow:0 0 0 3px color-mix(in srgb,var(--accent) 20%,transparent)}
.ribbon .fx::after{content:"fix live";position:absolute;bottom:-20px;left:50%;transform:translateX(-50%);font-size:11px;
color:var(--accent);white-space:nowrap;font-weight:500}
.ribbon .axis{display:flex;justify-content:space-between;margin-top:24px;font-size:12px;color:var(--muted);gap:12px;
flex-wrap:wrap}
.ribbon .legend{display:flex;gap:16px}.ribbon .legend i{display:inline-block;width:10px;height:10px;border-radius:2px;
margin-right:6px;vertical-align:-1px}
@keyframes rise{from{transform:scaleY(.2);opacity:0}}
.repair{background:var(--surface);border:1px solid var(--line);border-radius:18px;box-shadow:var(--shadow);margin-top:16px;
overflow:hidden}
.repair .rtop{display:flex;align-items:center;gap:14px;padding:18px 22px;border-bottom:1px solid var(--line);flex-wrap:wrap}
.repair .rtop h3{font-size:22px;font-weight:500;letter-spacing:-.02em;margin:0}
.repair .rtop .grow{flex:1}
.why{display:inline-block;font-size:13px;padding:2px 9px;border-radius:999px;background:color-mix(in srgb,var(--bad) 10%,#fff);
color:var(--bad);margin-right:6px}
.ttr{font-size:15px;padding:6px 14px;border-radius:999px;background:var(--navy);color:#fff;font-variant-numeric:tabular-nums}
.ttr b{font-size:20px;margin-left:4px}
.chain{display:grid;grid-template-columns:repeat(5,minmax(0,1fr));position:relative;padding:22px 22px 20px}
.chain::before{content:"";position:absolute;left:calc(10% + 4px);right:calc(10% + 4px);top:39px;height:2px;
background:linear-gradient(90deg,var(--bad),var(--accent) 30%,var(--accent) 70%,var(--ok))}
.step{position:relative;text-align:center;padding:0 10px;min-width:0}
.step .dot{width:36px;height:36px;margin:0 auto 12px;border-radius:50%;display:grid;place-items:center;color:#fff;
background:var(--accent);box-shadow:0 0 0 6px var(--surface);position:relative;font-size:15px;font-weight:600}
.step.lost .dot{background:var(--bad)}.step.back .dot{background:var(--ok)}.step.wait .dot{background:var(--line-2)}
.step .k{font-size:12px;color:var(--muted);text-transform:uppercase;letter-spacing:.06em}
.step .t{font-size:26px;font-weight:500;letter-spacing:-.02em;font-variant-numeric:tabular-nums;line-height:1.2}
.step .d{font-size:13px;color:var(--ink-2);margin-top:4px;overflow-wrap:anywhere;text-wrap:pretty}
.step .d a{font-weight:500}
.step .d.clamp{display:-webkit-box;-webkit-line-clamp:3;-webkit-box-orient:vertical;overflow:hidden}
.repair .foot{padding:12px 22px;border-top:1px solid var(--line);background:var(--sunk);font-size:13px;color:var(--ink-2);
display:flex;gap:8px 24px;flex-wrap:wrap}
.repair .foot b{color:var(--ink)}
.others td .why{margin:0}
@media (max-width:900px){.ns-kpis{grid-template-columns:repeat(2,minmax(0,1fr))}.chain{grid-template-columns:1fr;
row-gap:18px;text-align:left}.chain::before{display:none}.step{text-align:left;display:grid;grid-template-columns:44px 1fr;
column-gap:12px}.step .dot{grid-row:span 3;margin:0}.ns-hero h1{font-size:34px}}
@media (prefers-reduced-motion:reduce){.ribbon .c{animation:none}}
</style>"""

CHECK = "✓"


def _step(css: str, mark: str, label: str, at: str, detail: str, full: str = "") -> str:
    d = f"<div class='d clamp' title='{esc(full)}'>" if full else "<div class=d>"
    return (
        f"<div class='step {css}'><div class=dot>{mark}</div><div class=k>{label}</div><div class=t>{at}</div>"
        f"{d}{detail}</div></div>"
    )


def _card(item: dict, green: dict) -> str:
    run, first, fix = item["run"], item["first"], item["fix"]
    rounds_ = ", ".join(_round_link(r) for r in run)
    sig = item["signals"]
    raised = ", ".join(f"{sig.count(k)} {k}{'s' if sig.count(k) > 1 else ''}" for k in dict.fromkeys(sig))
    tested, urgent = green.get(fix["deploy"], (None, False))
    title = esc(fix["title"])
    change = f"<a href='{PULL}{fix['pr']}'>#{fix['pr']}</a> {title}" if fix["pr"] else title
    back = item["back"]
    steps = (
        _step("lost", "!", "Lost", _hm(first["at"]), f"{len(run)} round{'s' if len(run) > 1 else ''}: {rounds_}")
        + _step("", "1", "Fix merged", _hm(fix["at"]), change, fix["title"])
        + _step("", "2", "Tested on the host", _hm(tested), "tests and lint green" if tested else "no code: no test")
        + _step(
            "", "3", "Live", _hm(fix["live"]), "URGENT: right after our answer" if urgent else "in the safe window" if tested else "no restart"
        )
        + (
            _step("back", CHECK, "Accepted again", _hm(back["at"]),
                  f"round {_round_link(back)}" + (f" · {Decimal(str(back['points'])):.0f} points" if back["points"] else ""))
            if back
            else _step("wait", "…", "Accepted again", "—", "waiting for the next round")
        )
    )  # fmt: skip
    shipped = ", ".join(f"<a href='{PULL}{c['pr']}'>#{c['pr']}</a>" for c in item["with"][:6])
    more = len(item["with"]) - 6
    tried = ", ".join(
        (f"<a href='{PULL}{c['pr']}'>#{c['pr']}</a>" if c["pr"] else esc(c["title"])) + f" live {_hm(c['live'])}"
        for c in item["tried"]
    )
    foot = (
        (f"<span>Went live before the last loss: <b>{tried}</b></span>" if tried else "")
        + (f"<span>Raised on its own: <b>{raised}</b></span>" if raised else "")
        + (
            f"<span>Live in the same deploy: {shipped}{f' and {more} more' if more > 0 else ''}</span>"
            if shipped
            else ""
        )
    )
    return (
        f"<article class=repair><div class=rtop><h3>{_rounds_label(run)}</h3><div class=grow>{_reasons(run)}</div>"
        f"<span class=ttr>repaired in<b>{_span(item['repair'])}</b></span></div><div class=chain>{steps}</div>"
        + (f"<div class=foot>{foot}</div>" if foot else "")
        + "</article>"
    )


def _ribbon(found: list[dict], fixes: list[dict]) -> str:
    if not found:
        return "<div class='ribbon muted' id=ns-ribbon data-live>No round scored yet.</div>"
    marks = sorted({i["fix"]["live"] for i in fixes})
    cells, k = [], 0
    for i, r in enumerate(found):
        while k < len(marks) and marks[k] < r["at"]:
            if i:
                cells.append(f"<span class=fx title='fix live {_hm(marks[k])}'></span>")
            k += 1
        what = f"lost: {r['reason']}" if r["lost"] else "accepted"
        tag = f"a href='/sessions/{r['session']}'" if r["session"] else "span"
        cells.append(
            f"<{tag} class='c{' lost' if r['lost'] else ''}' style='--i:{i}' title='Round {r['round']} · "
            f"{_hm(r['at'])} · {esc(what)}'></{tag.split()[0]}>"
        )
    return (
        f"<div class=ribbon id=ns-ribbon data-live><div class=cells>{''.join(cells)}</div><div class=axis>"
        f"<span>Round {found[0]['round']} · {_hm(found[0]['at'])}</span><span class=legend>"
        "<span><i style='background:var(--ok)'></i>accepted</span><span><i style='background:var(--bad)'></i>lost</span>"
        "<span><i style='background:var(--accent)'></i>fix went live</span></span>"
        f"<span>Round {found[-1]['round']} · {_hm(found[-1]['at'])}</span></div></div>"
    )


def _others(items: list[dict]) -> str:
    rows = "".join(
        f"<tr><td>{_rounds_label(i['run'])}</td><td>{_reasons(i['run'])}</td><td class=n>{_hm(i['first']['at'])}</td>"
        f"<td>{', '.join(_round_link(r) for r in i['run'])}</td></tr>"
        for i in items
    )
    return (
        "<div class=panel><table><thead><tr><th>Rounds</th><th>Why</th><th class=n>At</th><th>Requests</th></tr>"
        f"</thead><tbody>{rows}</tbody></table></div>"
    )


def render(ctx: Context, commits: list[dict], green: dict) -> str:
    found = rounds(ctx)
    items = incidents(found, commits, signals(ctx))
    fixed = [i for i in items if i["fix"]]
    lost = sum(r["lost"] for r in found)
    ttr = median(i["repair"] for i in fixed) if fixed else None
    kpis = (
        "<div class=ns-kpis id=ns-kpis data-live>"
        f"<div><div class=k>Rounds</div><div class=v>{len(found)}</div><div class=note>scored by 5U</div></div>"
        f"<div><div class=k>Accepted</div><div class='v ok'>{len(found) - lost}</div><div class=note>with points</div></div>"
        f"<div><div class=k>Lost</div><div class='v bad'>{lost}</div><div class=note>in {len(items)} runs</div></div>"
        f"<div><div class=k>Repaired</div><div class='v fix'>{len(fixed)}</div><div class=note>by a change "
        "that names them</div></div>"
        f"<div><div class=k>Time to repair</div><div class=v>{_span(ttr) if ttr else '—'}</div>"
        "<div class=note>median, first loss to fix live</div></div></div>"
    )
    others = [i for i in items if not i["fix"]]
    body = (
        CSS + "<section class=ns-hero><h1>Night shift: <em>PIXEL repairs itself.</em></h1><p>A lost round is explained "
        "from the scorer's verdict and reported. The fix is merged, tested on our server and put live between two "
        "requests, without anyone touching the server. Every time below comes from the event log, the deploy log and "
        "git.</p></section>"
        + kpis
        + "<h2>Every round</h2>"
        + _ribbon(found, fixed)
        + f"<h2>Repairs <span class=count>{len(fixed)}</span></h2>"
        + f"<div id=ns-repairs data-live>{''.join(_card(i, green) for i in fixed) or '<p class=muted>No repair yet.</p>'}"
        + "</div>"
        + (
            f"<h2>Lost without a named fix <span class=count>{len(others)}</span></h2><div id=ns-others data-live>"
            + _others(others)
            + "</div>"
            if others
            else ""
        )
    )
    return body


@router.get("/night", response_class=HTMLResponse)
async def night(request: Request):
    ctx: Context = request.app.state.ctx
    commits, green = await asyncio.to_thread(_deploys)
    last = ctx.db.execute("SELECT MAX(id) FROM events").fetchone()[0] or 0
    script = f"const r=refresh();live('/events?after_id={last}',e=>{{if(e.kind==='feedback')r();}});"
    return layout("Night shift", render(ctx, commits, green), nav="night", script=script)
