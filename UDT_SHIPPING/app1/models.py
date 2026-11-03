from django.conf import settings
from django.db import models
from django.urls import reverse
from django.utils import timezone


# Masked carrier accounts shown in the UI (real credentials never live here)
CARRIER_ACCOUNTS = {
    "dhl": "DHL Tracking API · Design 4 IT",
    "fedex": "FedEx · Ultimate Design Technology •••• 9167",
    "ups": "UPS · Ultimate Design Technology",
}


class Shipment(models.Model):

    class Carrier(models.TextChoices):
        DHL = "dhl", "DHL"
        FEDEX = "fedex", "FedEx"
        UPS = "ups", "UPS"

    class Purpose(models.TextChoices):
        PROTOTYPE_EXPORT = "prototype_export", "Prototype export"
        BULK_EXPORT = "bulk_export", "Bulk export"
        QC_RETURN = "qc_return", "QC return"
        MATERIAL_IMPORT = "material_import", "Material import"

    class Status(models.TextChoices):
        ACTION_REQUIRED = "action_required", "Action required"
        CUSTOMS_PENDING = "customs_pending", "Customs pending"
        VENDOR_HANDOFF = "vendor_handoff", "Vendor handoff"
        IN_TRANSIT = "in_transit", "In transit"
        OUT_FOR_DELIVERY = "out_for_delivery", "Out for delivery"
        DELIVERED = "delivered", "Delivered"

    class Communication(models.TextChoices):
        NOT_REQUIRED = "not_required", "Not required"
        HELD = "held", "Client email held"
        RELEASED = "released", "Released"

    EXPORT_PURPOSES = {Purpose.PROTOTYPE_EXPORT, Purpose.BULK_EXPORT}

    # Tracked only by website link (no API) – customs clearance can't be detected,
    # so client communication is never held for these
    LINK_ONLY_CARRIERS = {Carrier.UPS}
    
    STATUS_DESCRIPTIONS = {
        Status.ACTION_REQUIRED: "An issue needs attention from the shipment owner.",
        Status.CUSTOMS_PENDING: "Client email remains held. No qualifying clearance event yet.",
        Status.VENDOR_HANDOFF: "Shipment received by the forwarding vendor.",
        Status.IN_TRANSIT: "Shipment is moving with the carrier.",
        Status.OUT_FOR_DELIVERY: "Shipment is out for delivery today.",
        Status.DELIVERED: "Shipment delivered to the recipient.",
    }

    # ---------- Core ----------
    shipment_id = models.CharField(max_length=20, unique=True, editable=False)
    carrier = models.CharField(max_length=10, choices=Carrier.choices)
    awb = models.CharField("Tracking number / AWB", max_length=30)
    purpose = models.CharField(max_length=30, choices=Purpose.choices)
    client_reference = models.CharField(max_length=150, blank=True)
    recipient_email = models.EmailField(blank=True)
    internal_recipients = models.CharField(
        max_length=200, default="Owner, branch manager, Chief of Staff"
    )

    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="shipments",
    )
    branch = models.CharField(max_length=50, default="Mumbai")

    # ---------- Status ----------
    status = models.CharField(max_length=30, choices=Status.choices, default=Status.IN_TRANSIT)
    communication = models.CharField(
        max_length=20, choices=Communication.choices, default=Communication.NOT_REQUIRED
    )
    eta = models.DateField(null=True, blank=True)

    # ---------- Carrier sync ----------
    carrier_status = models.CharField(max_length=200, blank=True)
    last_synced_at = models.DateTimeField(null=True, blank=True)

    # ---------- Scheduled emails ----------
    last_update_email_at = models.DateTimeField(null=True, blank=True)
    delivered_email_sent_at = models.DateTimeField(null=True, blank=True)
    
    # ---------- Route ----------
    origin = models.CharField(max_length=100, default="Mumbai")
    destination = models.CharField(max_length=100, blank=True)

    # ---------- Package ----------
    pieces = models.PositiveIntegerField(default=1)
    weight_kg = models.DecimalField(max_digits=8, decimal_places=2, null=True, blank=True)
    length_cm = models.PositiveIntegerField(null=True, blank=True)
    width_cm = models.PositiveIntegerField(null=True, blank=True)
    height_cm = models.PositiveIntegerField(null=True, blank=True)
    service = models.CharField(max_length=100, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(fields=["carrier", "awb"], name="unique_carrier_awb"),
        ]

    def __str__(self):
        return f"{self.shipment_id} ({self.get_carrier_display()} {self.awb})"

    def get_absolute_url(self):
        return reverse("shipment_detail", kwargs={"shipment_id": self.shipment_id})

    # ---------- Saving + automatic timeline ----------

    def save(self, *args, **kwargs):
        is_new = self._state.adding
        previous_status = None

        if is_new:
            self.apply_initial_state()
            if not self.shipment_id:
                self.shipment_id = self.generate_shipment_id()
        else:
            previous_status = (
                Shipment.objects.filter(pk=self.pk).values_list("status", flat=True).first()
            )

        super().save(*args, **kwargs)

        if is_new:
            self.log_initial_events()
        elif previous_status and previous_status != self.status:
            self.build_status_event().save()

    def apply_initial_state(self):
        """Indian export rules: hold client communication until customs clears."""
        if self.carrier in self.LINK_ONLY_CARRIERS:
            # No carrier scans to detect clearance → email the tracking link straight away
            self.status = self.Status.IN_TRANSIT
            self.communication = self.Communication.RELEASED
        elif self.purpose in self.EXPORT_PURPOSES:
            self.status = self.Status.CUSTOMS_PENDING
            self.communication = self.Communication.HELD
        elif self.purpose == self.Purpose.QC_RETURN:
            self.status = self.Status.IN_TRANSIT
            self.communication = self.Communication.NOT_REQUIRED
        else:
            self.status = self.Status.IN_TRANSIT
            self.communication = self.Communication.RELEASED

    @classmethod
    def generate_shipment_id(cls):
        """Format: UDT-SHP-YYMM-NNNN, numbering restarts each month."""
        prefix = f"UDT-SHP-{timezone.localdate():%y%m}-"
        last_id = (
            cls.objects.filter(shipment_id__startswith=prefix)
            .order_by("-shipment_id")
            .values_list("shipment_id", flat=True)
            .first()
        )
        next_number = int(last_id.rsplit("-", 1)[1]) + 1 if last_id else 1
        return f"{prefix}{next_number:04d}"

    def build_status_event(self):
        return ShipmentEvent(
            shipment=self,
            kind=ShipmentEvent.Kind.STATUS,
            status=self.status,
            title=self.get_status_display(),
            description=self.STATUS_DESCRIPTIONS.get(self.status, ""),
        )

    def log_initial_events(self):
        events = [
            ShipmentEvent(
                shipment=self,
                kind=ShipmentEvent.Kind.AWB_REGISTERED,
                title="AWB registered",
                description=f"{self.get_carrier_display()} tracking activated for internal monitoring.",
            )
        ]

        if self.recipient_email:
            if self.communication == self.Communication.HELD:
                description = "Client email queued, not generated or sent."
            else:
                description = f"Tracking updates will be sent to {self.recipient_email}."

            events.append(ShipmentEvent(
                shipment=self,
                kind=ShipmentEvent.Kind.RECIPIENTS_SAVED,
                title="Recipients saved",
                description=description,
            ))

        events.append(self.build_status_event())

        # Saved in order, so the status event is the newest (top of the timeline)
        for event in events:
            event.save()

    # ---------- Display helpers ----------

    @property
    def is_export(self):
        return self.purpose in self.EXPORT_PURPOSES

    @property
    def awb_display(self):
        """Groups characters in fours: 612988347712 -> 6129 8834 7712"""
        return " ".join(self.awb[i:i + 4] for i in range(0, len(self.awb), 4))

    @property
    def status_class(self):
        return {
            self.Status.ACTION_REQUIRED: "red",
            self.Status.CUSTOMS_PENDING: "amber",
            self.Status.VENDOR_HANDOFF: "amber",
        }.get(self.status, "green")

    @property
    def communication_class(self):
        return {
            self.Communication.HELD: "amber",
            self.Communication.RELEASED: "green",
        }.get(self.communication, "muted")

    @property
    def communication_icon(self):
        return {
            self.Communication.HELD: "i-lock",
            self.Communication.RELEASED: "i-check",
        }.get(self.communication, "")

    @property
    def client_release_display(self):
        return {
            self.Communication.HELD: "Held until customs clears",
            self.Communication.RELEASED: "Released",
        }.get(self.communication, "Not required")

    @property
    def eta_label(self):
        if self.eta:
            return f"{self.eta.day} {self.eta:%b}"
        return {
            self.Status.ACTION_REQUIRED: "Delayed",
            self.Status.CUSTOMS_PENDING: "Pending clearance",
            self.Status.VENDOR_HANDOFF: "Awaiting clearance",
            self.Status.DELIVERED: "Delivered",
        }.get(self.status, "—")

    @property
    def eta_is_delayed(self):
        return not self.eta and self.status == self.Status.ACTION_REQUIRED

    @property
    def owner_short(self):
        """'Priya Shah' -> 'P. Shah'"""
        user = self.owner
        if user.first_name and user.last_name:
            return f"{user.first_name[0]}. {user.last_name}"
        return user.get_full_name() or user.username

    @property
    def route_display(self):
        return f"{self.origin} → {self.destination}" if self.destination else self.origin

    @property
    def dimensions_display(self):
        if self.length_cm and self.width_cm and self.height_cm:
            return f"{self.length_cm} × {self.width_cm} × {self.height_cm} cm"
        return "—"

    @property
    def carrier_account(self):
        return CARRIER_ACCOUNTS.get(self.carrier, "—")

    @property
    def tracking_url(self):
        """Public tracking page on the carrier's website."""
        if self.carrier == self.Carrier.FEDEX:
            return f"https://www.fedex.com/fedextrack/?trknbr={self.awb}"
        if self.carrier == self.Carrier.DHL:
            return f"https://www.dhl.com/in-en/home/tracking.html?tracking-id={self.awb}&submit=1"
        if self.carrier == self.Carrier.UPS:
            return f"https://www.ups.com/track?loc=en_IN&tracknum={self.awb}"
        return ""

    @property
    def carrier_events(self):
        """Real carrier scans only, for the Tracking tab."""
        return [e for e in self.events.all() if e.kind == ShipmentEvent.Kind.CARRIER_SCAN]

    @property
    def system_events(self):
        """App-generated entries (registered, customs, emails, status), for the Activity tab."""
        return [e for e in self.events.all() if e.kind != ShipmentEvent.Kind.CARRIER_SCAN]


class ShipmentEvent(models.Model):

    class Kind(models.TextChoices):
        AWB_REGISTERED = "awb_registered", "AWB registered"
        RECIPIENTS_SAVED = "recipients_saved", "Recipients saved"
        STATUS = "status", "Status change"
        CARRIER_SCAN = "carrier_scan", "Carrier scan"
        CUSTOMS_CLEARED = "customs_cleared", "Customs cleared"
        EMAIL_SENT = "email_sent", "Email sent"
        NOTE = "note", "Note"

    STATUS_STYLES = {
        Shipment.Status.ACTION_REQUIRED: ("i-alert", "red"),
        Shipment.Status.CUSTOMS_PENDING: ("i-shield", "amber"),
        Shipment.Status.VENDOR_HANDOFF: ("i-truck", "blue"),
        Shipment.Status.IN_TRANSIT: ("i-plane", "blue"),
        Shipment.Status.OUT_FOR_DELIVERY: ("i-truck", "green"),
        Shipment.Status.DELIVERED: ("i-package", "green"),
    }

    KIND_STYLES = {
        Kind.AWB_REGISTERED: ("i-link", "teal"),
        Kind.RECIPIENTS_SAVED: ("i-mail", "gray"),
        Kind.CARRIER_SCAN: ("i-truck", "blue"),
        Kind.CUSTOMS_CLEARED: ("i-shield", "green"),
        Kind.EMAIL_SENT: ("i-mail", "green"),
        Kind.NOTE: ("i-check", "gray"),
    }

    shipment = models.ForeignKey(Shipment, on_delete=models.CASCADE, related_name="events")
    kind = models.CharField(max_length=30, choices=Kind.choices)
    status = models.CharField(max_length=30, choices=Shipment.Status.choices, blank=True)
    title = models.CharField(max_length=150)
    description = models.TextField(blank=True)
    occurred_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["-occurred_at", "-id"]

    def __str__(self):
        return f"{self.shipment.shipment_id}: {self.title}"

    @property
    def style(self):
        if self.kind == self.Kind.STATUS:
            return self.STATUS_STYLES.get(self.status, ("i-check", "gray"))
        return self.KIND_STYLES.get(self.kind, ("i-check", "gray"))

    @property
    def icon(self):
        return self.style[0]

    @property
    def color(self):
        return self.style[1]


class ShipmentRecipient(models.Model):
    """One email address to notify about a shipment."""

    shipment = models.ForeignKey(Shipment, on_delete=models.CASCADE, related_name="recipients")
    email = models.EmailField()
    notified_at = models.DateTimeField(null=True, blank=True)   # empty = not emailed yet
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["id"]
        constraints = [
            models.UniqueConstraint(fields=["shipment", "email"], name="unique_shipment_recipient"),
        ]

    def __str__(self):
        return f"{self.shipment.shipment_id} → {self.email}"