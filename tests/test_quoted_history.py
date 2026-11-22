import json
from pathlib import Path

import pytest

from pixel.agents.rfq import booking, extract
from pixel.api.mailbox import to_message
from tools.benchmark_generalization import _deadline

MAILS = Path(__file__).parent / "fixtures" / "mails"
# The quote headers mail clients write without an address: our own quote or an old request below them is history.
HEADERS = [
    "________________________________\nFrom: Tobias Auer\nSent: Sunday, 25 October 2026 09:12\nSubject: {subject}",
    "________________________________\nVon: Tobias Auer\nGesendet: Sonntag, 25. Oktober 2026 09:12\nBetreff: {subject}",
    "From: Tobias Auer\nSent: Sunday, 25 October 2026 09:12\nSubject: {subject}",
    "---\nSubject: {subject}",
]


@pytest.mark.parametrize("header", HEADERS)
def test_an_old_request_below_an_address_less_header_is_not_read(header):
    raw = json.loads((MAILS / "rfq-door-quoted-old-request.json").read_text())
    marker = "-----Original Message-----"
    head, _, old = raw["body"].partition(marker)
    subject = old.split("Subject: ", 1)[1].split("\n", 1)[0]
    body = head + header.format(subject=subject) + old.split(subject, 1)[1]
    original = extract.by_rules(to_message(raw, "in"), _deadline())
    changed = extract.by_rules(to_message(raw | {"body": body}, "in"), _deadline())
    assert changed is not None and (changed.destination, changed.weight_kg) == (
        original.destination,
        original.weight_kg,
    )


@pytest.mark.parametrize("header", HEADERS[:3])
def test_our_quote_below_an_address_less_header_does_not_accept(header):
    thanks = "Hi Lena,\n\nThanks for booking the shipment. Please let me know once the airline confirms.\n\nSara\n\n"
    ours = "Hi Sara,\n\nthe best option is 1483.43 EUR all-in. Just reply to this mail and we will book it for you."
    body = thanks + header.format(subject="Re: Air freight quote") + "\n\n" + ours
    own = extract.HISTORY.split(body, maxsplit=1)[0]
    assert booking.intent(own) == "other"


# A request laid out as header-like fields is the customer's own: no dated "Sent:" next to its "From:".
FIELD_REQUESTS = [
    (
        "Hello,\nplease quote the following shipment:\n------------------------------\nFrom: Frankfurt (FRA)\n"
        "To: Chicago (ORD)\nWeight: 340 kg\nVolume: 1.2 cbm\n\nThanks, Anna"
    ),
    "Hallo,\nbitte um ein Angebot:\n\nVon: FRA\nDatum: ab 25.11. versandbereit\nNach: ORD\nGewicht: 340 kg\n\nGruß Anna",
]


@pytest.mark.parametrize("body", FIELD_REQUESTS)
def test_a_request_laid_out_as_fields_stays_whole(body):
    assert extract.own_words(body) == body
