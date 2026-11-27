import json
import logging
import uuid

from django.conf import settings
from django.db import IntegrityError
from rest_framework import generics, status
from rest_framework.throttling import AnonRateThrottle
from rest_framework_simplejwt.tokens import RefreshToken
from rest_framework_simplejwt.views import TokenObtainPairView
from rest_framework.permissions import AllowAny, IsAdminUser, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework.filters import OrderingFilter, SearchFilter

from . import paystack
from .models import Combo, Notification, Order, Product
from .serializers import (ComboSerializer, EmailOrUsernameTokenSerializer, MeSerializer,
                          NotificationSerializer, OrderSerializer, ProductSerializer,
                          RegisterSerializer)
from .services import (CheckoutError, PaymentVerificationError, apply_paystack_payment,
                       create_order_from_cart, safe_send_mail)

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# Accounts
# --------------------------------------------------------------------------- #
class RegisterThrottle(AnonRateThrottle):
    scope = 'register'
    rate = '30/hour'  # per IP; generous because many customers share mobile-network IPs


class LoginThrottle(AnonRateThrottle):
    scope = 'login'
    rate = '20/min'


class LoginView(TokenObtainPairView):
    """POST {username, password}. `username` may be a username or an email address."""
    serializer_class = EmailOrUsernameTokenSerializer
    throttle_classes = [LoginThrottle]


class RegisterView(APIView):
    """Create a customer account and log them straight in."""
    permission_classes = [AllowAny]
    authentication_classes = []
    throttle_classes = [RegisterThrottle]

    def post(self, request):
        serializer = RegisterSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            user = serializer.save()
        except IntegrityError:  # two signups raced for the same username
            return Response({'error': 'That username or email was just taken, please try again'},
                            status=status.HTTP_400_BAD_REQUEST)
        refresh = RefreshToken.for_user(user)
        return Response({
            'user': MeSerializer(user).data,
            'access': str(refresh.access_token),
            'refresh': str(refresh),
        }, status=status.HTTP_201_CREATED)


class MeView(APIView):
    """The logged-in customer's own profile."""
    permission_classes = [IsAuthenticated]

    def get(self, request):
        return Response(MeSerializer(request.user).data)

    def patch(self, request):
        serializer = MeSerializer(request.user, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(serializer.data)


# --------------------------------------------------------------------------- #
# Catalogue
# --------------------------------------------------------------------------- #
class ProductListView(generics.ListAPIView):
    """In-stock products. Supports ?search=, ?ordering=price (or -price, name, created_at)."""
    queryset = Product.objects.filter(stock__gt=0)
    serializer_class = ProductSerializer
    permission_classes = [AllowAny]
    filter_backends = [SearchFilter, OrderingFilter]
    search_fields = ['name', 'description']
    ordering_fields = ['price', 'name', 'created_at']
    ordering = ['-created_at']


class ProductDetailView(generics.RetrieveAPIView):
    """One product. Sold-out products still load (stock 0) so shared links and carts keep working."""
    queryset = Product.objects.all()
    serializer_class = ProductSerializer
    permission_classes = [AllowAny]


class ComboListView(generics.ListAPIView):
    """Active combos. Supports ?search=."""
    queryset = Combo.objects.filter(is_active=True).prefetch_related('products')
    serializer_class = ComboSerializer
    permission_classes = [AllowAny]
    filter_backends = [SearchFilter]
    search_fields = ['name', 'description']


class ComboDetailView(generics.RetrieveAPIView):
    queryset = Combo.objects.filter(is_active=True).prefetch_related('products')
    serializer_class = ComboSerializer
    permission_classes = [AllowAny]

class AdminProductCreateView(generics.CreateAPIView):
    queryset = Product.objects.all()
    serializer_class = ProductSerializer
    permission_classes = [IsAdminUser]


# --------------------------------------------------------------------------- #
# Checkout and orders
# --------------------------------------------------------------------------- #
class CheckoutView(APIView):
    """Create an unpaid order from the cart. Payment is a separate step."""
    permission_classes = [IsAuthenticated]

    def post(self, request):
        data = request.data
        delivery_address = (data.get('delivery_address') or '').strip()
        phone = (data.get('phone') or '').strip()
        if not delivery_address or not phone:
            return Response({'error': 'delivery_address and phone are required'},
                            status=status.HTTP_400_BAD_REQUEST)

        try:
            order = create_order_from_cart(
                request.user, data.get('cart_items'), delivery_address, phone)
        except CheckoutError as exc:
            return Response({'error': exc.message}, status=exc.status_code)

        return Response(OrderSerializer(order).data, status=status.HTTP_201_CREATED)


class OrderListView(generics.ListAPIView):
    serializer_class = OrderSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        return (Order.objects.filter(user=self.request.user)
                .prefetch_related('items__product', 'items__combo'))

class NotificationListView(generics.ListAPIView):
    serializer_class = NotificationSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        return Notification.objects.filter(user=self.request.user).order_by('-created_at')

class NotificationMarkReadView(APIView):
    permission_classes = [IsAuthenticated]

    def patch(self, request, pk):
        notification = Notification.objects.filter(user=self.request.user, pk=pk).first()
        if notification is None:
            return Response({"error": "Notification not found"}, status=status.HTTP_404_NOT_FOUND)
        notification.is_read = True
        notification.save()
        return Response(NotificationSerializer(notification).data)


class OrderDetailView(generics.RetrieveAPIView):
    serializer_class = OrderSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        return (Order.objects.filter(user=self.request.user)
                .prefetch_related('items__product', 'items__combo'))


# --------------------------------------------------------------------------- #
# Paystack
# --------------------------------------------------------------------------- #
class PaymentInitializeView(APIView):
    """Step 1 of paying: ask Paystack for a checkout session for this order."""
    permission_classes = [IsAuthenticated]

    def post(self, request):
        order = Order.objects.filter(pk=request.data.get('order_id'),
                                     user=request.user).first() \
            if str(request.data.get('order_id', '')).isdigit() else None
        if order is None:
            return Response({'error': 'Order not found'}, status=status.HTTP_404_NOT_FOUND)
        if order.is_paid:
            return Response({'error': 'This order has already been paid'},
                            status=status.HTTP_400_BAD_REQUEST)

        if order.status == 'CANCELLED':
            return Response({'error': 'This order has expired, please place a new order'},
                            status=status.HTTP_400_BAD_REQUEST)
        
        if not request.user.email:
            return Response({'error': 'Your account needs an email address to pay'},
                            status=status.HTTP_400_BAD_REQUEST)

        reference = f"HVN-{order.id}-{uuid.uuid4().hex[:12]}"
        try:
            session = paystack.initialize_transaction(
                email=request.user.email,
                amount=order.total_amount,
                reference=reference,
                metadata={'order_id': order.id},
            )
        except paystack.PaystackError as exc:
            logger.error("Paystack initialize failed for order %s: %s", order.id, exc)
            return Response({'error': 'Payment provider is unavailable, please try again'},
                            status=status.HTTP_502_BAD_GATEWAY)

        order.paystack_reference = reference
        order.save(update_fields=['paystack_reference'])

        return Response({
            'reference': reference,
            'authorization_url': session.get('authorization_url'),  # redirect flow
            'access_code': session.get('access_code'),              # popup flow
            'public_key': settings.PAYSTACK_PUBLIC_KEY,
            'email': request.user.email,
            'amount': paystack.to_pesewas(order.total_amount),      # in pesewas
            'currency': paystack.CURRENCY,
        })


class PaymentVerifyView(APIView):
    """Step 2: after the customer pays, the frontend sends the reference here.

    The server asks Paystack directly. Nothing the browser says is trusted.
    """
    permission_classes = [IsAuthenticated]

    def post(self, request):
        reference = (request.data.get('reference') or '').strip()
        if not reference:
            return Response({'error': 'reference is required'},
                            status=status.HTTP_400_BAD_REQUEST)
        try:
            data = paystack.verify_transaction(reference)
        except paystack.PaystackError as exc:
            logger.error("Paystack verify failed for %s: %s", reference, exc)
            return Response({'error': 'Could not confirm payment right now, please retry'},
                            status=status.HTTP_502_BAD_GATEWAY)

        if data.get('status') != 'success':
            # e.g. "pending" while a MoMo prompt is unanswered, or "failed"/"abandoned"
            return Response({'paid': False, 'paystack_status': data.get('status')})

        try:
            order, _ = apply_paystack_payment(data, user=request.user)
        except PaymentVerificationError as exc:
            return Response({'paid': False, 'error': str(exc)},
                            status=status.HTTP_400_BAD_REQUEST)
        return Response({'paid': True, 'order': OrderSerializer(order).data})


class PaystackWebhookView(APIView):
    """Paystack calls this itself, so payments are recorded even if the customer
    closes the tab. Authenticated by signature, not by JWT."""
    authentication_classes = []
    permission_classes = [AllowAny]

    def post(self, request):
        raw_body = request.body  # read before touching request.data
        if not paystack.valid_signature(raw_body, request.headers.get('x-paystack-signature', '')):
            return Response(status=status.HTTP_400_BAD_REQUEST)
        try:
            payload = json.loads(raw_body)
        except ValueError:
            return Response(status=status.HTTP_400_BAD_REQUEST)

        if payload.get('event') == 'charge.success':
            try:
                apply_paystack_payment(payload.get('data') or {})
            except PaymentVerificationError as exc:
                # 200 anyway: retrying will not fix it. Logged for follow-up.
                logger.warning("Webhook payment not applied: %s", exc)
        return Response(status=status.HTTP_200_OK)


# --------------------------------------------------------------------------- #
# Admin order lifecycle
# --------------------------------------------------------------------------- #
class AdminOrderUpdateView(APIView):
    permission_classes = [IsAdminUser]

    def patch(self, request, order_id):
        order = Order.objects.select_related('user').filter(pk=order_id).first()
        if order is None:
            return Response({"error": "Order not found"}, status=status.HTTP_404_NOT_FOUND)

        new_status = str(request.data.get('status', '')).upper().replace(' ', '_')
        if new_status not in ('IN_TRANSIT', 'DELIVERED'):
            return Response({"error": "status must be IN_TRANSIT or DELIVERED"},
                            status=status.HTTP_400_BAD_REQUEST)
        if not order.is_paid:
            return Response({"error": "This order has not been paid yet"},
                            status=status.HTTP_400_BAD_REQUEST)

        if order.status == 'CANCELLED':
            return Response({"error": "This order was cancelled and cannot be shipped"},
                            status=status.HTTP_400_BAD_REQUEST)


        if new_status == 'IN_TRANSIT':
            rider_name = (request.data.get('rider_name') or '').strip()
            rider_phone = (request.data.get('rider_phone') or '').strip()
            if not rider_name or not rider_phone:
                return Response(
                    {"error": "Rider name and phone number are required when shipping an order"},
                    status=status.HTTP_400_BAD_REQUEST)

            order.rider_name, order.rider_phone, order.status = rider_name, rider_phone, 'IN_TRANSIT'
            order.save()

            Notification.objects.create(
                user=order.user,
                title="Your Hairven Naturals Order is on the way!",
                message=(f"Your package has been handed to our delivery rider, {rider_name}. "
                         f"You can reach them at {rider_phone} to coordinate drop-off."),
            )
            safe_send_mail(
                f"Hairven Naturals: Order #{order.id} is In Transit!",
                (f"Hello {order.user.username},\n\n"
                 f"Great news! Your Hairven Naturals order #{order.id} is on its way.\n\n"
                 f"────── DELIVERY RIDER DETAILS ──────\n"
                 f"Rider Name: {rider_name}\n"
                 f"Contact Phone Number: {rider_phone}\n\n"
                 f"Please keep your phone on so the rider can reach you.\n\n"
                 f"Thank you for shopping with us!\n~ The Hairven Naturals Team"),
                order.user.email,
            )
        else:
            order.status = 'DELIVERED'
            order.save()

            Notification.objects.create(
                user=order.user,
                title="Order Delivered!",
                message=(f"Your order #{order.id} has been marked as delivered. "
                         f"Thank you for shopping with Hairven Naturals!"),
            )
            safe_send_mail(
                f"Hairven Naturals: Order #{order.id} Successfully Delivered!",
                (f"Hello {order.user.username},\n\n"
                 f"Your order #{order.id} from Hairven Naturals has been delivered! "
                 f"We hope you enjoy your new hair products.\n\n"
                 f"If you have any feedback or concerns, reply directly to this email.\n\n"
                 f"Thank you for choosing Hairven Naturals\n~ The Hairven Naturals Team"),
                order.user.email,
            )

        return Response(OrderSerializer(order).data, status=status.HTTP_200_OK)
