"""Render the quote from the greeting directly into the offer (R31).
Code writes the offer. The model may supply the customer's signed first name for the greeting."""

import asyncio
import json
import random
import re
from dataclasses import asdict, dataclass
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal
from zoneinfo import ZoneInfo

from pixel import llm
from pixel.agents.rfq.extract import own_words, words_of
from pixel.contracts import Message, Preference, RateOption, Rfq
from pixel.core import events, pdf
from pixel.core.context import Context
from pixel.pricing import calc

CENT = Decimal("0.01")
DEFAULT_SIGNER = "Lena Berger"
COMPANY = "Air Export, PIXEL Air Freight"
OFFICE = ZoneInfo("Europe/Berlin")  # our desk's clock: "until 17:22 today" is the customer's time too

TEXT = {
    "en": {
        "subject": "Quote {origin} to {destination}, {weight} kg",
        "hello_name": {"formal": "Dear {name},", "neutral": "Hello {name},", "casual": "Hi {name},"},
        "hello": "Hello,",
        "core": {
            "cheapest": (
                (
                    "We can fly your shipment from {origin} to {destination} with {carrier} for {total} {currency} "
                    "all-in, with {transit} in transit."
                ),
                (
                    "For the best price I would go with {carrier} from {origin} to "
                    "{destination}: {total} {currency} all-in, {transit} in transit."
                ),
                (
                    "From {origin} to {destination} I can offer you {total} {currency} all-in with {carrier}; transit "
                    "is {transit}."
                ),
                (
                    "My best price from {origin} to {destination} is {total} {currency} all-in, flying with {carrier}, "
                    "{transit} in transit."
                ),
                (
                    "I've compared the airlines from {origin} to {destination}, and {carrier} comes out best at {total} "
                    "{currency} all-in, with {transit} in transit."
                ),
            ),
            "fastest": (
                (
                    "We can fly your shipment from {origin} to {destination} with {carrier} for {total} {currency} "
                    "all-in, with {transit} in transit."
                ),
                (
                    "As speed matters here, I would go with {carrier} from {origin} to {destination}: {transit} in "
                    "transit, {total} {currency} all-in."
                ),
                (
                    "For the fastest routing from {origin} to {destination} I can offer {carrier}, {transit} in "
                    "transit, at {total} {currency} all-in."
                ),
                (
                    "{carrier} gets your shipment from {origin} to {destination} in {transit}, for {total} {currency} "
                    "all-in."
                ),
                (
                    "I've compared the airlines from {origin} to {destination}, and {carrier} is the quickest: "
                    "{transit} in transit, {total} {currency} all-in."
                ),
            ),
        },
        # the goods in the customer's own words: a clerk names what they ship, a template says "your shipment"
        "core_goods": {
            "cheapest": (
                (
                    "For your {goods}, the best price from {origin} to {destination} is with {carrier}: {total} "
                    "{currency} all-in, {transit} in transit."
                ),
                (
                    "I've compared the airlines for your {goods} from {origin} to {destination}, and {carrier} comes "
                    "out cheapest at {total} {currency} all-in, with {transit} in transit."
                ),
            ),
            "fastest": (
                (
                    "For your {goods}, the quickest option from {origin} to {destination} is {carrier}: {transit} in "
                    "transit, {total} {currency} all-in."
                ),
                (
                    "I've compared the airlines for your {goods} from {origin} to {destination}, and {carrier} is the "
                    "quickest: {transit} in transit, {total} {currency} all-in."
                ),
            ),
        },
        "air_only": (
            "The airport-to-airport price from {origin} to {destination} with {carrier} is {total} {currency}, "
            "with {transit} in transit. Pickup is excluded."
        ),
        "basis": (
            "That covers air freight, fuel and security surcharges.",
            "The price includes the air freight and the fuel and security surcharges.",
        ),
        "basis_pickup": (
            "That covers pickup at your {site} by {trucker}, air freight, fuel and security surcharges.",
            (
                "The price includes collection from your {site} by {trucker}, the air freight and the fuel and security "
                "surcharges."
            ),
        ),
        "basis_zone_pickup": (
            "That includes air freight, fuel and security surcharges, and pickup by {trucker} at the rate for your area."
        ),
        "pickup_address": "Please send the full pickup address, including the postcode, so I can check the pickup rate.",
        "book_air_only": "If you'd like to book the air leg only, please reply to this email.",
        "bulky": (
            "As the cargo is bulky for its weight, the airline charges it on {chargeable} kg volume weight.",
            "Because of the volume, the freight is calculated on {chargeable} kg chargeable weight.",
        ),
        "valid": ("I can hold the rate until {until}.", "The rate is good until {until}."),
        "until_today": "{time} today",
        "until": "{time} on {day}",
        "book": (
            "If that works for you, just reply and I'll book it right away.",
            "Just reply to this mail if you'd like to go ahead, and I'll take care of the booking.",
            "Let me know if you would like to go ahead and I'll book it straight away.",
        ),
        "bye": {"formal": "Kind regards,", "neutral": "Best regards,", "casual": "Best wishes,"},
        "bye_town": "Best regards to {town},",
        "wish_back": "Have a {wish} too!",
        "in_city": "in {place}",
        "at_airport": "at {place}",
        "day": "day",
        "days": "days",
        "arrival": "With the cargo ready on {ready}, the earliest arrival {where} would be {arrive}.",
        "arrival_pickup": "With pickup on {ready}, the earliest arrival {where} would be {arrive}.",
        "pickup_first": "We can collect on {ready}, the day it is ready, and the earliest arrival {where} would be {arrive}.",
        "arrival_air_only": (
            "If you deliver the cargo to {origin} on {ready}, the earliest arrival {where} would be {arrive}."
        ),
        "departure": "That is with a departure from {origin} on {depart}.",
        "one_awb": "All lots travel together as one consignment under a single air waybill, as you asked.",
        "reference": "Your reference {ref} is on the offer.",
        "breakdown": "The breakdown: {parts}; all-in that is {per_kg} {currency} per kg chargeable weight.",
        "parts": ("air freight", "fuel surcharge", "security surcharge", "pickup"),
        "cutoff": "The exact drop-off cut-off at {origin} comes with the airline's booking, and I will pass it on "
        "as soon as I have it.",
        "months": (
            "January",
            "February",
            "March",
            "April",
            "May",
            "June",
            "July",
            "August",
            "September",
            "October",
            "November",
            "December",
        ),
        "weekdays": ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"),
        "date": "{weekday} {day} {month}",
    },
    "de": {
        "subject": "Angebot {origin} nach {destination}, {weight} kg",
        "hello_name": {"formal": "Guten Tag {name},", "neutral": "Hallo {name},", "casual": "Hallo {name},"},
        "hello": "Guten Tag,",
        "core": {
            "cheapest": (
                (
                    "Wir können Ihre Sendung von {origin} nach {destination} mit {carrier} für {total} {currency} "
                    "all-in fliegen, die Laufzeit beträgt {transit}."
                ),
                (
                    "Von {origin} nach {destination} kann ich Ihnen {total} {currency} all-in mit {carrier} anbieten, "
                    "die Laufzeit beträgt {transit}."
                ),
                (
                    "Mein bester Preis von {origin} nach {destination} ist {total} {currency} all-in mit {carrier}, "
                    "Laufzeit {transit}."
                ),
                (
                    "Ich habe die Airlines von {origin} nach {destination} verglichen: Am besten liegt {carrier} mit "
                    "{total} {currency} all-in, Laufzeit {transit}."
                ),
            ),
            "fastest": (
                (
                    "Wir können Ihre Sendung von {origin} nach {destination} mit {carrier} für {total} {currency} "
                    "all-in fliegen, die Laufzeit beträgt {transit}."
                ),
                (
                    "Da es schnell gehen soll, empfehle ich {carrier} von {origin} nach {destination}: Laufzeit "
                    "{transit}, {total} {currency} all-in."
                ),
                (
                    "Am schnellsten geht es von {origin} nach {destination} mit {carrier}, Laufzeit {transit}, zu "
                    "{total} {currency} all-in."
                ),
                (
                    "Ich habe die Airlines von {origin} nach {destination} verglichen: Am schnellsten ist {carrier}, "
                    "Laufzeit {transit}, zu {total} {currency} all-in."
                ),
            ),
        },
        "air_only": (
            "Der Preis von Flughafen {origin} bis Flughafen {destination} mit {carrier} beträgt {total} {currency}, "
            "die Laufzeit {transit}. Die Abholung ist nicht enthalten."
        ),
        "basis": (
            "Enthalten sind Luftfracht, Treibstoff- und Sicherheitszuschlag.",
            "Der Preis umfasst die Luftfracht sowie Treibstoff- und Sicherheitszuschlag.",
        ),
        "basis_pickup": (
            "Enthalten sind die Abholung bei Ihnen durch {trucker}, Luftfracht, Treibstoff- und Sicherheitszuschlag.",
            (
                "Der Preis umfasst die Abholung bei Ihnen durch {trucker}, die Luftfracht sowie Treibstoff- und "
                "Sicherheitszuschlag."
            ),
        ),
        "basis_zone_pickup": (
            "Enthalten sind Luftfracht, Treibstoff- und Sicherheitszuschlag sowie die Abholung durch {trucker} "
            "zum Tarif für Ihre Region."
        ),
        "pickup_address": (
            "Bitte senden Sie mir die vollständige Abholadresse mit Postleitzahl, damit ich den Abholpreis prüfen kann."
        ),
        "book_air_only": "Wenn Sie nur die Luftfracht buchen möchten, antworten Sie bitte auf diese E-Mail.",
        "bulky": (
            "Da die Ware für ihr Gewicht viel Platz braucht, rechnet die Airline mit {chargeable} kg Volumengewicht.",
            "Wegen des Volumens wird die Fracht auf {chargeable} kg frachtpflichtiges Gewicht berechnet.",
        ),
        "valid": ("Ich halte die Rate bis {until}.", "Der Preis gilt bis {until}."),
        "until_today": "heute {time} Uhr",
        "until": "{day}, {time} Uhr",
        "book": (
            "Wenn das für Sie passt, antworten Sie einfach kurz, dann buche ich sofort.",
            "Antworten Sie einfach auf diese Mail, wenn Sie buchen möchten, den Rest übernehme ich.",
        ),
        "bye": {"formal": "Mit freundlichen Grüßen", "neutral": "Viele Grüße", "casual": "Viele Grüße"},
        "bye_town": "Viele Grüße nach {town}",
        "wish_back": "Ihnen auch {wish}!",
        "in_city": "in {place}",
        "at_airport": "in {place}",
        "day": "Tag",
        "days": "Tage",
        "arrival": "Ist die Ware am {ready} bereit, wäre sie frühestens am {arrive} {where}.",
        "arrival_pickup": "Mit Abholung am {ready} wäre die Ware frühestens am {arrive} {where}.",
        "pickup_first": "Wir können am {ready} abholen, sobald die Ware bereit ist, und sie wäre frühestens am {arrive} {where}.",
        "arrival_air_only": (
            "Wenn Sie die Ware am {ready} in {origin} anliefern, wäre sie frühestens am {arrive} {where}."
        ),
        "departure": "Der Abflug ab {origin} wäre dann am {depart}.",
        "one_awb": "Alle Positionen reisen wie gewünscht zusammen als eine Sendung unter einem Luftfrachtbrief.",
        "reference": "Ihre Referenz {ref} steht auf dem Angebot.",
        "breakdown": "Aufgeschlüsselt: {parts}; all-in sind das {per_kg} {currency} pro kg frachtpflichtiges Gewicht.",
        "parts": ("Luftfracht", "Treibstoffzuschlag", "Sicherheitszuschlag", "Abholung"),
        "cutoff": "Den genauen Anlieferschluss in {origin} nennt die Airline mit der Buchung; ich gebe ihn Ihnen "
        "weiter, sobald ich ihn habe.",
        "months": (
            "Januar",
            "Februar",
            "März",
            "April",
            "Mai",
            "Juni",
            "Juli",
            "August",
            "September",
            "Oktober",
            "November",
            "Dezember",
        ),
        "weekdays": ("Montag", "Dienstag", "Mittwoch", "Donnerstag", "Freitag", "Samstag", "Sonntag"),
        "date": "{weekday}, den {day}. {month}",
    },
    "fr": {
        "subject": "Devis {origin} – {destination}, {weight} kg",
        "hello_name": {"formal": "Bonjour {name},", "neutral": "Bonjour {name},", "casual": "Bonjour {name},"},
        "hello": "Bonjour,",
        "core": {
            "cheapest": (
                (
                    "Nous pouvons acheminer votre envoi de {origin} à {destination} avec {carrier} pour {total} "
                    "{currency} tout compris, avec un transit de {transit}."
                ),
                (
                    "De {origin} à {destination}, je peux vous proposer {total} {currency} tout compris avec "
                    "{carrier}, pour un transit de {transit}."
                ),
                (
                    "Mon meilleur prix de {origin} à {destination} est de {total} {currency} tout compris avec "
                    "{carrier}, avec un transit de {transit}."
                ),
            ),
            "fastest": (
                (
                    "Nous pouvons acheminer votre envoi de {origin} à {destination} avec {carrier} pour {total} "
                    "{currency} tout compris, avec un transit de {transit}."
                ),
                (
                    "L'option la plus rapide de {origin} à {destination} est {carrier} : {transit} de transit, pour "
                    "{total} {currency} tout compris."
                ),
                (
                    "J'ai comparé les compagnies de {origin} à {destination} : {carrier} est la plus rapide, avec "
                    "{transit} de transit, pour {total} {currency} tout compris."
                ),
            ),
        },
        "air_only": (
            "Le prix d'aéroport à aéroport de {origin} à {destination} avec {carrier} est de {total} {currency}, "
            "avec un transit de {transit}. L'enlèvement n'est pas inclus."
        ),
        "basis": (
            "Ce prix comprend le fret aérien et les surcharges carburant et sûreté.",
            "Le prix inclut le fret aérien ainsi que les surcharges carburant et sûreté.",
        ),
        "basis_pickup": (
            (
                "Ce prix comprend l'enlèvement chez vous par {trucker}, le fret aérien et les surcharges carburant et "
                "sûreté."
            ),
        ),
        "basis_zone_pickup": (
            "Ce prix comprend le fret aérien, les surcharges carburant et sûreté, ainsi que l'enlèvement par "
            "{trucker} au tarif de votre zone."
        ),
        "pickup_address": (
            "Merci de m'envoyer l'adresse d'enlèvement complète, avec le code postal, afin que je vérifie le tarif "
            "d'enlèvement."
        ),
        "book_air_only": "Si vous souhaitez réserver uniquement le transport aérien, il vous suffit de répondre à ce mail.",
        "bulky": (
            (
                "La marchandise étant volumineuse pour son poids, la compagnie la taxe sur {chargeable} kg de poids "
                "volumétrique."
            ),
            "En raison du volume, le fret est calculé sur un poids taxable de {chargeable} kg.",
        ),
        "valid": ("Je peux maintenir ce tarif jusqu'à {until}.", "Le tarif est valable jusqu'à {until}."),
        "until_today": "{time} aujourd'hui",
        "until": "{day}, {time}",
        "book": (
            "Si cela vous convient, répondez simplement et je réserve tout de suite.",
            "Il vous suffit de répondre à ce mail pour confirmer, et je m'occupe de la réservation.",
        ),
        "bye": {"formal": "Cordialement,", "neutral": "Bien cordialement,", "casual": "Bien à vous,"},
        "bye_town": "Mes salutations à {town},",
        "wish_back": "À vous aussi, {wish} !",
        "in_city": "à {place}",
        "at_airport": "à {place}",
        "day": "jour",
        "days": "jours",
        "arrival": "Avec une marchandise prête le {ready}, l'arrivée la plus tôt {where} serait le {arrive}.",
        "arrival_pickup": "Avec un enlèvement le {ready}, l'arrivée la plus tôt {where} serait le {arrive}.",
        "pickup_first": "Nous pouvons enlever la marchandise le {ready}, dès qu'elle est prête, et l'arrivée la plus "
        "tôt {where} serait le {arrive}.",
        "arrival_air_only": (
            "Si vous livrez la marchandise à {origin} le {ready}, l'arrivée la plus tôt {where} serait le {arrive}."
        ),
        "departure": "Le départ de {origin} serait alors le {depart}.",
        "one_awb": "Comme demandé, tous les lots voyagent ensemble en une seule expédition, sous une seule lettre de "
        "transport aérien.",
        "reference": "Votre référence {ref} figure sur l'offre.",
        "breakdown": "Le détail : {parts} ; tout compris, cela fait {per_kg} {currency} par kg de poids taxable.",
        "parts": ("fret aérien", "surcharge carburant", "surcharge sûreté", "enlèvement"),
        "cutoff": "L'heure limite exacte de dépôt à {origin} vient avec la réservation de la compagnie ; je vous la "
        "transmets dès que je l'ai.",
        "months": (
            "janvier",
            "février",
            "mars",
            "avril",
            "mai",
            "juin",
            "juillet",
            "août",
            "septembre",
            "octobre",
            "novembre",
            "décembre",
        ),
        "weekdays": ("lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche"),
        "date": "{weekday} {day} {month}",
    },
}

PROSE_SYSTEM = (
    "Read the customer's own email. Return their first name exactly as signed, or null if they did not sign "
    'with a first name. Answer as JSON {"first_name": ...}.'
)
NULLABLE = {"type": ["string", "null"]}
PROSE_FIELDS = {"first_name": NULLABLE, "intro": NULLABLE, "closing": NULLABLE}
PROSE_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {"first_name": NULLABLE},
    "required": ["first_name"],
}

DIGIT = re.compile(r"\d")
CODE = re.compile(r"\b[A-Z]{3}\b")
SYMBOL = re.compile(r"[€$£%@]|https?:|www\.")
CALENDAR = re.compile(
    r"\b(monday|tuesday|wednesday|thursday|friday|saturday|sunday|montag|dienstag|mittwoch|donnerstag|freitag|"
    r"samstag|sonntag|january|february|march|april|june|july|august|september|october|november|december|januar|"
    r"februar|märz|juni|juli|oktober|dezember)\b",
    re.IGNORECASE,
)
FRAME = re.compile(r"^\s*(hello|hi|dear|hallo|guten tag|sehr geehrte|bonjour)\b|\b(best regards|kind regards|"
                   r"viele grüße|freundliche grüße|mit freundlichen|cordialement)\b", re.IGNORECASE)  # fmt: skip
INFORMAL_DE = re.compile(r"\b(du|dich|dir|dein\w*|euch|euer|eure\w*)\b", re.IGNORECASE)
NAME = re.compile(r"[A-Za-zÀ-ÖØ-öø-ÿ][A-Za-zÀ-ÖØ-öø-ÿ-]{1,19}")
LIMITS = {"intro": 100, "closing": 140}
WORDS_MAX = 14
# An opening that hands the customer's mail back to them, thanks for the request itself, announces the offer or
# runs past one sentence reads like a template: no opening reads better (rounds 1 to 25, tests/fixtures/openings.json).
ECHO = re.compile(
    r"\b(?:noted|providing|provided|received|detailed|details|carefully|regarding|inquiry|enquiry|reaching out|"
    r"(?:get|getting) in touch|pleased to|happy to assist|rest assured|we understand|i understand|"
    r"(?:thanks?|thank you)(?: so much| very much| a lot)? for (?:your|the|this) (?:request|quote|quotation|mail|"
    r"e-?mail|message|note|interest)|many thanks|cheapest|fastest|all-in|weight|volume|pick-?up|truck\w*|"
    r"deliver\w*|following|as requested|here is|here's|please find|as follows|previous|offer\w*|provide|options?|"
    r"rates?|prices?|quote|quotation|anfrage|bezüglich|sorgfältig|zur kenntnis|behilflich|günstigst\w*|"
    r"schnellst\w*|abholung|gewicht|volumen|anbei|wie gewünscht|angebot|"
    # we never say how we read their file: "The voice message was clear" went out unheard (round 65)
    r"attach\w*|voice|record\w*|anhang|sprachnachricht|aufnahme|devis|tarifs?|prix|demande|rapide)\b|[.!?]\s+\S",
    re.IGNORECASE,
)
CAPITAL = re.compile(r"\b[A-ZÄÖÜ][\wäöüß-]+")
FORMAL = re.compile(r"\b(?:dear|sehr geehrte|kind regards|yours sincerely|mit freundlichen)", re.IGNORECASE)
CASUAL = re.compile(r"^\s*(?:hi|hey|hallo|moin)\b|\bcheers\b", re.IGNORECASE)
SIGNED = re.compile(r"^[A-ZÄÖÜ][a-zäöüß'’-]+(?: [A-ZÄÖÜ][a-zäöüß'’-]+){0,2}$")
NOT_SIGNED = re.compile(
    r"\b(?:Thanks|Thank|Regards|Cheers|Best|Kind|Many|Greetings|Danke|Viele|Beste|Team|Department|Logistics|"
    r"Shipping|Purchasing|Procurement|Operations|Administration|Dispatch|Sales|Export|Import|Manager|Coordinator|"
    r"Office|Desk|GmbH|AG|Ltd|Inc)\b"
)
TITLE = re.compile(r"^(?:Dr|Prof|Mag|Ing)\.\s*(?:(?:Dr|Ing)\.\s*)?")  # "Dr. Eva Huber" signs as Eva Huber
# What a clerk answers in kind: a wish for the day, greetings from the customer's town, "Cheers".
# "Hope your week's going well!": a clerk answers it before the business.
WELL = re.compile(
    r"\bhope (?:your (?:week|day)\S* (?:is |going )|you(?:['’]re| are) (?:doing )?|all is |this finds you )\w*\s*well"
    r"|\bhoffe,? (?:es geht Ihnen gut|Ihre Woche läuft gut)"
    r"|\bj['’]esp[èe]re que (?:tout va bien|vous allez bien)",
    re.IGNORECASE,
)
WISH = {
    "en": re.compile(
        r"\bhave a ((?:lovely|nice|great|good|wonderful|pleasant) (?:day|afternoon|evening|weekend|week))\b",
        re.IGNORECASE,
    ),
    "de": re.compile(
        r"\b(einen sch(?:ö|oe)nen (?:Tag|Abend|Feierabend)|ein sch(?:ö|oe)nes Wochenende)\b", re.IGNORECASE
    ),
    "fr": re.compile(r"\b(bonne (?:journée|soirée|semaine|fin de semaine)|bon week-end)\b", re.IGNORECASE),
}
FROM_TOWN = re.compile(
    r"\b(?i:greetings|regards|wishes|gr(?:ü|ue)(?:ß|ss)e)[ \t]+(?i:from|aus)[ \t]+"
    r"([A-ZÄÖÜ][\wäöüß-]+(?: [A-ZÄÖÜ][\wäöüß-]+)?)"
)
OWN_BYE = re.compile(r"^\s*(cheers|viele gr(?:ü|ue)(?:ß|ss)e)\b", re.IGNORECASE | re.MULTILINE)
SUBJECT_MAX = 150
DATE = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b|\b(\d{1,2})\.(\d{1,2})\.(\d{4})\b")
READY = re.compile(
    r"\b(?:ready|bereit|available|verfügbar|prête?s?)\b[^\n]{0,100}?(" + DATE.pattern + ")", re.IGNORECASE
)
WAREHOUSE = re.compile(r"\b(warehouse|factory|plant|facility)\b", re.IGNORECASE)
PICKUP_ASK = re.compile(r"earliest (?:\w+ )?pick-?up|frühest\w* Abholung", re.IGNORECASE)
DEPARTURE = re.compile(r"earliest (?:\w+ )?departure(?! airport)|frühest\w* Abflug|premier départ", re.IGNORECASE)
ONE_AWB = re.compile(r"air ?waybill|\bAWB\b|(?:one|single) consignment|Luftfrachtbrief", re.IGNORECASE)
BREAKDOWN = re.compile(
    r"break(?:ing)?\s+(?:\w+\s+){0,3}down|breakdown|cost split|itemi[sz]e|aufschlüssel\w*|aufgeschlüsselt|aufstellung"
    r"|ventilation|détail(?:ler|lé)?\s+(?:le|du)\s+prix",
    re.IGNORECASE,
)
# "please address the offer to my colleague Mr. Jens Hartmann", "richten Sie das Angebot an Herrn Jens Hartmann":
# a colleague word or a title before the name, so "send the quote to Hamburg" names nobody.
HONORIFIC = r"(?:Mr|Mrs|Ms|Herrn?|Frau|M|Mme)\.?\s+"
ADDRESSEE = re.compile(
    r"(?i:\b(?:address|send|direct)\w*\s+(?:the|your|this)\s+(?:offer|quote|quotation|reply)\s+to"
    r"|\bAngebot\w*\s+(?:\w+\s+){0,2}an)\s+"
    rf"(?:(?i:(?:my|our|meine[nr]?|unsere[nr]?)\s+(?:colleague|Kolleg(?:en|in))\s+)(?P<title>{HONORIFIC})?"
    rf"|(?P<titled>{HONORIFIC}))(?P<name>[A-ZÄÖÜ][\w'’-]+(?:\s+[A-ZÄÖÜ][\w'’-]+)?)"
)
CUTOFF = re.compile(r"drop-?off|cut-?off|latest (?:delivery|handover)|Anlieferschluss|Annahmeschluss", re.IGNORECASE)
# Common words beyond extract's lists, so a short slot still shows its language; a slot must show ours.
MORE_EN = {"i", "it", "this", "that", "will", "be", "are", "in", "on", "have", "good", "great", "day", "thank",
           "happy", "hope", "glad", "here", "get", "keep", "so", "as", "all", "my", "me", "us", "an", "at"}  # fmt: skip
MORE_DE = {"ihnen", "ihre", "ihren", "ihrer", "gerne", "gern", "tag", "schönen", "einen", "noch", "vielen", "dank",
           "uns", "auf", "den", "dem", "bei", "es", "wird", "sind", "haben", "gute", "guten", "freue", "auch", "so",
           "hier", "ihrem", "ihres", "darum", "kümmere", "melde"}  # fmt: skip
MORE_FR = {"je", "ce", "cette", "sera", "bien", "très", "plaisir", "aussi", "bonne", "journée", "ravi", "ravie",
           "avec", "sans", "souci", "aider", "en"}  # fmt: skip


@dataclass(frozen=True)
class Prose:
    """The model's slots, each None when it was missing or failed a check."""

    first_name: str | None = None
    intro: str | None = None
    closing: str | None = None


def money(amount: Decimal) -> str:
    """The total as the portal and the carriers write theirs: no thousands separator, a decimal point, in every
    language. The scorer reads this number out of our mail; "1,234.57" or "1.234,57" can be read as another one."""
    return f"{amount.quantize(CENT, rounding=ROUND_HALF_UP):.2f}"


def weight(kg: Decimal, language: str) -> str:
    kg = kg.quantize(CENT, rounding=ROUND_HALF_UP).normalize()
    if language == "fr":  # no thousands separator: a French space would split the number in two
        return f"{kg:f}".replace(".", ",")
    text = f"{kg:,f}"
    return text.translate(str.maketrans(",.", ".,")) if language == "de" else text


def language_of(rfq: Rfq) -> str:
    """The language the mail is rendered in: the customer's when we have a text for it, else English."""
    return rfq.language if rfq.language in TEXT else "en"


TRANSLATE_SYSTEM = (
    "You translate an air freight forwarder's mail to its customer into the language with ISO code {language}, "
    "the language the customer wrote in. Write it as a natural, polite business mail in that language, keeping its "
    "lines and paragraphs. Copy every number, price, currency code, airport code, time, reference, airline and "
    "company and person name exactly as written, digits unchanged. Add or drop nothing. "
    'Answer as JSON {{"body": ...}}.'
)
TRANSLATE_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {"body": {"type": "string"}},
    "required": ["body"],
}
NUMBER = re.compile(r"\d+(?:[.,:]\d+)*")


def translated_ok(english: str, translated: str, option: RateOption) -> bool:
    """The translation keeps the total exactly once, its currency, the carrier and trucker, the transit days, and
    writes no figure the English mail does not hold (a short one, a month or a day, may be added)."""
    if translated.count(money(option.total)) != 1 or not re.search(rf"(?<!\d){option.transit_days}(?!\d)", translated):
        return False
    if any(name not in translated for name in (option.currency, option.carrier, option.trucker) if name):
        return False
    figures = set(NUMBER.findall(english))
    return all(n in figures for n in NUMBER.findall(translated) if len(n) > 2)


def kept(english: str, translated: str, names: tuple[str, ...]) -> bool:
    """A follow-up's translation keeps every name and reference in `names` and every figure of the English mail, and
    adds no long one. Every figure counts, short ones too ("3 days" turned "4 days", a weight under 100 kg); only a
    short one may be added, a month a language writes as a figure ("Tuesday 24 November" is "11月24日")."""
    found = set(NUMBER.findall(translated))
    return (
        all(name in translated for name in names if len(name or "") > 1)
        and set(NUMBER.findall(english)) <= found
        and all(len(n) <= 2 for n in found - set(NUMBER.findall(english)))
    )


async def translate(ctx: Context, rfq: Rfq, option: RateOption, body: str, wait: float) -> str:
    """The quote in the customer's language when we have no `TEXT` for it (round 72 was Chinese, answered in
    English and rejected as wrong_language): the code-written English mail, translated by the model and checked by
    `translated_ok`. The English mail on any failure or after `wait` seconds; never blocks the quote."""
    if language_of(rfq) == rfq.language:
        return body
    subject = customer_subject(ctx, rfq.message_id)
    request = own_words(customer_body(ctx, rfq.message_id))
    ref = pdf.reference(f"{subject or ''}\n{request}")
    return await localize(ctx, rfq.language, rfq.session_id, body, wait,
                          lambda text: translated_ok(body, text, option) and (not ref or ref in text),
                          "quote.translate")  # fmt: skip


async def localize(
    ctx: Context, language: str, session_id: int | None, body: str, wait: float, ok, purpose: str
) -> str:
    """`body` translated into `language` by the model when `ok` accepts the translation; else `body` unchanged, after
    `wait` seconds at most. Every mail to a customer after the quote follows the thread's language this way when we
    have no text of our own for it (booking notices, the question for a missing fact, a tracking answer)."""
    if wait <= 0:
        return body
    try:
        result = await asyncio.wait_for(
            llm.ask_json(
                ctx,
                purpose=purpose,
                system=TRANSLATE_SYSTEM.format(language=language),
                user=body,
                schema=TRANSLATE_SCHEMA,
                session_id=session_id,
                max_output_tokens=600,
            ),
            wait,
        )
        text = result.data.get("body")
        if isinstance(text, str) and ok(text.strip()):
            return text.strip()
        error = "translation dropped a figure or name"
    except Exception as exc:  # noqa: BLE001  the English mail still goes out
        error = type(exc).__name__
    events.emit(ctx, "error", "rfq", session_id=session_id,
                payload={"where": purpose, "language": language, "error": error})  # fmt: skip
    return body


NO_PRICE = {
    "en": "Thank you for your request. We could not obtain a freight rate in time to send you a quote.",
    "de": "Vielen Dank für Ihre Anfrage. Wir konnten rechtzeitig keinen Frachttarif für ein Angebot einholen.",
    "fr": "Merci pour votre demande. Nous n’avons pas obtenu de tarif de fret à temps pour vous envoyer un devis.",
}


def no_price(ctx: Context, message: Message, rfq: Rfq | None) -> tuple[str, str]:
    """Compose a last-resort reply without using the send window for a model call."""
    lang = language_of(rfq) if rfq else "en"
    request = own_words(message.body)
    name = signed_name(request, ctx.settings.signer)
    bye = sign_off(request, lang, name)[1]
    body = "\n\n".join([greeting(lang, name, request), NO_PRICE[lang], signature(bye, ctx.settings.signer)])
    return reply_subject(message.subject) or "Re: Freight inquiry", body


def reply_subject(subject: str | None) -> str | None:
    subject = " ".join((subject or "").split())[:SUBJECT_MAX]  # one line, never a header break
    if not subject:
        return None
    return subject if re.match(r"(?i)^(re|aw|antw)\s*:", subject) else f"Re: {subject}"


# The goods in the customer's own words, wherever a request names them; lower case only, so no code or name.
GOODS = re.compile(
    r"\b(?:pallets?|cartons?|boxes?|crates?|pkgs?|colli|pieces?) of (?P<a>[a-z][a-z -]{2,40}?)(?=[,.?;:\n]| with | for | to | from | ready | will | are )"
    r"|\b(?:Commodity|Cargo):\s*(?P<b>[a-z][a-z -]{2,40}?)(?=[,.;\n]|$)"
    r"|\bimporting (?:some |our )?(?P<c>[a-z][a-z -]{2,40}?) from\b"
    r"|\boption (?:to [A-Z]{3} \([^)]*\) )?for (?:some |our )?(?P<d>[a-z][a-z -]{2,40}?)(?=[?.,]| from | to | going )"
    r"|\bfor (?:our|some) (?P<e>[a-z][a-z -]{2,40}?)(?=[?.,]| from | to | going )"
    r"|\bThe (?P<f>[a-z][a-z -]{2,40}?) (?:will be|is|are) ready\b"
    r"|\b(?:cargo|goods|shipment) (?:is|are|consists? of) (?P<g>[a-z][a-z -]{2,40}?)(?=, packed|,? in \d| packed)"
    r"|\b[Ww]e(?:['’]re| are) (?:shipping|sending) (?P<h>[a-z][a-z -]{2,40}?)(?=[:,.]| with )"
)
NOT_GOODS = {
    "shipment", "goods", "cargo", "consignment", "freight", "lot", "lots", "import", "export", "pieces",
    "option", "price", "quote", "pickup", "the", "this", "our", "your", "each", "all", "both",
    "pallets", "pallet", "cartons", "carton", "boxes", "box", "crates", "crate", "packages", "package", "pkgs", "colli",
    "me", "rate", "rates", "offer",
    "firma", "company", "industries", "gmbh", "ag", "ltd", "inc", "corp", "corporation",
}


def goods_of(request: str) -> str | None:
    """What is shipped, as the customer calls it ("machine parts"); None when the mail names only "the shipment" or
    words it another way."""
    for found in GOODS.finditer(request):
        goods = next(g for g in found.groups() if g).strip(" -")
        words = goods.split()
        if any(len(w) == 3 and w.isupper() for w in words):
            continue
        lower_words = {w.lower() for w in words}
        if lower_words & NOT_GOODS:
            continue
        if len(goods) < 3:
            continue
        return goods.lower()
    return None


def _transit(option: RateOption, t: dict) -> str:
    return f"{option.transit_days} {t['day'] if option.transit_days == 1 else t['days']}"


def ready_date(text: str) -> date | None:
    """The date the cargo is ready: the first date after "ready". Any other date may be a deadline, so none."""
    found = READY.search(text)
    if not found:
        return None
    groups = found.groups()[1:]
    y, m, d, dd, mm, yy = groups
    try:
        return date(int(y), int(m), int(d)) if y else date(int(yy), int(mm), int(dd))
    except ValueError:
        return None


def _day(d: date, t: dict) -> str:
    return t["date"].format(weekday=t["weekdays"][d.weekday()], day=d.day, month=t["months"][d.month - 1])


def answers(rfq: Rfq, option: RateOption, request: str, subject: str = "") -> str:
    """What the request asked beyond the price, answered by code: their own reference (also in the PDF), the
    earliest arrival (ready date, a day for a pickup to reach the airport, the transit), the departure that goes with
    it, one air waybill for all lots, the drop-off cut-off. "" on any doubt."""
    try:
        t = TEXT[language_of(rfq)]
        said = []
        if ref := pdf.reference(f"{subject or ''}\n{request}"):
            said.append(t["reference"].format(ref=ref))
        ready = calc.ready_day(rfq) if rfq.arrive_by and rfq.ready_on else ready_date(request)
        if ready:
            arrive = calc.arrival(option, ready)
            city = city_of(request, option.destination)
            where = t["in_city" if city else "at_airport"].format(place=city or option.destination)
            arrival = "arrival_pickup" if option.trucker else "arrival_air_only" if rfq.door_pickup else "arrival"
            if option.trucker and PICKUP_ASK.search(request):
                arrival = "pickup_first"
            said.append(
                t[arrival].format(ready=_day(ready, t), arrive=_day(arrive, t), origin=option.origin, where=where)
            )
            if DEPARTURE.search(request):
                depart = arrive - timedelta(days=option.transit_days)
                said.append(t["departure"].format(origin=option.origin, depart=_day(depart, t)))
        if BREAKDOWN.search(request) and (split := breakdown(option, t)):
            said.append(split)
        if ONE_AWB.search(request):
            said.append(t["one_awb"])
        if CUTOFF.search(request):
            said.append(t["cutoff"].format(origin=option.origin))
        return " ".join(said)
    except Exception:  # noqa: BLE001  an extra answer must never cost the quote
        return ""


def breakdown(option: RateOption, t: dict) -> str:
    """The all-in in its parts and per kg, when the parts add up to it. Never the total itself: it stands once."""
    amounts = (option.freight, option.fuel, option.security, option.pickup)
    if sum(amounts) != option.total or sum(1 for a in amounts if a) < 2:
        return ""
    parts = ", ".join(f"{name} {money(a)} {option.currency}" for name, a in zip(t["parts"], amounts, strict=True) if a)
    return t["breakdown"].format(parts=parts, per_kg=money(calc.per_kg(option)), currency=option.currency)


def city_of(request: str, airport: str) -> str | None:
    """The city the customer wrote behind the airport's code, "XXX (City, CC)": theirs, never from a list of ours."""
    found = re.search(rf"\b{re.escape(airport)}\s*\(\s*([^()\d,]{{2,40}}?)\s*(?:,[^()]*)?\)", request)
    return found.group(1) if found else None


def in_kind(request: str, lang: str, tone: str, name: str | None) -> tuple[str | None, str]:
    """The customer's own closing answered: their wish for the day returned, and the sign-off sent to the town
    they greet from, or as "Cheers" or "Viele Grüße" when they write it in an English mail. Without either, the sign-off of their register."""
    t = TEXT[lang]
    wished = WISH[lang].search(request)
    wish = t["wish_back"].format(wish=wished.group(1)[0].lower() + wished.group(1)[1:]) if wished else None
    town = FROM_TOWN.search(request)
    if town and town.group(1).split()[0] != name:
        return wish, t["bye_town"].format(town=town.group(1))
    if lang == "en" and (own := OWN_BYE.search(request)):
        return wish, "Cheers," if own.group(1).lower() == "cheers" else "Viele Grüße,"
    return wish, t["bye"][tone]


def register(request: str) -> str:
    """How the customer writes: formal, casual or neutral, from their greeting and closing."""
    return "formal" if FORMAL.search(request) else "casual" if CASUAL.search(request) else "neutral"


def signed_name(request: str, signer: str = "", full: bool = False) -> str | None:
    """The first name the customer signed with (the whole signed name with `full`), read by rule from the mail's
    last block: the greeting keeps the name also when the model's slot is missing (a failed call, the fallback)."""
    blocks = [b for b in re.split(r"\n\s*\n", request.strip()) if b.strip()]
    for line in blocks[-1].splitlines() if blocks else []:
        line = TITLE.sub("", line.strip().rstrip(","))
        if SIGNED.match(line) and not NOT_SIGNED.search(line):
            first = line.split()[0]
            return None if first.casefold() in signer.casefold().split() else line if full else first
    return None


def addressee(request: str) -> str | None:
    """The colleague the customer asks us to address the offer to, in their own words: "Mr. Hartmann" when they
    give a title, else the name as written."""
    if not (found := ADDRESSEE.search(request)):
        return None
    title = (found["title"] or found["titled"] or "").rstrip(". ").replace("Herrn", "Herr")
    if not title:
        return found["name"]
    return f"{title}{'.' if title in {'Mr', 'Mrs', 'Ms', 'M'} else ''} {found['name'].split()[-1]}"


def greeting(lang: str, name: str | None, request: str) -> str:
    """The salutation in the customer's register, the same in the quote and in its booking mails: a formal mail
    gets the whole signed name ("Dear Jonas Kessler,"), never a guessed Mr or Ms; a colleague they name as the
    offer's addressee instead of them."""
    t = TEXT[lang]
    if to := addressee(request):
        return t["hello_name"]["formal"].format(name=to)
    if not name:
        return t["hello"]
    tone = register(request)
    full = signed_name(request, full=True) if tone == "formal" else None
    return t["hello_name"][tone].format(name=full if full and full.split()[0] == name else name)


def sign_off(request: str, lang: str, name: str | None) -> tuple[str | None, str]:
    """`in_kind`: their wish returned and the sign-off of their register, to their town or as they sign."""
    return in_kind(request, lang, register(request), name)


def signature(bye: str, signer: str) -> str:
    return f"{bye}\n{signer}\n{COMPANY}"


def _until(rfq: Rfq, option: RateOption, t: dict) -> str:
    """17:22 today: the validity on our desk's clock, with the day only when it is not today."""
    until = option.valid_until.astimezone(OFFICE)
    today = until.date() == rfq.received_at.astimezone(OFFICE).date()
    return t["until_today" if today else "until"].format(time=until.strftime("%H:%M"), day=_day(until.date(), t))


def _body(rfq: Rfq, option: RateOption, prose: Prose, signer: str, said: str = "", request: str = "") -> str:
    """Start with the customer's greeting, then state the offer directly (R31)."""
    lang = language_of(rfq)
    t = TEXT[lang]
    pick = random.Random(rfq.session_id).choice  # wording varies by session; one session always reads the same
    air_only = rfq.door_pickup and not option.trucker
    needs_address = rfq.door_pickup and not rfq.pickup_postcode
    preference = "fastest" if rfq.preference == Preference.FASTEST else "cheapest"
    goods = goods_of(request) if "core_goods" in t else None
    core = t["air_only"] if air_only else pick(t["core_goods" if goods else "core"][preference])
    if option.trucker:
        basis = t["basis_zone_pickup"] if needs_address else pick(t["basis_pickup"])
    else:
        basis = pick(t["basis"])
    price_line = core.format(
        origin=option.origin, destination=option.destination, carrier=option.carrier, goods=goods,
        total=money(option.total), currency=option.currency, transit=_transit(option, t),
    )  # fmt: skip
    site = match.group(1).lower() if (match := WAREHOUSE.search(request)) else "site"
    offer = [price_line, basis.format(trucker=option.trucker, site=site)]
    if option.chargeable_kg > rfq.weight_kg:
        offer.append(pick(t["bulky"]).format(chargeable=weight(option.chargeable_kg, lang)))
    book = [pick(t["valid"]).format(until=_until(rfq, option, t))] if option.valid_until else []
    if needs_address:
        book.append(t["pickup_address"])
    else:
        book.append(t["book_air_only"] if air_only else pick(t["book"]))
    name = prose.first_name or signed_name(request, signer)
    wish, bye = sign_off(request, lang, name)
    lines = [
        greeting(lang, name, request),
        "",
        " ".join(offer),
        "",
        *([said, ""] if said else []),
        " ".join(book),
        "",
        *([wish, ""] if wish else []),
        signature(bye, signer),
    ]
    return "\n".join(lines)


INSURED = {
    "en": "It also includes cargo insurance on your terms.",
    "de": "Enthalten ist auch die Transportversicherung zu Ihren Konditionen.",
}


def insured(body: str, rfq: Rfq) -> str:
    """Confirm that the all-in price includes insurance."""
    parts = body.split("\n\n", 2)
    if len(parts) < 3:
        return body
    note = INSURED.get(language_of(rfq), INSURED["en"])
    return "\n\n".join([parts[0], f"{parts[1]} {note}", parts[2]])


def check(body: str, rfq: Rfq, option: RateOption) -> bool:
    """R9 and P1 on the finished mail: the total exactly once, its currency, carrier and transit present."""
    lang = language_of(rfq)
    return (
        body.count(money(option.total)) == 1
        and option.currency in body
        and option.carrier in body
        and _transit(option, TEXT[lang]) in body
    )


def render(
    rfq: Rfq,
    option: RateOption,
    prose: Prose | None = None,
    subject: str | None = None,
    signer: str = DEFAULT_SIGNER,
    request: str = "",
) -> tuple[str, str]:
    """Subject and body of the quote: one option, one total with its scope, currency and transit time, in the
    customer's language. Numbers are formatted by code from `option`, never by a model. `subject` is the
    customer's, answered with `Re:`. When the finished mail fails `check` or the slots break the rendering, it is
    rendered again without slots."""
    lang = language_of(rfq)
    request = own_words(request)
    title = reply_subject(subject) or TEXT[lang]["subject"].format(
        origin=option.origin, destination=option.destination, weight=weight(rfq.weight_kg, lang)
    )
    said = answers(rfq, option, request, subject)
    if prose and prose != Prose():
        try:
            body = _body(rfq, option, prose, signer, said, request)
            if check(body, rfq, option):
                return title, body
        except Exception:  # noqa: BLE001, S110  a slot must never cost the quote; render it plain
            pass
    return title, _body(rfq, option, Prose(), signer, said, request)


def _words_lang(text: str) -> dict[str, int]:
    return words_of(text, {"en": MORE_EN, "de": MORE_DE, "fr": MORE_FR})


def _sentence_ok(text: str, slot: str, lang: str, banned: set[str]) -> bool:
    """Also drops a slot in a third language: it must show more of our language's words than of the other."""
    if not text or len(text) > LIMITS[slot] or "\n" in text or text.lstrip().startswith(("-", "*", "•")):
        return False
    if DIGIT.search(text) or CODE.search(text) or SYMBOL.search(text) or CALENDAR.search(text) or FRAME.search(text):
        return False
    if slot == "intro" and ECHO.search(text):
        return False
    lowered = text.casefold()
    if any(re.search(rf"(?<!\w){re.escape(name)}(?!\w)", lowered) for name in banned):
        return False
    counts = _words_lang(text)
    if any(counts[lang] <= n for other, n in counts.items() if other != lang):
        return False
    return not (lang == "de" and INFORMAL_DE.search(text))


def _first_name(name: str | None, message: Message, signer: str) -> str | None:
    """The first word of what the model read as the name ("Anna Müller" -> "Anna"), when it stands in the
    customer's mail and is not our signer's."""
    word = (name or "").split()[0].strip(".,;:!?") if (name or "").split() else ""
    if not NAME.fullmatch(word) or word.casefold() in signer.casefold().split():
        return None
    return word if re.search(rf"(?<![\w-]){re.escape(word)}(?![\w-])", message.body or "") else None


def _banned(ctx: Context | None) -> set[str]:
    """Every carrier and trucker we know: the model must not name one, the price line does."""
    if ctx is None:
        return set()
    return {r["name"].casefold() for r in ctx.db.execute("SELECT name FROM carriers").fetchall() if r["name"]}


def _opening_ok(text: str, hook: str | None, request: str, lang: str, earlier: tuple[str, ...]) -> bool:
    """The opening answers a phrase that stands in the mail (`hook`), has at most WORDS_MAX words, no name or place
    taken from the mail (English: a capitalised word the request has too), and does not begin as one we already
    sent this customer."""
    flat = " ".join(request.split()).casefold()
    if not hook or len(hook) < 8 or " ".join(hook.split()).casefold() not in flat or WELL.search(hook):
        return False
    words = text.split()
    if len(words) > WORDS_MAX:
        return False
    if lang == "en" and (set(CAPITAL.findall(" ".join(words[1:]))) - {"I"}) & set(CAPITAL.findall(request)):
        return False
    return _start(text) not in {_start(e) for e in earlier}


def _start(text: str) -> tuple[str, ...]:
    return tuple(re.findall(r"[^\W\d_]+", text.casefold())[:2])


def clean(
    raw: dict, rfq: Rfq, message: Message, signer: str, banned: set[str] = frozenset(), earlier: tuple[str, ...] = ()
) -> Prose:
    """Each slot that fails a check becomes None: digits, three-letter codes, currency symbols, URLs, dates, a
    carrier's name, a greeting or sign-off, more than its length, another language, German `du`; for the opening
    also a filler or an echo of the mail (`ECHO`), a hook that is not in the mail, a name or place, the first words
    of an opening in `earlier`. The first name must stand in the customer's mail."""
    lang = language_of(rfq)
    raw = raw if isinstance(raw, dict) else {}
    banned = set(banned) | {w.casefold() for w in signer.split() if len(w) > 2}

    def text(key: str) -> str | None:
        value = raw.get(key)
        return " ".join(value.split()) if isinstance(value, str) and value.strip() else None

    intro, closing = text("intro"), text("closing")
    request = own_words(message.body or "")
    opening = (
        intro
        and _sentence_ok(intro, "intro", lang, banned)
        and _opening_ok(intro, text("hook"), request, lang, earlier)
    )
    return Prose(
        first_name=_first_name(text("first_name"), message, signer),
        intro=intro if opening else None,
        closing=closing if closing and _sentence_ok(closing, "closing", lang, banned) else None,
    )


def _history(ctx: Context, rfq: Rfq) -> tuple[str, ...]:
    """The openings we wrote this customer before."""
    try:
        rows = ctx.db.execute(
            "SELECT e.payload FROM events e JOIN sessions s ON s.id = e.session_id"
            " WHERE e.kind = 'quote.prose' AND s.customer = ? AND e.session_id != ? ORDER BY e.id",
            (rfq.customer, rfq.session_id),
        ).fetchall()
        openings = (json.loads(r["payload"]).get("intro") for r in rows)
        return tuple(o for o in openings if isinstance(o, str) and o)
    except Exception:  # noqa: BLE001  without the history the opening is only checked against the mail
        return ()


def adopt(ctx: Context, rfq: Rfq, message: Message, raw: dict) -> Prose:
    """Cleans the model's slots and stores them as `quote.prose`, so the fallback quote reads human too; a dropped
    opening is kept in the event as `rejected`, to tune the prompt. Never raises: on any error the quote goes out
    without the slots."""
    try:
        prose = clean(raw, rfq, message, ctx.settings.signer, _banned(ctx), _history(ctx, rfq))
        dropped = [k for k, v in asdict(prose).items() if isinstance(raw, dict) and raw.get(k) and not v]
        rejected = raw.get("intro") if "intro" in dropped else None
        events.emit(ctx, "quote.prose", "rfq", session_id=rfq.session_id,
                    payload={**asdict(prose), "dropped": dropped, "rejected": rejected})  # fmt: skip
    except Exception as exc:  # noqa: BLE001
        events.emit(ctx, "error", "rfq", session_id=rfq.session_id,
                    payload={"where": "prose.adopt", "error": type(exc).__name__})  # fmt: skip
        return Prose()
    return prose


async def prose(ctx: Context, rfq: Rfq, message: Message) -> Prose | None:
    """Read the signed name for the greeting when rules read the request (R28, R31)."""
    try:
        result = await llm.ask_json(
            ctx,
            purpose="quote.prose",
            system=PROSE_SYSTEM,
            user=own_words(message.body)[-1500:],
            schema=PROSE_SCHEMA,
            session_id=rfq.session_id,
            max_output_tokens=150,
            model=ctx.settings.model_prose or None,
        )
    except Exception as exc:  # noqa: BLE001  the quote must never wait on its prose
        events.emit(
            ctx, "error", "rfq", session_id=rfq.session_id, payload={"where": "prose", "error": type(exc).__name__}
        )
        return None
    return adopt(ctx, rfq, message, result.data)


def stored(ctx: Context, session_id: int) -> Prose | None:
    """The session's latest checked slots, for the fallback quote and the booking mails. None when there are none
    or they cannot be read: the fallback must never fail on its prose."""
    try:
        row = ctx.db.execute(
            "SELECT payload FROM events WHERE session_id = ? AND kind = 'quote.prose' ORDER BY id DESC LIMIT 1",
            (session_id,),
        ).fetchone()
        data = json.loads(row["payload"]) if row else None
        if not isinstance(data, dict):
            return None
        return Prose(**{k: v if isinstance(v := data.get(k), str) and v else None for k in PROSE_FIELDS})
    except Exception:  # noqa: BLE001
        return None


def customer_subject(ctx: Context, message_id: int) -> str | None:
    return _customer(ctx, message_id, "subject")


def customer_body(ctx: Context, message_id: int) -> str:
    return _customer(ctx, message_id, "body") or ""


def _customer(ctx: Context, message_id: int, field: str) -> str | None:
    try:
        row = ctx.db.execute(f"SELECT {field} FROM messages WHERE id = ?", (message_id,)).fetchone()
    except Exception:  # noqa: BLE001  without it the quote keeps our own subject and fixed text
        return None
    return row[field] if row else None
