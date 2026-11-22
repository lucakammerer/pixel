"""Data contracts shared by every module. Change only on main, in a commit of its own, announced to all builders."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict


class Model(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class MailKind(StrEnum):
    CUSTOMER_RFQ = "customer_rfq"
    CUSTOMER_REPLY = "customer_reply"  # acceptance, decline or question in a thread we quoted in
    CARRIER_BID = "carrier_bid"  # airline or trucker answering our bid request
    RATEDESK_REPLY = "ratedesk_reply"
    BOOKING_CONFIRMATION = "booking_confirmation"
    TARIFF = "tariff"
    TRACKING_QUESTION = "tracking_question"
    OTHER = "other"


class Preference(StrEnum):
    CHEAPEST = "cheapest"
    FASTEST = "fastest"


class Source(StrEnum):
    SHEET = "sheet"
    PORTAL = "portal"
    BID = "bid"
    RATEDESK = "ratedesk"
    BROWSER = "browser"


class QuoteStatus(StrEnum):
    DRAFT = "draft"
    SENT = "sent"
    ACCEPTED = "accepted"  # the customer accepted
    DECLINED = "declined"
    EXPIRED = "expired"  # no customer answer


class BookingStatus(StrEnum):
    REQUESTED = "requested"
    BOOKED = "booked"  # portal answered 201/200
    CONFIRMED = "confirmed"  # the airline's confirmation mail arrived
    FAILED = "failed"
    CANCELLED = "cancelled"  # the customer withdrew; the portal cancelled it


class Attachment(Model):
    id: int
    filename: str
    content_type: str
    size: int


class Message(Model):
    id: int
    session_id: int  # id of the first mail of the customer thread this mail belongs to; own id if none
    direction: str  # "in" | "out"
    sender: str
    to: str
    subject: str
    body: str
    in_reply_to: int | None = None
    references: tuple[int, ...] = ()
    attachments: tuple[Attachment, ...] = ()
    sent_at: datetime
    kind: MailKind | None = None
    raw: dict[str, Any] = {}


class Rfq(Model):
    session_id: int
    message_id: int
    customer: str
    origin: str  # IATA
    destination: str  # IATA
    weight_kg: Decimal
    volume_cbm: Decimal = Decimal(0)
    pieces: int | None = None
    preference: Preference = Preference.CHEAPEST
    door_pickup: bool = False
    pickup_postcode: str | None = None
    pickup_zone: str | None = None
    currency: str | None = None  # only when the customer asks for one
    language: str = "en"
    received_at: datetime
    deadline_at: datetime
    extracted_by: str = "rules"  # "rules" | "model"
    missing: tuple[str, ...] = ()
    excluded_carriers: tuple[str, ...] = ()  # airline names or codes the customer ruled out
    required_carriers: tuple[str, ...] = ()  # the only airlines the customer allows
    ready_on: date | None = None
    arrive_by: date | None = None
    max_transit_days: int | None = None


class LaneTariff(Model):
    carrier: str
    origin: str
    destination: str
    currency: str
    valid_from: date
    valid_to: date
    version: str
    transit_days: int
    minimum: Decimal
    breaks: tuple[tuple[Decimal, Decimal], ...]  # (from kg, rate per kg), ascending
    fuel_per_kg: Decimal
    security_per_kg: Decimal
    source: str  # file name or "mail:<message id>"


class RateOption(Model):
    source: Source
    carrier: str
    carrier_code: str | None = None
    origin: str
    destination: str
    chargeable_kg: Decimal
    billed_kg: Decimal  # differs from chargeable_kg when a higher break is cheaper
    freight: Decimal
    fuel: Decimal = Decimal(0)
    security: Decimal = Decimal(0)
    pickup: Decimal = Decimal(0)
    trucker: str | None = None
    pickup_hours: int | None = None  # the trucker's pickup time, when the bid states one
    total: Decimal  # all-in, in `currency`
    currency: str
    fx_eur: Decimal | None = None  # EUR per 1 unit of `currency`, only when not EUR
    total_eur: Decimal
    transit_days: int
    rate_id: str | None = None
    valid_until: datetime | None = None
    dataset_version: str | None = None
    evidence_message_id: int | None = None


class Quote(Model):
    id: str  # CRM id
    session_id: int
    rfq: Rfq
    option: RateOption
    total: Decimal
    currency: str
    subject: str
    body: str
    status: QuoteStatus = QuoteStatus.DRAFT
    fallback: bool = False  # sent by the deadline fallback
    llm_calls: int = 0
    llm_cost_usd: Decimal = Decimal(0)
    created_at: datetime
    sent_message_id: int | None = None
    sent_at: datetime | None = None


class Booking(Model):
    id: str
    quote_id: str
    session_id: int
    team_reference: str  # the shipment's CRM id, sent to the portal
    via: str  # "portal" | "email"
    carrier: str
    rate_id: str | None = None
    booking_reference: str | None = None
    price: Decimal | None = None
    currency: str | None = None
    status: BookingStatus = BookingStatus.REQUESTED
    created_at: datetime
    confirmed_at: datetime | None = None


class Shipment(Model):
    id: str  # equals Booking.team_reference
    session_id: int
    booking_id: str
    customer: str
    origin: str
    destination: str
    weight_kg: Decimal
    carrier: str
    booking_reference: str | None = None
    status: str
    transit_days: int | None = None
    updated_at: datetime


class Event(Model):
    id: int | None = None
    ts: datetime
    session_id: int | None = None
    kind: str  # see pixel/core/events.py KINDS
    actor: str  # poller, router, rfq, tracking, tariffs, pricing, crm, llm, human
    message_id: int | None = None
    payload: dict[str, Any] = {}
    tokens_in: int = 0
    tokens_out: int = 0
    cost_usd: Decimal = Decimal(0)
    duration_ms: int = 0


class LlmResult(Model):
    data: dict[str, Any]
    model: str
    tokens_in: int
    tokens_out: int
    cost_usd: Decimal
