import hashlib
import hmac
import json
from decimal import Decimal
from unittest.mock import patch

from django.conf import settings
from django.contrib.auth.models import User
from django.core import mail
from django.test import TestCase, override_settings
from django.urls import reverse
from rest_framework.test import APIClient

from api import paystack
from api.models import Combo, Notification, Order, Product

LOCMEM = {"default": {"BACKEND": "django.core.mail.backends.locmem.EmailBackend"}}


@override_settings(MAILERS=LOCMEM)
class BaseCase(TestCase):
    def setUp(self):
        self.user = User.objects.create_user('ama', 'ama@example.com', 'pw12345!')
        self.other = User.objects.create_user('kofi', 'kofi@example.com', 'pw12345!')
        self.admin = User.objects.create_superuser('boss', 'boss@example.com', 'pw12345!')
        self.shampoo = Product.objects.create(name='Shampoo', price=Decimal('50.00'), stock=10)
        self.oil = Product.objects.create(name='Oil', price=Decimal('30.00'), stock=5)
        self.combo = Combo.objects.create(name='Starter Set', discount_price=Decimal('70.00'))
        self.combo.products.set([self.shampoo, self.oil])
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    def checkout(self, items, **extra):
        body = {'cart_items': items, 'delivery_address': 'Mercedes St, Accra', 'phone': '0551234567'}
        body.update(extra)
        return self.client.post(reverse('customer-checkout'), body, format='json')

    def make_order(self, items=None):
        res = self.checkout(items or [{'product_id': self.shampoo.id, 'quantity': 2}])
        return Order.objects.get(pk=res.data['id'])

    def paystack_data(self, order, **over):
        data = {'status': 'success', 'currency': 'GHS', 'reference': f'REF-{order.id}',
                'amount': paystack.to_pesewas(order.total_amount), 'channel': 'mobile_money',
                'metadata': {'order_id': order.id}}
        data.update(over)
        return data


class CheckoutTests(BaseCase):
    def test_product_checkout_deducts_stock_and_uses_server_prices(self):
        res = self.checkout([{'product_id': self.shampoo.id, 'quantity': 2, 'price': '0.01'}])
        self.assertEqual(res.status_code, 201)
        self.assertEqual(Decimal(res.data['total_amount']), Decimal('100.00'))
        self.shampoo.refresh_from_db()
        self.assertEqual(self.shampoo.stock, 8)
        self.assertFalse(res.data['is_paid'])

    def test_combo_checkout_deducts_member_stock(self):
        res = self.checkout([{'combo_id': self.combo.id, 'quantity': 2}])
        self.assertEqual(res.status_code, 201)
        self.assertEqual(Decimal(res.data['total_amount']), Decimal('140.00'))
        self.shampoo.refresh_from_db(); self.oil.refresh_from_db()
        self.assertEqual((self.shampoo.stock, self.oil.stock), (8, 3))

    def test_failure_midway_rolls_everything_back(self):
        res = self.checkout([{'product_id': self.shampoo.id, 'quantity': 3},
                             {'product_id': self.oil.id, 'quantity': 99}])
        self.assertEqual(res.status_code, 400)
        self.assertEqual(Order.objects.count(), 0)
        self.shampoo.refresh_from_db()
        self.assertEqual(self.shampoo.stock, 10)

    def test_same_product_twice_cannot_oversell(self):
        res = self.checkout([{'product_id': self.oil.id, 'quantity': 3},
                             {'product_id': self.oil.id, 'quantity': 3}])
        self.assertEqual(res.status_code, 400)
        self.oil.refresh_from_db()
        self.assertEqual(self.oil.stock, 5)

    def test_bad_input_rejected(self):
        for item in ({'product_id': self.shampoo.id, 'quantity': 0},
                     {'product_id': self.shampoo.id, 'quantity': -1},
                     {'product_id': self.shampoo.id, 'quantity': 'x'},
                     {'product_id': 'abc'},
                     {'product_id': self.shampoo.id, 'combo_id': self.combo.id},
                     {}, 'junk'):
            self.assertEqual(self.checkout([item]).status_code, 400, item)
        self.assertEqual(self.checkout([{'product_id': 9999}]).status_code, 404)
        self.assertEqual(self.checkout([]).status_code, 400)
        self.assertEqual(Order.objects.count(), 0)

    def test_requires_address_phone_and_login(self):
        res = self.client.post(reverse('customer-checkout'),
                               {'cart_items': [{'product_id': self.shampoo.id}]}, format='json')
        self.assertEqual(res.status_code, 400)
        self.assertEqual(APIClient().post(reverse('customer-checkout'), {}, format='json').status_code, 401)

    def test_inactive_combo_rejected(self):
        self.combo.is_active = False; self.combo.save()
        self.assertEqual(self.checkout([{'combo_id': self.combo.id}]).status_code, 404)

    def test_users_only_see_their_own_orders(self):
        order = self.make_order()
        self.assertEqual(len(self.client.get(reverse('order-list')).data), 1)
        other = APIClient(); other.force_authenticate(self.other)
        self.assertEqual(len(other.get(reverse('order-list')).data), 0)
        self.assertEqual(other.get(reverse('order-detail', args=[order.id])).status_code, 404)


class PaymentTests(BaseCase):
    @patch('api.paystack.initialize_transaction')
    def test_initialize(self, init):
        init.return_value = {'authorization_url': 'https://checkout.paystack.com/x', 'access_code': 'AC'}
        order = self.make_order()
        res = self.client.post(reverse('payment-initialize'), {'order_id': order.id}, format='json')
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.data['amount'], 10000)
        self.assertEqual(res.data['currency'], 'GHS')
        kwargs = init.call_args.kwargs
        self.assertEqual(kwargs['metadata'], {'order_id': order.id})
        self.assertEqual(kwargs['email'], 'ama@example.com')
        order.refresh_from_db()
        self.assertEqual(order.paystack_reference, res.data['reference'])

    @patch('api.paystack.initialize_transaction')
    def test_initialize_guards(self, init):
        order = self.make_order()
        other = APIClient(); other.force_authenticate(self.other)
        self.assertEqual(other.post(reverse('payment-initialize'), {'order_id': order.id}, format='json').status_code, 404)
        self.assertEqual(self.client.post(reverse('payment-initialize'), {'order_id': 'x'}, format='json').status_code, 404)
        self.user.email = ''; self.user.save()
        self.assertEqual(self.client.post(reverse('payment-initialize'), {'order_id': order.id}, format='json').status_code, 400)
        init.assert_not_called()

    @patch('api.paystack.initialize_transaction', side_effect=paystack.PaystackError('down'))
    def test_initialize_paystack_down(self, _):
        order = self.make_order()
        res = self.client.post(reverse('payment-initialize'), {'order_id': order.id}, format='json')
        self.assertEqual(res.status_code, 502)

    @patch('api.paystack.verify_transaction')
    def test_verify_marks_paid_once(self, verify):
        order = self.make_order()
        verify.return_value = self.paystack_data(order)
        with self.captureOnCommitCallbacks(execute=True):
            res = self.client.post(reverse('payment-verify'), {'reference': 'REF'}, format='json')
        self.assertTrue(res.data['paid'])
        order.refresh_from_db()
        self.assertTrue(order.is_paid)
        self.assertEqual(order.payment_method, 'mobile_money')
        self.assertIsNotNone(order.paid_at)
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(Notification.objects.filter(user=self.user).count(), 1)

        with self.captureOnCommitCallbacks(execute=True):  # same payment again
            self.client.post(reverse('payment-verify'), {'reference': 'REF'}, format='json')
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(Notification.objects.filter(user=self.user).count(), 1)

    @patch('api.paystack.verify_transaction')
    def test_verify_rejections(self, verify):
        order = self.make_order()  # total 100.00
        for over in ({'amount': 100}, {'currency': 'USD'}, {'metadata': {}}):
            verify.return_value = self.paystack_data(order, **over)
            res = self.client.post(reverse('payment-verify'), {'reference': 'R'}, format='json')
            self.assertEqual(res.status_code, 400, over)
        verify.return_value = self.paystack_data(order, status='pending')
        res = self.client.post(reverse('payment-verify'), {'reference': 'R'}, format='json')
        self.assertFalse(res.data['paid'])
        order.refresh_from_db()
        self.assertFalse(order.is_paid)

    @patch('api.paystack.verify_transaction')
    def test_cannot_pay_someone_elses_order(self, verify):
        order = self.make_order()
        verify.return_value = self.paystack_data(order)
        other = APIClient(); other.force_authenticate(self.other)
        self.assertEqual(other.post(reverse('payment-verify'), {'reference': 'R'}, format='json').status_code, 400)
        order.refresh_from_db()
        self.assertFalse(order.is_paid)

    def signed(self, payload, key=None):
        raw = json.dumps(payload).encode()
        sig = hmac.new((key or settings.PAYSTACK_SECRET_KEY).encode(), raw, hashlib.sha512).hexdigest()
        return raw, sig

    def post_webhook(self, raw, sig):
        return APIClient().post(reverse('paystack-webhook'), data=raw,
                                content_type='application/json',
                                HTTP_X_PAYSTACK_SIGNATURE=sig)

    def test_webhook_valid_signature_marks_paid(self):
        order = self.make_order()
        raw, sig = self.signed({'event': 'charge.success', 'data': self.paystack_data(order)})
        with self.captureOnCommitCallbacks(execute=True):
            self.assertEqual(self.post_webhook(raw, sig).status_code, 200)
            self.assertEqual(self.post_webhook(raw, sig).status_code, 200)  # Paystack retries
        order.refresh_from_db()
        self.assertTrue(order.is_paid)
        self.assertEqual(len(mail.outbox), 1)

    def test_webhook_bad_signature_ignored(self):
        order = self.make_order()
        raw, _ = self.signed({'event': 'charge.success', 'data': self.paystack_data(order)})
        for sig in ('', 'nope', self.signed({}, key='sk_other')[1]):
            self.assertEqual(self.post_webhook(raw, sig).status_code, 400)
        order.refresh_from_db()
        self.assertFalse(order.is_paid)

    def test_webhook_wrong_amount_not_applied_but_acknowledged(self):
        order = self.make_order()
        raw, sig = self.signed({'event': 'charge.success', 'data': self.paystack_data(order, amount=1)})
        self.assertEqual(self.post_webhook(raw, sig).status_code, 200)
        order.refresh_from_db()
        self.assertFalse(order.is_paid)

    def test_email_failure_does_not_break_payment(self):
        order = self.make_order()
        raw, sig = self.signed({'event': 'charge.success', 'data': self.paystack_data(order)})
        with patch('api.services.send_mail', side_effect=OSError('smtp down')):
            with self.captureOnCommitCallbacks(execute=True):
                self.assertEqual(self.post_webhook(raw, sig).status_code, 200)
        order.refresh_from_db()
        self.assertTrue(order.is_paid)

    def test_signature_helper_and_pesewas(self):
        self.assertEqual(paystack.to_pesewas(Decimal('19.99')), 1999)
        self.assertEqual(paystack.to_pesewas(Decimal('0.10')), 10)


class AdminOrderUpdateTests(BaseCase):
    def setUp(self):
        super().setUp()
        self.admin_client = APIClient(); self.admin_client.force_authenticate(self.admin)

    def url(self, order):
        return reverse('admin-order-update', args=[order.id])

    def test_unpaid_order_cannot_ship(self):
        order = self.make_order()
        res = self.admin_client.patch(self.url(order), {'status': 'IN_TRANSIT', 'rider_name': 'K', 'rider_phone': '1'}, format='json')
        self.assertEqual(res.status_code, 400)

    def test_ship_then_deliver(self):
        order = self.make_order(); order.is_paid = True; order.save()
        res = self.admin_client.patch(self.url(order), {'status': 'IN TRANSIT'}, format='json')
        self.assertEqual(res.status_code, 400)  # rider details missing
        res = self.admin_client.patch(self.url(order), {'status': 'IN TRANSIT', 'rider_name': 'Kwesi', 'rider_phone': '0244000000'}, format='json')
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.data['status'], 'IN_TRANSIT')
        res = self.admin_client.patch(self.url(order), {'status': 'DELIVERED'}, format='json')
        self.assertEqual(res.data['status'], 'DELIVERED')
        self.assertEqual(len(mail.outbox), 2)
        self.assertEqual(self.admin_client.patch(self.url(order), {'status': 'bogus'}, format='json').status_code, 400)

    def test_customers_cannot_use_admin_endpoint(self):
        order = self.make_order()
        self.assertEqual(self.client.patch(self.url(order), {'status': 'DELIVERED'}, format='json').status_code, 403)

    def test_mail_failure_does_not_500(self):
        order = self.make_order(); order.is_paid = True; order.save()
        with patch('api.services.send_mail', side_effect=OSError('smtp down')):
            res = self.admin_client.patch(self.url(order), {'status': 'DELIVERED'}, format='json')
        self.assertEqual(res.status_code, 200)


class DjangoAdminTests(BaseCase):
    def test_can_create_order_with_products_and_combos_in_admin(self):
        self.client.force_login(self.admin)
        data = {
            'user': self.user.id, 'status': 'PENDING', 'delivery_address': 'x', 'phone': '1',
            'payment_method': '', 'rider_name': '', 'rider_phone': '',
            'items-TOTAL_FORMS': '2', 'items-INITIAL_FORMS': '0',
            'items-MIN_NUM_FORMS': '0', 'items-MAX_NUM_FORMS': '1000',
            'items-0-product': self.shampoo.id, 'items-0-quantity': '2', 'items-0-price': '',
            'items-1-combo': self.combo.id, 'items-1-quantity': '1', 'items-1-price': '',
        }
        res = self.client.post('/admin/api/order/add/', data)
        self.assertEqual(res.status_code, 302, getattr(res, 'context', None) and res.context['errors'] if res.status_code == 200 else '')
        order = Order.objects.get()
        self.assertEqual(order.total_amount, Decimal('170.00'))  # 2 x 50 + 1 x 70
        self.assertEqual(order.items.count(), 2)
        self.assertEqual(self.client.get(f'/admin/api/order/{order.id}/change/').status_code, 200)
        self.assertEqual(self.client.get('/admin/api/combo/').status_code, 200)
