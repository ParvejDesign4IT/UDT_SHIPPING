from rest_framework import serializers

from .models import Shipment


class ShipmentSerializer(serializers.ModelSerializer):
    carrier_display = serializers.CharField(source="get_carrier_display", read_only=True)
    purpose_display = serializers.CharField(source="get_purpose_display", read_only=True)
    status_display = serializers.CharField(source="get_status_display", read_only=True)
    communication_display = serializers.CharField(source="get_communication_display", read_only=True)
    owner_name = serializers.CharField(source="owner_short", read_only=True)

    class Meta:
        model = Shipment
        fields = [
            "id", "shipment_id",
            "carrier", "carrier_display",
            "awb",
            "purpose", "purpose_display",
            "client_reference", "recipient_email", "internal_recipients",
            "owner_name", "branch",
            "status", "status_display",
            "communication", "communication_display",
            "eta",
            "carrier_status", "last_synced_at",
            "origin", "destination",
            "pieces", "weight_kg", "length_cm", "width_cm", "height_cm", "service",
            "created_at",
        ]
        read_only_fields = [
            "id", "shipment_id", "status", "communication",
            "carrier_status", "last_synced_at", "created_at",
        ]
        # Duplicate check is handled in validate() with a clearer message
        validators = []

    def validate_awb(self, value):
        awb = value.replace(" ", "").strip()

        if not awb.isdigit():
            raise serializers.ValidationError("Tracking number must contain digits only.")
        if not 10 <= len(awb) <= 22:
            raise serializers.ValidationError("Tracking number must be 10–22 digits long.")

        return awb

    def validate(self, attrs):
        instance = self.instance
        carrier = attrs.get("carrier", getattr(instance, "carrier", None))
        awb = attrs.get("awb", getattr(instance, "awb", None))
        purpose = attrs.get("purpose", getattr(instance, "purpose", None))
        recipient = attrs.get("recipient_email", getattr(instance, "recipient_email", ""))

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