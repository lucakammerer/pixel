"""The ElevenLabs agent that makes our calls, applied with `uv run python -m tools.voice_agent`. It handles:
- `order_status`: calls a carrier/handler about a specific booked order to get its live status, location and ETA,
  calling `record_order_status` so the laptop UI updates in real time.
- `carrier`: asks a late carrier for their all-in price and transit/pickup time (`record_carrier_offer`).
- `customer`: asks a customer for missing shipment facts (`record_customer_details`).

It never names, compares or computes a price and never quotes."""

PROMPT = """You are the freight assistant of PIXEL Air Freight, a digital air freight forwarder, on a short phone call. \
This is a hackathon demo with fictional companies.
You are calling {{name}}, a {{party}}, about {{reference}} for an air export shipment {{lane}} ({{shipment}}). \
We still need: {{wanted}}. Your first message already asked for it.

Be quick, natural and friendly: one short sentence per turn, finish the call in under a minute.

If party is order_status:
- You are checking the live status of this specific order ({{reference}}, {{lane}}).
- Ask targeted questions to learn:
  1. The current shipment status (booked, picked up, at origin warehouse, in transit, in customs, arrived, out for delivery, delivered, or delayed).
  2. The current location (city, airport or cargo terminal).
  3. The expected arrival or delivery time (ETA), flight number if known, and any delay reason or remarks.
- As soon as they give you any status update, call record_order_status right away with everything they told you so far.
- If they only gave the status and have not mentioned the current location or ETA yet, ask one brief follow-up for location and ETA, then call record_order_status again with the complete details.
- Once you have recorded the update, confirm the new status and ETA back in one short sentence, thank them and say goodbye.

If party is carrier:
- Ask for their all-in price for exactly this shipment: one total with its currency, including freight, fuel and \
security. For an airline also the transit time in days; for a pickup also how many hours until they can collect.
- If they give separate charges or a price per kg, ask for the one total they would charge for this shipment. Never add, \
multiply or convert anything yourself.
- As soon as you have the total, the currency and (airline) the transit days, call record_carrier_offer without saying \
anything first. Then repeat it back in one sentence, thank them and say goodbye.
- If they cannot offer this lane or shipment, thank them and say goodbye. Do not call the tool.

If party is customer:
- Ask only for what we still need. Airports as their three-letter codes (if they name a city, ask for or confirm the \
airport), weight in kilograms, volume in cubic metres (if they give dimensions, ask for the total volume), the pickup \
postcode.
- As soon as you have it, call record_customer_details without saying anything first, with only what they told you. \
Then repeat it back in one sentence, say their quote follows by email shortly and say goodbye.
- Never tell the customer a price, an estimate or a carrier. If asked, say the quote arrives by email in a few minutes.

For everyone:
- If they correct you, use the corrected answer.
- If they do not know, say that is fine, they can reply to our email, and say goodbye.
- Never ask for or accept bank details, passwords or card numbers.
- If this is the wrong person or they do not want to talk, apologise and end the call.
- Every time you have said goodbye, call end_call straight away."""

FIRST_MESSAGE = "Hi {{name}}, this is the PIXEL Air Freight assistant calling about {{reference}}, {{lane}}. \
Could you give me {{wanted}}?"

# Runs in the browser during the call (`call.js` / `phone.js`), posting straight to /api/calls/{id}/answer.
ORDER_STATUS_TOOL = {
    "type": "client",
    "name": "record_order_status",
    "description": "Updates the order's live status on the PIXEL dashboard as soon as the carrier or handler states "
    "where the shipment is. Can be called again if they add location, ETA or notes in a follow-up turn.",
    "expects_response": True,
    "response_timeout_secs": 8,
    "parameters": {
        "type": "object",
        "properties": {
            "status": {
                "type": "string",
                "description": "One of: booked, picked_up, at_origin, in_transit, customs, arrived, "
                "out_for_delivery, delivered, delayed, exception",
            },
            "location": {
                "type": "string",
                "description": "Current city, airport or facility, e.g. Frankfurt Airport (FRA) or JFK Cargo Terminal",
            },
            "eta": {
                "type": "string",
                "description": "Expected arrival or delivery time as stated, e.g. Tomorrow 14:30 EST",
            },
            "flight_number": {
                "type": "string",
                "description": "Flight number or AWB if mentioned, e.g. CS-402. Empty if not mentioned.",
            },
            "notes": {
                "type": "string",
                "description": "Short summary of any delay reason, customs detail or extra remarks. Empty if none.",
            },
        },
        "required": ["status"],
    },
}
CARRIER_TOOL = {
    "type": "client",
    "name": "record_carrier_offer",
    "description": "Saves the carrier's all-in offer for this shipment, before you say goodbye. Only figures the "
    "carrier said, never one you worked out.",
    "expects_response": True,
    "response_timeout_secs": 8,
    "parameters": {
        "type": "object",
        "properties": {
            "total": {"type": "string", "description": "The all-in total the carrier said, digits only, e.g. 1195.90"},
            "currency": {"type": "string", "description": "Three-letter currency code, e.g. EUR or USD"},
            "transit_days": {"type": "string", "description": "Transit time in days (airline). Empty for a pickup."},
            "pickup_hours": {"type": "string", "description": "Hours until pickup (trucker). Empty for an airline."},
        },
        "required": ["total", "currency"],
    },
}
CUSTOMER_TOOL = {
    "type": "client",
    "name": "record_customer_details",
    "description": "Saves the shipment details the customer gave, before you say goodbye. Leave out what they did not "
    "say.",
    "expects_response": True,
    "response_timeout_secs": 8,
    "parameters": {
        "type": "object",
        "properties": {
            "origin": {"type": "string", "description": "Departure airport, three-letter IATA code"},
            "destination": {"type": "string", "description": "Destination airport, three-letter IATA code"},
            "weight_kg": {"type": "string", "description": "Gross weight in kg, digits only"},
            "volume_cbm": {"type": "string", "description": "Total volume in cubic metres, digits only"},
            "pickup_postcode": {"type": "string", "description": "Postcode to collect the goods from"},
        },
        "required": [],
    },
}
TOOLS = (ORDER_STATUS_TOOL, CARRIER_TOOL, CUSTOMER_TOOL)

AGENT = {
    "name": "PIXEL carrier and customer calls",
    "tags": ["pixel", "start-hack"],
    "conversation_config": {
        "agent": {
            "first_message": FIRST_MESSAGE,
            "language": "en",
            "dynamic_variables": {
                "dynamic_variable_placeholders": {
                    "name": "Cobalt Skylift",
                    "party": "order_status",
                    "reference": "booking BK-88412",
                    "lane": "FRA to JFK",
                    "shipment": "420 kg for Nordlicht Audio GmbH, currently booked",
                    "wanted": "a quick status update on where the shipment is right now and its expected arrival time",
                    "signer": "Lena Berger",
                }
            },
            "prompt": {
                "prompt": PROMPT,
                "temperature": 0.2,
                # tool_ids is filled in by tools/voice_agent.py with the ids of TOOLS.
                "built_in_tools": {
                    "end_call": {
                        "type": "system",
                        "name": "end_call",
                        "description": "",
                        "params": {"system_tool_type": "end_call"},
                    },
                },
            },
        },
        # Browser calls only: full 16 kHz audio. Flash is ElevenLabs' lowest-latency English model.
        "tts": {
            "model_id": "eleven_flash_v2",
            "voice_id": "cjVigY5qzO86Huf0OWal",  # Eric: calm, American English
            "stability": 0.75,
            "similarity_boost": 0.8,
            "optimize_streaming_latency": 4,
            "agent_output_audio_format": "pcm_16000",
        },
        "turn": {"turn_eagerness": "eager", "silence_end_call_timeout": 15},
        "asr": {"user_input_audio_format": "pcm_16000"},
        "conversation": {"max_duration_seconds": 110},  # below `calls.CALL_MAX_S`, the longest a wait is held
    },
    "platform_settings": {
        "auth": {"enable_auth": True},  # sessions only through signed URLs from our server
        "data_collection": {
            "reached": {"type": "boolean", "description": "True if the right person engaged with the call."},
            "callback_requested": {"type": "boolean", "description": "True if they asked a person to call back."},
            "open_questions": {
                "type": "string",
                "description": "Questions the assistant could not answer. Empty if none.",
            },
        },
    },
}
