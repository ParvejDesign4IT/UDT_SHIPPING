from django.contrib import admin

from .models import Shipment, ShipmentEvent


class ShipmentEventInline(admin.TabularInline):
    model = ShipmentEvent
    extra = 0
    fields = ("occurred_at", "kind", "status", "title", "description")


@admin.register(Shipment)
class ShipmentAdmin(admin.ModelAdmin):
    list_display = (
        "shipment_id", "carrier", "awb", "purpose", "status",
        "communication", "owner", "last_synced_at", "created_at",
    )
    list_filter = ("carrier", "purpose", "status", "communication")
    search_fields = ("shipment_id", "awb", "client_reference", "recipient_email")
    readonly_fields = ("shipment_id", "carrier_status", "last_synced_at", "created_at", "updated_at")
    inlines = [ShipmentEventInline]

    fieldsets = (
        ("Shipment", {"fields": ("shipment_id", "carrier", "awb", "purpose", "client_reference", "owner", "branch")}),
        ("Status", {"fields": ("status", "communication", "eta")}),
        ("Carrier sync", {"fields": ("carrier_status", "last_synced_at")}),
        ("Recipients", {"fields": ("recipient_email", "internal_recipients")}),
        ("Route", {"fields": ("origin", "destination")}),
        ("Package", {"fields": ("pieces", "weight_kg", "length_cm", "width_cm", "height_cm", "service")}),
        ("Timestamps", {"fields": ("created_at", "updated_at")}),
    )