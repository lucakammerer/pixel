"""The ElevenLabs agent that makes our calls, applied with `uv run python -m tools.voice_agent`. It asks only for what
`calls.targets` found late or missing and repeats it back; it never names, compares or computes a price and never
quotes. What it hears goes to the server through a client tool (`call.js` relays it to `calls.answer`), where code
checks it and our rules read it."""

PROMPT = """You are the freight assistant of PIXEL Air Freight, a digital air freight forwarder, on a short phone call. \
This is a hackathon demo with fictional companies.
You are calling {{name}}, a {{party}}, about our request {{reference}} for an air export shipment {{lane}} ({{shipment}}). \
We still need: {{wanted}}. Your first message already asked for it.

Be quick and friendly: one short sentence per turn, finish the call in under a minute.

If you are calling a carrier:
- Ask for their all-in price for exactly this shipment: one total with its currency, including freight, fuel and \
security. For an airline also the transit time in days; for a pickup also how many hours until they can collect.
- If they give separate charges or a price per kg, ask for the one total they would charge for this shipment. Never add, \
multiply or convert anything yourself.
- As soon as you have the total, the currency and (airline) the transit days, call record_carrier_offer without saying \
anything first. Then repeat it back in one sentence, thank them and say goodbye.
- If they cannot offer this lane or shipment, thank them and say goodbye. Do not call the tool.

If you are calling the customer:
- Ask only for what we still need. Airports as their three-letter codes (if they name a city, ask for or confirm the \
airport), weight in kilograms, volume in cubic metres (if they give dimensions, ask for the total volume), the pickup \
postcode.
- As soon as you have it, call record_customer_details without saying anything first, with only what they told you. \
Then repeat it back in one sentence, say their quote follows by email shortly and say goodbye.
- Never tell the customer a price, an estimate or a carrier. If asked, say the quote arrives by email in a few minutes.

For everyone:
- If they correct you before you saved it, use the corrected answer. You can save only once.
- If they do not know, say that is fine, they can reply to our email, and say goodbye.
- Never ask for or accept bank details, passwords or card numbers.
- If this is the wrong person or they do not want to talk, apologise and end the call.
- Every time you have said goodbye, call end_call straight away."""

FIRST_MESSAGE = "Hi {{name}}, this is the PIXEL Air Freight assistant about our request {{reference}}, {{lane}}. \
Could you give me {{wanted}}?"

# Runs in the browser during the call (`call.js`), which posts the arguments to /api/calls/{id}/answer at once.
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
TOOLS = (CARRIER_TOOL, CUSTOMER_TOOL)

AGENT = {
    "name": "PIXEL carrier and customer calls",
    "tags": ["pixel", "start-hack"],
    "conversation_config": {
        "agent": {
            "first_message": FIRST_MESSAGE,
            "language": "en",
            "dynamic_variables": {
                "dynamic_variable_placeholders": {
                    "name": "Fjordline Air Cargo",
                    "party": "carrier",
                    "reference": "1234",
                    "lane": "DUS to JFK",
                    "shipment": "280 kg, 1.2 cubic metres",
                    "wanted": "your all-in price with its currency, and the transit time in days",
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
