import logging
from dataclasses import dataclass, field
from datetime import datetime

import requests
from django.conf import settings
from django.core.cache import cache
from django.utils import timezone
from django.utils.dateparse import parse_date, parse_datetime

from .models import Shipment, ShipmentEvent

logger = logging.getLogger(__name__)

TIMEOUT = 10  # seconds
Status = Shipment.Status


class TrackingError(Exception):
    """A user-friendly error from a carrier lookup."""


@dataclass
class TrackingEvent:
    occurred_at: datetime
    description: str
    location: str = ""


@dataclass
class TrackingResult:
    status: str | None          # a Shipment.Status value, or None if not yet known
    status_text: str            # the carrier's own wording
    eta: datetime | None = None
    events: list = field(default_factory=list)
    destination: str = ""       # e.g. "Brooklyn, NY, US"


def _parse(value):
    """Parse ISO datetimes, and plain dates like '2026-10-16'."""
    if not value:
        return None

    parsed = parse_datetime(value)

    if not parsed:
        only_date = parse_date(value[:10])
        if only_date:
            parsed = datetime(only_date.year, only_date.month, only_date.day)

    if parsed and timezone.is_naive(parsed):
        parsed = timezone.make_aware(parsed)
    return parsed


def _join_place(*parts):
    """('BROOKLYN', 'NY', 'US') -> 'Brooklyn, NY, US'"""
    cleaned = []
    for index, part in enumerate(parts):
        if part:
            cleaned.append(part.title() if index == 0 else part)
    return ", ".join(cleaned)


# ---------------------------------------------------------------- DHL

DHL_STATUS_MAP = {
    "pre-transit": None,              # label created, not picked up yet
    "transit": Status.IN_TRANSIT,
    "delivered": Status.DELIVERED,
    "failure": Status.ACTION_REQUIRED,
}

# UDT ships with DHL Express; other DHL divisions can reuse the same numbers
DHL_PREFERRED_SERVICE = "express"


def _dhl_request(params):
    """Call the DHL Unified API and return its list of matching shipments."""
    response = requests.get(
        settings.DHL_TRACKING_URL,
        params=params,
        headers={"DHL-API-Key": settings.DHL_API_KEY},
        timeout=TIMEOUT,
    )

    if response.status_code == 404:
        return []
    if response.status_code == 401:
        raise TrackingError("DHL rejected the API key. Check DHL_API_KEY in .env.")
    if response.status_code == 429:
        raise TrackingError("DHL rate limit reached. Try again in a few minutes.")
    if not response.ok:
        raise TrackingError(f"DHL returned an error ({response.status_code}).")

    return response.json().get("shipments") or []


def track_dhl(awb):
    if not settings.DHL_API_KEY:
        raise TrackingError("DHL API key is not configured.")

    # 1. Ask DHL Express first, so a same-numbered parcel from another division isn't picked
    shipments = _dhl_request({"trackingNumber": awb, "service": DHL_PREFERRED_SERVICE})

    # 2. Fall back to all DHL divisions
    if not shipments:
        shipments = _dhl_request({"trackingNumber": awb})

    if not shipments:
        raise TrackingError("DHL has no record of this tracking number yet.")

    # If several divisions match, still prefer Express
    data = next(
        (item for item in shipments if item.get("service") == DHL_PREFERRED_SERVICE),
        shipments[0],
    )

    status = data.get("status") or {}
    code = status.get("statusCode", "")
    text = status.get("description") or status.get("status") or "No status yet"

    mapped = DHL_STATUS_MAP.get(code)
    if code == "transit" and "out for delivery" in text.lower():
        mapped = Status.OUT_FOR_DELIVERY

    events = []
    for item in data.get("events", []):
        occurred_at = _parse(item.get("timestamp"))
        if occurred_at:
            address = (item.get("location") or {}).get("address") or {}
            events.append(TrackingEvent(
                occurred_at=occurred_at,
                description=item.get("description") or item.get("status") or "Carrier update",
                location=address.get("addressLocality", ""),
            ))

    destination_address = (data.get("destination") or {}).get("address") or {}
    destination = _join_place(
        destination_address.get("addressLocality"),
        destination_address.get("countryCode"),
    )

    return TrackingResult(
        status=mapped,
        status_text=text,
        eta=_parse(data.get("estimatedTimeOfDelivery")),
        events=events,
        destination=destination,
    )


# ---------------------------------------------------------------- FedEx

FEDEX_STATUS_MAP = {
    "OC": None,                         # label created
    "PU": Status.IN_TRANSIT,            # picked up
    "IT": Status.IN_TRANSIT,
    "AR": Status.IN_TRANSIT,
    "DP": Status.IN_TRANSIT,
    "OD": Status.OUT_FOR_DELIVERY,
    "DL": Status.DELIVERED,
    "DE": Status.ACTION_REQUIRED,       # delivery exception
    "SE": Status.ACTION_REQUIRED,       # shipment exception
    "CD": Status.ACTION_REQUIRED,       # clearance delay
}

# Order matters: the first type found wins
FEDEX_ETA_TYPES = [
    "ACTUAL_DELIVERY",      # already delivered
    "ESTIMATED_DELIVERY",   # FedEx's forecast
    "COMMITMENT",           # promised date, e.g. "Friday before 5:00 pm"
    "APPOINTMENT_DELIVERY",
]


def _fedex_eta(result):
    """FedEx sends the delivery date in different fields depending on the service."""
    dates = {
        item.get("type"): item.get("dateTime")
        for item in result.get("dateAndTimes", [])
        if item.get("type")
    }

    for eta_type in FEDEX_ETA_TYPES:
        eta = _parse(dates.get(eta_type))
        if eta:
            return eta

    # Some international shipments only give a time window
    for window_key in ("estimatedDeliveryTimeWindow", "standardTransitTimeWindow"):
        window = (result.get(window_key) or {}).get("window") or {}
        eta = _parse(window.get("ends") or window.get("begins"))
        if eta:
            return eta

    return None


def _fedex_token():
    token = cache.get("fedex_access_token")
    if token:
        return token

    response = requests.post(
        f"{settings.FEDEX_BASE_URL}/oauth/token",
        data={
            "grant_type": "client_credentials",
            "client_id": settings.FEDEX_CLIENT_ID,
            "client_secret": settings.FEDEX_CLIENT_SECRET,
        },
        timeout=TIMEOUT,
    )
    if not response.ok:
        raise TrackingError("FedEx authentication failed. Check the API key and secret.")

    payload = response.json()
    # Cache slightly shorter than the token's lifetime
    cache.set("fedex_access_token", payload["access_token"], payload.get("expires_in", 3600) - 120)
    return payload["access_token"]


def track_fedex(awb):
    if not (settings.FEDEX_CLIENT_ID and settings.FEDEX_CLIENT_SECRET):
        raise TrackingError("FedEx API credentials are not configured.")

    response = requests.post(
        f"{settings.FEDEX_BASE_URL}/track/v1/trackingnumbers",
        json={
            "includeDetailedScans": True,
            "trackingInfo": [{"trackingNumberInfo": {"trackingNumber": awb}}],
        },
        headers={
            "Authorization": f"Bearer {_fedex_token()}",
            "Content-Type": "application/json",
            "X-locale": "en_US",
        },
        timeout=TIMEOUT,
    )
    if not response.ok:
        raise TrackingError(f"FedEx returned an error ({response.status_code}).")

    try:
        result = response.json()["output"]["completeTrackResults"][0]["trackResults"][0]
    except (KeyError, IndexError):
        raise TrackingError("Unexpected response from FedEx.")

    if result.get("error"):
        raise TrackingError(result["error"].get("message") or "FedEx could not find this tracking number.")

    latest = result.get("latestStatusDetail") or {}
    code = latest.get("code") or latest.get("derivedCode", "")
    text = latest.get("description") or latest.get("statusByLocale") or "No status yet"

    events = []
    for item in result.get("scanEvents", []):
        occurred_at = _parse(item.get("date"))
        if occurred_at:
            events.append(TrackingEvent(
                occurred_at=occurred_at,
                description=item.get("eventDescription") or "Carrier update",
                location=(item.get("scanLocation") or {}).get("city", ""),
            ))

    recipient_address = (result.get("recipientInformation") or {}).get("address") or {}
    destination = _join_place(
        recipient_address.get("city"),
        recipient_address.get("stateOrProvinceCode"),
        recipient_address.get("countryCode"),
    )

    return TrackingResult(
        status=FEDEX_STATUS_MAP.get(code),
        status_text=text,
        eta=_fedex_eta(result),
        events=events,
        destination=destination,
    )


# ---------------------------------------------------------------- Public helpers

TRACKERS = {
    Shipment.Carrier.DHL: track_dhl,
    Shipment.Carrier.FEDEX: track_fedex,
}


def track(carrier, awb):
    """Look up live status without saving anything."""
    tracker = TRACKERS.get(carrier)
    if not tracker:
        raise TrackingError("Unsupported carrier.")

    try:
        return tracker(awb)
    except requests.RequestException as exc:
        logger.warning("Carrier lookup failed for %s %s: %s", carrier, awb, exc)
        raise TrackingError("Could not reach the carrier. Check your internet connection.") from exc


# Carrier scans that prove customs has cleared the shipment
CLEARANCE_PHRASES = (
    "shipment release - export",        # FedEx: "International shipment release - Export"
    "export clearance complete",
    "customs clearance complete",
    "clearance processing complete",    # DHL Express
    "cleared customs",
)

# While client email is held for customs, routine movement doesn't override "Customs pending"
KEEP_WHILE_HELD = {Status.IN_TRANSIT, Status.OUT_FOR_DELIVERY}


def _find_clearance_event(events):
    """Return the earliest scan that confirms customs clearance, if any."""
    for event in sorted(events, key=lambda e: e.occurred_at):
        text = event.description.lower()
        if any(phrase in text for phrase in CLEARANCE_PHRASES):
            return event
    return None


def sync_shipment(shipment):
    """Fetch live status and save it to the shipment and its timeline."""
    result = track(shipment.carrier, shipment.awb)

    held = shipment.communication == Shipment.Communication.HELD

    # ---------- Automatic customs release ----------
    if held:
        clearance = _find_clearance_event(result.events)
        if clearance:
            shipment.communication = Shipment.Communication.RELEASED
            held = False

            where = f" ({clearance.location.title()})" if clearance.location else ""
            ShipmentEvent.objects.get_or_create(
                shipment=shipment,
                kind=ShipmentEvent.Kind.CUSTOMS_CLEARED,
                defaults={
                    "title": "Customs cleared",
                    "description": f"Confirmed by carrier scan: {clearance.description}{where}. "
                                   f"Client communication released.",
                    "occurred_at": clearance.occurred_at,
                },
            )

    # ---------- Status ----------
    if result.status and not (held and result.status in KEEP_WHILE_HELD):
        shipment.status = result.status
    elif not held and shipment.status == Status.CUSTOMS_PENDING:
        # Released, but the carrier hasn't reported movement yet
        shipment.status = Status.IN_TRANSIT

    # ---------- ETA, destination, carrier wording ----------
    if result.eta:
        # Keep the carrier's local date (the destination's day), not India time
        shipment.eta = result.eta.date()

    if result.destination and not shipment.destination:
        shipment.destination = result.destination[:100]

    shipment.carrier_status = result.status_text[:200]
    shipment.last_synced_at = timezone.now()
    shipment.save()

    # get_or_create avoids duplicate events on repeated syncs
    for event in result.events:
        ShipmentEvent.objects.get_or_create(
            shipment=shipment,
            kind=ShipmentEvent.Kind.CARRIER_SCAN,
            occurred_at=event.occurred_at,
            title=event.description[:150],
            defaults={"description": event.location},
        )

    return result