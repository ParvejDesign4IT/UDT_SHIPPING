from datetime import timedelta

from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.contrib.auth.views import LoginView, LogoutView
from django.db.models import Case, Count, IntegerField, Q, Value, When
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse_lazy
from django.utils import timezone
from django.views import View
from django.views.generic import DetailView, ListView, TemplateView
from rest_framework import generics, permissions

from .forms import LoginForm
from .models import Shipment
from .serializers import ShipmentSerializer
from .tracking import TrackingError, sync_shipment, track
from django.utils.http import url_has_allowed_host_and_scheme 

ATTENTION_STATUSES = [
    Shipment.Status.ACTION_REQUIRED,
    Shipment.Status.CUSTOMS_PENDING,
    Shipment.Status.VENDOR_HANDOFF,
]

TRANSIT_STATUSES = [
    Shipment.Status.IN_TRANSIT,
    Shipment.Status.OUT_FOR_DELIVERY,
]

class ShipmentDeleteView(LoginRequiredMixin, View):
    """Deletes a shipment and its timeline. POST only, after the confirmation dialog."""

    def post(self, request, shipment_id):
        shipment = get_object_or_404(Shipment, shipment_id=shipment_id)

        # Only the shipment's owner or a staff user may delete it
        if not (request.user.is_staff or shipment.owner_id == request.user.id):
            messages.error(request, "You don't have permission to delete this shipment.")
        else:
            deleted_id = shipment.shipment_id
            shipment.delete()          # timeline events are removed with it
            messages.success(request, f"{deleted_id} deleted.")

        # Return to the page the user came from (dashboard or shipments list)
        next_url = request.POST.get("next", "")
        if next_url and "/shipments/UDT-" not in next_url and url_has_allowed_host_and_scheme(
            next_url, allowed_hosts={request.get_host()}
        ):
            return redirect(next_url)
        return redirect("shipments")
    
# ================================================================ Auth

class UserLoginView(LoginView):
    template_name = "login.html"
    authentication_form = LoginForm
    redirect_authenticated_user = True

    def get_success_url(self):
        return self.get_redirect_url() or reverse_lazy("dashboard")


class UserLogoutView(LogoutView):
    next_page = reverse_lazy("login")


# ================================================================ Dashboard

class DashboardView(LoginRequiredMixin, TemplateView):
    template_name = "dashboard.html"
    row_limit = 10

    # Shown as tabs above the table
    TAB_KEYS = ["active", "attention", "delivered", "drafts"]

    # Shown as clickable summary cards: key -> (icon, colour)
    CARD_STYLES = {
        "attention": ("i-alert", "red"),
        "customs": ("i-shield", "amber"),
        "transit": ("i-plane", "blue"),
        "delivered_30": ("i-package", "green"),
    }

    def get_filters(self):
        """One definition per filter, shared by the cards and the table."""
        delivered = Q(status=Shipment.Status.DELIVERED)
        last_30_days = Q(updated_at__gte=timezone.now() - timedelta(days=30))

        return {
            "active": ("Active", ~delivered),
            "attention": ("Needs attention", Q(status__in=ATTENTION_STATUSES)),
            "customs": ("Customs pending", Q(communication=Shipment.Communication.HELD) & ~delivered),
            "transit": ("In transit", Q(status__in=TRANSIT_STATUSES)),
            "delivered_30": ("Delivered · 30 days", delivered & last_30_days),
            "delivered": ("Delivered", delivered),
            "drafts": ("Drafts", None),     # no draft records yet
        }

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        filters = self.get_filters()

        current_tab = self.request.GET.get("tab", "active")
        if current_tab not in filters:
            current_tab = "active"

        current_label, current_filter = filters[current_tab]
        query = self.request.GET.get("q", "").strip()

        # ---------- Table rows ----------
        shipments = Shipment.objects.select_related("owner")
        shipments = shipments.none() if current_filter is None else shipments.filter(current_filter)

        if query:
            shipments = shipments.filter(
                Q(shipment_id__icontains=query)
                | Q(awb__icontains=query.replace(" ", ""))
                | Q(client_reference__icontains=query)
            )

        # Exceptions first, then customs-held, then everything else (newest first)
        shipments = shipments.annotate(
            priority=Case(
                When(status=Shipment.Status.ACTION_REQUIRED, then=Value(0)),
                When(status=Shipment.Status.CUSTOMS_PENDING, then=Value(1)),
                When(status=Shipment.Status.VENDOR_HANDOFF, then=Value(2)),
                default=Value(9),
                output_field=IntegerField(),
            )
        ).order_by("priority", "-created_at")

        total = shipments.count()

        # ---------- Summary cards (one query, same filters as the table) ----------
        counts = Shipment.objects.aggregate(**{
            key: Count("id", filter=filters[key][1]) for key in self.CARD_STYLES
        })

        cards = [
            {
                "key": key,
                "label": filters[key][0],
                "value": counts[key],
                "icon": icon,
                "color": color,
            }
            for key, (icon, color) in self.CARD_STYLES.items()
        ]

        context.update({
            "shipments": shipments[:self.row_limit],
            "total": total,
            "row_limit": self.row_limit,
            "cards": cards,
            "tabs": [(key, filters[key][0]) for key in self.TAB_KEYS],
            "current_tab": current_tab,
            "current_label": current_label,
            "is_card_filter": current_tab not in self.TAB_KEYS,
            "query": query,
        })
        return context


# ================================================================ Shipments

class ShipmentsView(LoginRequiredMixin, ListView):
    model = Shipment
    template_name = "shipments.html"
    context_object_name = "shipments"
    paginate_by = 10

    def get_queryset(self):
        return Shipment.objects.select_related("owner")


class ShipmentDetailView(LoginRequiredMixin, DetailView):
    model = Shipment
    template_name = "shipment_detail.html"
    context_object_name = "shipment"
    slug_field = "shipment_id"
    slug_url_kwarg = "shipment_id"

    def get_queryset(self):
        return Shipment.objects.select_related("owner").prefetch_related("events")


class RegisterTrackingView(LoginRequiredMixin, View):
    template_name = "register_tracking.html"

    def get_context(self, form_data=None, errors=None):
        return {
            "carrier_choices": Shipment.Carrier.choices,
            "purpose_choices": Shipment.Purpose.choices,
            "form_data": form_data or {},
            "errors": errors or {},
        }

    def get(self, request):
        return render(request, self.template_name, self.get_context())

    def post(self, request):
        serializer = ShipmentSerializer(data=request.POST)

        if serializer.is_valid():
            shipment = serializer.save(owner=request.user)

            try:
                sync_shipment(shipment)
                messages.success(
                    request,
                    f"{shipment.shipment_id} registered. Carrier status: {shipment.carrier_status}",
                )
            except TrackingError as exc:
                messages.warning(
                    request,
                    f"{shipment.shipment_id} registered, but live status could not be fetched: {exc}",
                )

            return redirect("shipments")

        # Re-show the form with the user's input and the errors
        return render(
            request,
            self.template_name,
            self.get_context(request.POST, serializer.errors),
            status=400,
        )


class TrackingPreviewView(LoginRequiredMixin, View):
    """JSON lookup used by the register form while the user types."""

    def get(self, request):
        carrier = request.GET.get("carrier", "")
        awb = request.GET.get("awb", "").replace(" ", "")

        if len(awb) < 10:
            return JsonResponse({"ok": False, "error": "Enter the full tracking number."}, status=400)

        try:
            result = track(carrier, awb)
        except TrackingError as exc:
            return JsonResponse({"ok": False, "error": str(exc)}, status=502)

        latest = result.events[0] if result.events else None

        return JsonResponse({
            "ok": True,
            "status_label": Shipment.Status(result.status).label if result.status else "Label created",
            "status_text": result.status_text,
            # Carrier's local date (the destination's day), not India time
            "eta": result.eta.strftime("%d %b %Y") if result.eta else None,
            "last_event": latest.description if latest else None,
            "last_location": latest.location if latest else None,
        })


class ShipmentSyncView(LoginRequiredMixin, View):
    """Refresh button on the detail page."""

    def post(self, request, shipment_id):
        shipment = get_object_or_404(Shipment, shipment_id=shipment_id)

        try:
            sync_shipment(shipment)
            messages.success(request, f"Status refreshed: {shipment.carrier_status}")
        except TrackingError as exc:
            messages.error(request, f"Could not refresh status: {exc}")

        return redirect(shipment)


# ================================================================ Other pages

class AddressesView(LoginRequiredMixin, TemplateView):
    template_name = "addresses.html"


class NotificationsView(LoginRequiredMixin, TemplateView):
    template_name = "notifications.html"


class SettingsView(LoginRequiredMixin, TemplateView):
    template_name = "settings.html"


# ================================================================ REST API

class ShipmentListCreateAPIView(generics.ListCreateAPIView):
    queryset = Shipment.objects.select_related("owner")
    serializer_class = ShipmentSerializer
    permission_classes = [permissions.IsAuthenticated]

    def perform_create(self, serializer):
        serializer.save(owner=self.request.user)


class ShipmentDetailAPIView(generics.RetrieveUpdateDestroyAPIView):
    queryset = Shipment.objects.select_related("owner")
    serializer_class = ShipmentSerializer
    permission_classes = [permissions.IsAuthenticated]