"""Business logic kept out of the views so it can be reused and tested."""
import json
import logging
from decimal import Decimal

from django.core.mail import send_mail
from django.db import transaction
from django.utils import timezone

from . import paystack
from .models import Combo, Notification, Order, OrderItem, Product

logger = logging.getLogger(__name__)

MAX_QUANTITY_PER_LINE = 100


class CheckoutError(Exception):
    def __init__(self, message, status_code=400):
        super().__init__(message)
        self.message = message
        self.status_code = status_code


class PaymentVerificationError(Exception):
    pass


# --------------------------------------------------------------------------- #
# Email
# --------------------------------------------------------------------------- #
def safe_send_mail(subject, body, recipient):
    """Send an email without ever letting a mail failure break the request."""
    if not recipient:
        logger.warning("Skipped email %r: user has no email address", subject)
        return False
    try:
        send_mail(subject=subject, message=body, from_email=None,
                  recipient_list=[recipient], fail_silently=False)
        return True
    except Exception:
        logger.exception("Failed to send email %r to %s", subject, recipient)
        return False


# --------------------------------------------------------------------------- #
# Checkout
# --------------------------------------------------------------------------- #
def _to_int(value, label):
    if isinstance(value, bool):
        raise CheckoutError(f"Invalid {label}")
    try:
        return int(value)
    except (TypeError, ValueError):
        raise CheckoutError(f"Invalid {label}")


def _take_stock(product, quantity):
    if product.stock < quantity:
        raise CheckoutError(f"Not enough stock available for {product.name}")
    product.stock -= quantity
    product.save(update_fields=['stock'])


@transaction.atomic
def create_order_from_cart(user, cart_items, delivery_address, phone):
    """Validate a cart, deduct stock and create the order, all or nothing.

    Prices always come from the database, never from the client. Any
    CheckoutError rolls back the order and every stock change.
    """
    if not isinstance(cart_items, list) or not cart_items:
        raise CheckoutError("Your shopping cart is empty")

    order = Order.objects.create(
        user=user,
        total_amount=Decimal('0.00'),
        delivery_address=delivery_address,
        phone=phone,
    )
    total = Decimal('0.00')

    for raw in cart_items:
        if not isinstance(raw, dict):
            raise CheckoutError("Invalid cart item")

        quantity = _to_int(raw.get('quantity', 1), "quantity")
        if quantity < 1 or quantity > MAX_QUANTITY_PER_LINE:
            raise CheckoutError(f"Quantity must be between 1 and {MAX_QUANTITY_PER_LINE}")

        product_id, combo_id = raw.get('product_id'), raw.get('combo_id')
        if (product_id is None) == (combo_id is None):
            raise CheckoutError("Each cart item needs exactly one of product_id or combo_id")

        if product_id is not None:
            product = Product.objects.select_for_update().filter(
                pk=_to_int(product_id, "product_id")).first()
            if product is None:
                raise CheckoutError("Product not found", 404)
            _take_stock(product, quantity)
            OrderItem.objects.create(order=order, product=product,
                                     quantity=quantity, price=product.price)
            total += product.price * quantity
        else:
            combo = Combo.objects.filter(
                pk=_to_int(combo_id, "combo_id"), is_active=True).first()
            if combo is None:
                raise CheckoutError("Promotional set combo bundle not found", 404)
            members = list(Product.objects.select_for_update()
                           .filter(combos=combo).order_by('pk'))
            if not members:
                raise CheckoutError(f"{combo.name} has no products and cannot be ordered")
            for member in members:  # one unit of every member per combo ordered
                _take_stock(member, quantity)
            OrderItem.objects.create(order=order, combo=combo,
                                     quantity=quantity, price=combo.discount_price)
            total += combo.discount_price * quantity

    order.total_amount = total
    order.save(update_fields=['total_amount'])
    return order


# --------------------------------------------------------------------------- #
# Payment
# --------------------------------------------------------------------------- #
def _metadata_dict(data):
    metadata = data.get('metadata')
    if isinstance(metadata, str):
        try:
            metadata = json.loads(metadata)
        except ValueError:
            metadata = {}
    return metadata if isinstance(metadata, dict) else {}


def send_payment_receipt(order, reference):
    safe_send_mail(
        subject=f"Hairven Naturals: Payment Received! Invoice #{order.id}",
        body=(
            f"Hello {order.user.username},\n\n"
            f"Thank you for your purchase! We have successfully processed your payment for Order #{order.id}.\n\n"
            f"────── TRANSACTION RECEIPT ──────\n"
            f"Order ID: #{order.id}\n"
            f"Total Paid: ₵{order.total_amount}\n"
            f"Payment Method: {order.payment_method or 'Paystack'}\n"
            f"Payment Reference: {reference}\n\n"
            f"We are packing your order now. As soon as it is handed to a delivery rider, "
            f"we will send you the rider's contact details.\n\n"
            f"Stay beautiful,\n~ The Hairven Naturals Team"
        ),
        recipient=order.user.email,
    )


def apply_paystack_payment(data, user=None):
    """Mark an order paid from a Paystack transaction object.

    `data` comes from Paystack's verify endpoint or a signed webhook, never
    from the browser. Safe to call repeatedly: the order row is locked and an
    already-paid order is left untouched. Returns (order, newly_paid).
    """
    if data.get('status') != 'success':
        raise PaymentVerificationError("Payment was not successful")
    if data.get('currency') != paystack.CURRENCY:
        raise PaymentVerificationError("Unexpected payment currency")

    order_id = _metadata_dict(data).get('order_id')
    reference = data.get('reference')
    if not order_id or not reference:
        raise PaymentVerificationError("Transaction is not linked to an order")

    with transaction.atomic():
        order = (Order.objects.select_for_update().select_related('user')
                 .filter(pk=order_id).first())
        if order is None:
            raise PaymentVerificationError("Order not found")
        if user is not None and order.user_id != user.id:
            raise PaymentVerificationError("Order not found")

        if order.is_paid:
            if order.paystack_reference != reference:
                logger.warning(
                    "Order %s already paid with %s but Paystack also reports a "
                    "successful payment %s: check the dashboard for a refund",
                    order.id, order.paystack_reference, reference)
            return order, False

        if data.get('amount') != paystack.to_pesewas(order.total_amount):
            logger.warning("Amount mismatch on order %s: paid %s pesewas, expected %s",
                           order.id, data.get('amount'), paystack.to_pesewas(order.total_amount))
            raise PaymentVerificationError("Amount paid does not match the order total")

        order.is_paid = True
        order.paid_at = timezone.now()
        order.paystack_reference = reference
        order.payment_method = (data.get('channel') or '')[:30]
        order.save(update_fields=['is_paid', 'paid_at', 'paystack_reference', 'payment_method'])

        Notification.objects.create(
            user=order.user,
            title="Payment Confirmed!",
            message=(f"Thank you! Your payment of ₵{order.total_amount} for Order #{order.id} "
                     f"was received. We are preparing your package for shipping."),
        )
        transaction.on_commit(lambda: send_payment_receipt(order, reference))

    return order, True
