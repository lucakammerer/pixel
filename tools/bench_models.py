"""Compare gateway models on request mails with the production extraction prompt: accuracy, tokens, time, cost.

    uv run python -m tools.bench_models                      # every model in pixel.llm.PRICES
    uv run python -m tools.bench_models gpt-4.1-mini gpt-4o-mini

It spends through the gateway, and that spend counts in the hour it runs in (R28): about 0.02 USD for all models.
The mails are written for this tool; the expected values are what the request asks for.
"""

import asyncio
import json
import statistics
import sys
import time

from pixel.agents.rfq.extract import SCHEMA, SYSTEM
from pixel.api.client import make_client
from pixel.config import settings
from pixel.llm import PRICES

CAP = 200  # the production `max_output_tokens` of rfq.extract
IN_FLIGHT = 8  # the gateway allows 20 per team; the live service needs the rest
FIELDS = "origin destination weight volume preference door postcode"

MAILS = {
    "de-dims-door": (
        "Subject: Anfrage Luftfracht\n\nGuten Tag,\nwir brauchen ein Angebot für Luftfracht ab München nach Tokio Narita. 3 Kartons, je 60x40x50 cm, zusammen 95 kg. Bitte die schnellste Option. Abholung bei uns in 86150 Augsburg.\nViele Grüße, Jonas Weber",
        {
            "origin": "MUC",
            "destination": "NRT",
            "weight": 95,
            "volume": 0.36,
            "preference": "fastest",
            "door": True,
            "postcode": "86150",
        },
    ),
    "en-lbs": (
        "Subject: rate needed\n\nHi team, need a rate for 1 pallet, 480 lbs, 120x80x100 cm from Hamburg to Chicago O'Hare, most economical please. Thanks, Priya",
        {
            "origin": "HAM",
            "destination": "ORD",
            "weight": 217.7,
            "volume": 0.96,
            "preference": "cheapest",
            "door": False,
            "postcode": None,
        },
    ),
    "terse": (
        "Subject: fra-sin\n\nfra-sin 1.2t 6cbm asap pls, airport to airport",
        {
            "origin": "FRA",
            "destination": "SIN",
            "weight": 1200,
            "volume": 6,
            "preference": "fastest",
            "door": False,
            "postcode": None,
        },
    ),
    "gross-net-door": (
        "Subject: Quote request\n\nHello, gross weight 312 kg (net 290 kg), 2.4 m3, from Stuttgart to Mexico City, cheapest routing. Please collect at our warehouse, Koenigstrasse 12, 70173 Stuttgart. Best, Marta",
        {
            "origin": "STR",
            "destination": "MEX",
            "weight": 312,
            "volume": 2.4,
            "preference": "cheapest",
            "door": True,
            "postcode": "70173",
        },
    ),
    "city-names": (
        "Subject: Air export enquiry\n\nDear Sir or Madam, kindly offer your best price for 5 crates, total 640 kilos and 3.1 cubic metres, Dusseldorf to Johannesburg. Price matters more than speed. Regards, T. Nkosi",
        {
            "origin": "DUS",
            "destination": "JNB",
            "weight": 640,
            "volume": 3.1,
            "preference": "cheapest",
            "door": False,
            "postcode": None,
        },
    ),
    "unclear": (
        "Subject: Hello\n\nCould you help us with a shipment to New York next week?",
        {
            "origin": None,
            "destination": "JFK",
            "weight": None,
            "volume": None,
            "preference": "cheapest",
            "door": False,
            "postcode": None,
        },
    ),
}


def _number(value) -> float | None:
    try:
        return float(str(value).replace(",", "."))
    except ValueError:
        return None


def score(data: dict, expected: dict) -> list[bool]:
    weight, volume = _number(data.get("weight_kg")), _number(data.get("volume_cbm"))
    return [
        str(data.get("origin") or "").upper() == (expected["origin"] or ""),
        str(data.get("destination") or "").upper() == (expected["destination"] or ""),
        weight is None if expected["weight"] is None else weight is not None and abs(weight - expected["weight"]) <= 1,
        expected["volume"] is None or (volume is not None and abs(volume - expected["volume"]) <= 0.02),
        data.get("preference") == expected["preference"],
        bool(data.get("door_pickup")) == expected["door"],
        (data.get("pickup_postcode") or None) == expected["postcode"],
    ]


async def ask(client, limit: asyncio.Semaphore, model: str, name: str) -> dict:
    text, expected = MAILS[name]
    payload = {
        "model": model,
        "messages": [{"role": "system", "content": SYSTEM}, {"role": "user", "content": text}],
        "response_format": {
            "type": "json_schema",
            "json_schema": {"name": "rfq_extract", "schema": SCHEMA, "strict": True},
        },
        "max_completion_tokens": CAP,
    }
    async with limit:
        started = time.monotonic()
        response = await client.post("/v1/gateway/chat/completions", json=payload, timeout=60)
        seconds = time.monotonic() - started
    row = {"model": model, "mail": name, "seconds": seconds, "tokens_in": 0, "tokens_out": 0, "ok": None}
    if response.status_code != 200:
        return row | {"error": f"{response.status_code}"}
    body = response.json()
    usage = body.get("usage") or {}
    row |= {"tokens_in": usage.get("prompt_tokens", 0), "tokens_out": usage.get("completion_tokens", 0)}
    try:
        return row | {"ok": score(json.loads(body["choices"][0]["message"]["content"] or ""), expected)}
    except ValueError:
        return row | {"error": f"no answer (finish_reason {body['choices'][0].get('finish_reason')})"}


async def run(models: list[str]) -> None:
    limit = asyncio.Semaphore(IN_FLIGHT)
    async with make_client(settings()) as client:
        rows = await asyncio.gather(*(ask(client, limit, model, name) for model in models for name in MAILS))
    print(
        f"{'model':<14} {'mails':>6} {'fields':>7} {'failed':>6} {'in':>5} {'out':>5} {'s med':>6} {'USD/call':>9} {'points':>7}"
    )
    for model in models:
        mine = [r for r in rows if r["model"] == model]
        read = [r["ok"] for r in mine if r["ok"]]
        tokens_in = statistics.mean(r["tokens_in"] for r in mine)
        tokens_out = statistics.mean(r["tokens_out"] for r in mine)
        price_in, price_out = (float(p) for p in PRICES[model])
        usd = (tokens_in * price_in + tokens_out * price_out) / 1e6
        print(
            f"{model:<14} {sum(all(ok) for ok in read):>4}/{len(MAILS)} {sum(map(sum, read)):>4}/{len(MAILS) * 7}"
            f" {len(mine) - len(read):>6} {tokens_in:>5.0f} {tokens_out:>5.0f}"
            f" {statistics.median(r['seconds'] for r in mine):>6.1f} {usd:>9.5f} {usd / 0.10 * 40:>7.2f}"
        )
        for r in mine:
            if r["ok"] is None:
                print(f"     {r['mail']}: {r['error']}")
            elif not all(r["ok"]):
                print(f"     {r['mail']}: wrong {' '.join(f for f, ok in zip(FIELDS.split(), r['ok']) if not ok)}")


def main() -> None:
    asyncio.run(run(sys.argv[1:] or list(PRICES)))


if __name__ == "__main__":
    main()
