from django.urls import path

from .views import (
    UserLoginView, UserLogoutView, DashboardView,
    ShipmentsView, RegisterTrackingView, TrackingPreviewView,
    ShipmentDetailView, ShipmentSyncView, ShipmentDeleteView,
    AddressesView, NotificationsView, SettingsView,
    ShipmentListCreateAPIView, ShipmentDetailAPIView,
)


urlpatterns = [
    # Auth
    path("", UserLoginView.as_view(), name="login"),
    path("logout/", UserLogoutView.as_view(), name="logout"),

    # Pages
    path("dashboard/", DashboardView.as_view(), name="dashboard"),
    path("shipments/", ShipmentsView.as_view(), name="shipments"),

    # Fixed paths must come before <str:shipment_id>
    path("shipments/register/", RegisterTrackingView.as_view(), name="register_tracking"),
    path("shipments/track-preview/", TrackingPreviewView.as_view(), name="tracking_preview"),

    path("shipments/<str:shipment_id>/", ShipmentDetailView.as_view(), name="shipment_detail"),
    path("shipments/<str:shipment_id>/sync/", ShipmentSyncView.as_view(), name="shipment_sync"),
    path("shipments/<str:shipment_id>/delete/", ShipmentDeleteView.as_view(), name="shipment_delete"),

    path("addresses/", AddressesView.as_view(), name="addresses"),
    path("notifications/", NotificationsView.as_view(), name="notifications"),
    path("settings/", SettingsView.as_view(), name="settings"),

    # REST API
    path("api/shipments/", ShipmentListCreateAPIView.as_view(), name="api_shipments"),
    path("api/shipments/<int:pk>/", ShipmentDetailAPIView.as_view(), name="api_shipment_detail"),
]