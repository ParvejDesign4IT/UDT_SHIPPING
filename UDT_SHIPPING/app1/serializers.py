from rest_framework import serializers

from .models import Shipment, ShipmentRecipient


class RecipientSerializer(serializers.ModelSerializer):
    class Meta:
        model = ShipmentRecipient
        fields = ["email", "notified_at"]


class ShipmentSerializer(serializers.ModelSerializer):
    carrier_display = serializers.CharField(source="get_carrier_display", read_only=True)
    purpose_display = serializers.CharField(source="get_purpose_display", read_only=True)
    status_display = serializers.CharField(source="get_status_display", read_only=True)
    communication_display = serializers.CharField(source="get_communication_display", read_only=True)
    owner_name = serializers.CharField(source="owner_short", read_only=True)

    recipients = RecipientSerializer(many=True, read_only=True)
    extra_emails = serializers.ListField(
        child=serializers.EmailField(allow_blank=True),
        required=False,
        write_only=True,
        max_length=10,
    )

    class Meta:
        model = Shipment
        fields = [
            "id", "shipment_id",
            "carrier", "carrier_display",
            "awb",
            "purpose", "purpose_display",
            "client_reference", "recipient_email", "extra_emails", "recipients",
            "internal_recipients",
            "owner_name", "branch",
            "status", "status_display",
            "communication", "communication_display",
            "eta",
            "carrier_status", "last_synced_at",
            "last_update_email_at", "delivered_email_sent_at",
            "origin", "destination",
            "pieces", "weight_kg", "length_cm", "width_cm", "height_cm", "service",
            "created_at",
        ]
        read_only_fields = [
            "id", "shipment_id", "status", "communication",
            "carrier_status", "last_synced_at",
            "last_update_email_at", "delivered_email_sent_at",
            "created_at",
        ]
        # Duplicate check is handled in validate() with a clearer message
        validators = []

    def validate_awb(self, value):
        awb = value.replace(" ", "").strip().upper()

        if not awb.isalnum():
            raise serializers.ValidationError("Tracking number can contain only letters and digits.")
        if not 9 <= len(awb) <= 35:
            raise serializers.ValidationError("Tracking number must be 9–35 characters long.")

        return awb

    def validate(self, attrs):
        instance = self.instance
        carrier = attrs.get("carrier", getattr(instance, "carrier", None))
        awb = attrs.get("awb", getattr(instance, "awb", None))
        purpose = attrs.get("purpose", getattr(instance, "purpose", None))
        recipient = attrs.get("recipient_email", getattr(instance, "recipient_email", ""))

        # DHL Express and FedEx numbers are digits only; UPS uses letters too (1Z…)
        if carrier in (Shipment.Carrier.DHL, Shipment.Carrier.FEDEX) and awb and not awb.isdigit():
            raise serializers.ValidationError(
                {"awb": "DHL and FedEx tracking numbers contain digits only."}
            )

        duplicates = Shipment.objects.filter(carrier=carrier, awb=awb)
        if instance:
            duplicates = duplicates.exclude(pk=instance.pk)
        if duplicates.exists():
            raise serializers.ValidationError(
                {"awb": "This tracking number is already registered for this carrier."}
            )

        if purpose in Shipment.EXPORT_PURPOSES and not recipient:
            raise serializers.ValidationError(
                {"recipient_email": "A client recipient is required for exports."}
            )

        return attrs

    # ---------- Save recipients together with the shipment ----------

    def _save_recipients(self, shipment, extra_emails):
        emails = [shipment.recipient_email] if shipment.recipient_email else []
        emails += [e for e in extra_emails if e]
        for email in dict.fromkeys(e.strip().lower() for e in emails):
            ShipmentRecipient.objects.get_or_create(shipment=shipment, email=email)

    def create(self, validated_data):
        extra_emails = validated_data.pop("extra_emails", [])
        shipment = super().create(validated_data)
        self._save_recipients(shipment, extra_emails)
        return shipment

    def update(self, instance, validated_data):
        extra_emails = validated_data.pop("extra_emails", [])
        shipment = super().update(instance, validated_data)
        self._save_recipients(shipment, extra_emails)
        return shipment