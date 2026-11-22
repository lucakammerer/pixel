# Manually handle a freight request

Use this when PIXEL is stuck and a customer's request still has time left. A request expires six minutes after the
mail was received. A sent quote has a timestamp in the session. Check it before taking over and again before sending.
Only the first quote for the request counts.

## From incoming email to sent quote

1. Open **Requests → Open** and select the customer thread. **Activity** lists the customer's mail, the quote and
   booking, newest last, with only the six latest entries open and the rest behind **Show earlier entries**; each run of automatic steps (carrier mail, priced options, model calls) is one line you can expand.
   Expand the incoming email and confirm it asks for an air-freight quote. Tracking questions, tariff updates, and carrier bids belong to their own handlers.
2. While PIXEL works, **PIXEL is on it** shows its four steps and how far it got. Select **Take over** there or at
   the top. The session shows **You’re handling this request**: PIXEL holds its quote for you and sends nothing to
   the customer, carrier replies keep coming in, and if no quote is out by the deadline PIXEL still sends its best
   price.
3. **Get the quote out** walks you through four steps: request read, prices, quote draft, send. A step that is
   done shows a check mark, and the next one has the highlighted button. **Prices** also says how many carrier replies
   are still out. Open **Read incoming email** and download any listed attachments. Use the latest
   customer's message and its governing documents. Compare the lane, gross kg, volume in cbm, packages, cheapest
   or fastest preference, required or excluded airlines, currency, and the language with the **Request** panel. For
   door-to-port, confirm the pickup postcode. Note the goods-ready date, arrival deadline and maximum transit if
   the customer gives them. For sensitive cargo, carry over any required
   documentation and service conditions. If automatic extraction stopped before rates were sourced, the session lets
   you enter the shipment details. A blank origin for a door shipment uses the postcode zone's assigned airport.
4. Select **Find sheet and portal rates**. This checks valid local tariffs and calls the 5U rate API for airlines on
   the lane. Review **Options** and **Activity**. The lookup does not send mail.
5. Select **Request carrier bids** if you need mail-only air rates or a pickup quote. PIXEL uses the current carrier
   list and pickup zone, skips carriers already sourced or contacted for the lane, and sends through the outbox.
   Incoming quotes appear in **Activity**, inside the folded line of automatic steps, and usable prices under
   **Options**. If the rate API returns 503,
   **Ask the rate desk** requests its portal prices and rate IDs. For a custom carrier message, use **Write to a
   carrier**. Use the bid button when you need the response to feed into PIXEL’s pricing.
6. Select **Prepare quote draft**. PIXEL selects and prices a complete offer from the saved options, including pickup
   for door-to-port. A pickup bid alone is a ground price. An air rate alone covers airport-to-airport. If pickup
   is still missing, keep watching for bids or release the session so recovery can continue.
7. Review the draft's recipient, total, currency, carrier, transit, and pickup scope. Edit the subject and message
   in the customer's language. Preserve the priced values in the draft, including the exact numeric format. Select
   **Send quote** in step 4 to send the draft as it is, or edit it under **Review and send the quote**. PIXEL sends a threaded reply with its offer PDF and records it in the CRM. If another quote
   went out while you were editing, the send is refused. Verify **Quote sent** in **Activity**.
8. Select **Release to autopilot** so the session can continue through acceptance, booking, and tracking. While in
   takeover, use **Write or review a follow-up email** for a separate question after the quote.

If the saved shipment facts are wrong and pricing has already started, the page shows the existing facts and
options. The manual-entry form is for a stopped extraction before sourcing. Check the latest customer mail and
use the read-only rate steps below to verify the discrepancy before deciding how to proceed.

## What PIXEL does behind the page

| Stage | What happens | What you see |
| --- | --- | --- |
| Receive and classify | The webhook or poller records the mail. The router decides whether this is a quote request, customer reply, carrier bid, tariff update, or tracking question. | **Mail received**, **Sorted as customer RFQ** |
| Read the shipment | The RFQ agent reads the current mail and its attachments. Rules handle supported layouts and the gateway handles ambiguous ones. | **Request read** with missing facts in **Activity**; the evidence in the automatic steps after it |
| Source and select | The engine prices valid sheets, calls the portal, and sends relevant air and pickup enquiries. It compares in EUR, respects excluded airlines, and selects for the customer's stated preference. | **Options**, **Bid requested**, **Bid received**, **Option failed** |
| Send the first quote | The CRM stores the draft. The outbox claims the reply, sends it to the customer thread and records delivery. At the deadline, recovery can send an airport-only offer if pickup is still unpriced. If air freight is also unpriced, PIXEL replies that a rate is unavailable. | **Quote drafted**, **Quote sent**, or **Fallback quote sent** |
| Book and track | The booking agent waits for an explicit instruction to book the delivered offer. It uses the selected rate ID or booking by mail when required. Tracking reads the shipment record from the CRM. | **Booking requested**, **Booked**, **Booking confirmed**, tracking replies |

## Rate portal and API reference

The session page uses the 5U API. If you inspect it directly, use your authorized base URL and the team key in the
`X-API-Key` header through your existing secret setup. Keep reads targeted to the lane. Rate reads share the live
team's quota.

| Need | Use | Inputs and result |
| --- | --- | --- |
| Which airlines price this lane? | `GET /v1/portal/ports` | Find the exact origin and destination in `lanes`. Use the airline code returned there. |
| Does this airline have a better weight break? | `GET /v1/portal/brackets?airline=<code>` | Returns `breaks_kg`. A 404 can tell you where to email the airline instead. |
| What is the air price? | `GET /v1/portal/rates` | Pass `origin`, `destination`, `airline`, `weight_kg` (gross kg), and `volume_cbm`. Read `total`, `currency`, `transit_days`, `rate_id`, `valid_until`, and `dataset_version`. The total includes freight, fuel, and security. |
| Where is the customer's attachment? | `GET /v1/mailbox/messages/<message_id>/attachments/<attachment_id>` | Use the IDs from the incoming mail metadata or the download link in the session. |

PIXEL's code computes chargeable weight as the greater of gross kg and volume × 167. It can test a higher weight
break, using the higher break as `weight_kg` and zero volume for that read. It keeps the cheaper offer when both
rates are from the same dataset version. A 429 means the rate budget is exhausted. Check `Retry-After` and use the
rates already available if waiting would risk the deadline. A 503 means the rate API is unavailable. Use the rate
desk or bids. A rate ID is normally valid for 15 minutes. Booking follows acceptance and needs a fresh rate ID.

Standalone API lookups are for checking facts. Use the website's sourcing controls to put rates on the session and
its quote composer to send the first reply through the outbox.

For example, in PowerShell with `PIXEL_API_BASE` and `PIXEL_API_KEY` already provisioned, read one rate after
choosing the airline code from `/v1/portal/ports`. Replace the placeholders with values from the request.

```powershell
$rateUrl = "$env:PIXEL_API_BASE/v1/portal/rates"
$rateInputs = @{
    origin = "<origin IATA code>"
    destination = "<destination IATA code>"
    airline = "<portal airline code>"
    weight_kg = "<gross kg>"
    volume_cbm = "<volume in cbm>"
}
Invoke-RestMethod -Method Get -Uri $rateUrl -Body $rateInputs -Headers @{"X-API-Key" = $env:PIXEL_API_KEY} |
    Select-Object airline, total, currency, transit_days, rate_id, valid_until, dataset_version
```

## Email templates

Fill every placeholder from the request or verified draft. Include the pickup sentence only when it describes the
priced service. If the request asks about a departure, give it only when confirmed. Match the customer's language.

**Customer quote**

```text
Subject: Re: [customer’s subject]

Hello [customer name],

We can fly your shipment from [origin] to [destination] with [carrier] for [total] [currency] all-in,
with [transit] in transit. [For door service: This includes pickup from [postcode] to [origin].]

[Answer a specific question from the request using confirmed information.]
Let me know if you’d like me to book this.

[Your name]
PIXEL Air Freight
```

For an airport-only offer where the customer asked for pickup, state **This price covers airport-to-airport
transport. Pickup is excluded.** Use the exact transit phrase already in the draft when editing a built-in language.

**Airline enquiry**

```text
Subject: Rate request [origin]-[destination], [gross kg] kg

Hello,

Please quote air freight from [origin] to [destination] for [pieces] pieces, [gross kg] kg gross,
[volume] cbm, ready [date]. Please send the all-in freight, fuel and security total with currency,
transit time in days, and the rate ID or validity if available.

Thank you,
PIXEL Air Freight
```

**Rate-desk enquiry**

```text
Subject: Rate request [origin]-[destination], [gross kg] kg

Hello,

Please send the portal rates for [origin]-[destination], [gross kg] kg, [volume] cbm.

Thank you,
PIXEL Air Freight
```

**Pickup enquiry**

```text
Subject: Pickup request [postcode] to [origin]

Hello,

Please quote pickup from [postcode] to [origin] airport. The shipment is [pieces] pieces,
[gross kg] kg gross and [volume] cbm, ready [date]. Please send the all-in pickup price with
currency and the pickup lead time in hours.

Thank you,
PIXEL Air Freight
```

To practise, run `uv run python -m tools.preview` and open the synthetic session marked **person**. Try editing its
draft, sending in dry-run mode and releasing to autopilot. The preview has synthetic rates and no live mailbox or
portal connection.
