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


class AccountTests(BaseCase):
    STRONG = 'Str0ng!pass-xyz'

    def setUp(self):
        super().setUp()
        from django.core.cache import cache
        cache.clear()  # throttle counters live in the cache
        self.anon = APIClient()

    def register(self, **over):
        body = {'email': 'New.User@Example.com', 'password': self.STRONG}
        body.update(over)
        return self.anon.post(reverse('register'), body, format='json')

    def test_register_creates_user_profile_and_tokens(self):
        res = self.register(phone='0551234567', first_name='Ama')
        self.assertEqual(res.status_code, 201, res.data)
        self.assertNotIn('password', json.dumps(res.data))
        user = User.objects.get(email='new.user@example.com')  # stored lowercase
        self.assertTrue(user.check_password(self.STRONG))
        self.assertEqual(user.username, 'new.user')  # derived from the email
        self.assertEqual(user.profile.phone_number, '0551234567')
        self.assertEqual(res.data['user']['phone'], '0551234567')

        authed = APIClient()
        authed.credentials(HTTP_AUTHORIZATION=f"Bearer {res.data['access']}")
        self.assertEqual(authed.get(reverse('order-list')).status_code, 200)

    def test_generated_usernames_are_unique(self):
        self.register(email='twin@one.com')
        self.register(email='twin@two.com')
        names = set(User.objects.filter(email__startswith='twin@').values_list('username', flat=True))
        self.assertEqual(len(names), 2)

    def test_explicit_username(self):
        self.assertEqual(self.register(username='ama_k').status_code, 201)
        self.assertTrue(User.objects.filter(username='ama_k').exists())

    def test_rejections(self):
        self.assertEqual(self.register(email='ama@example.com').status_code, 400)   # exists
        self.assertEqual(self.register(email='AMA@EXAMPLE.COM').status_code, 400)   # case-insensitive
        self.assertEqual(self.register(email='not-an-email').status_code, 400)
        self.assertEqual(self.register(password='short').status_code, 400)
        self.assertEqual(self.register(password='12345678').status_code, 400)
        self.assertEqual(self.register(password='password123').status_code, 400)    # too common
        self.assertEqual(self.register(username='ama').status_code, 400)            # taken
        self.assertEqual(self.register(username='AMA').status_code, 400)            # taken, any case
        self.assertEqual(self.register(username='a@b.com').status_code, 400)        # '@' not allowed
        self.assertEqual(self.register(username='has space').status_code, 400)
        self.assertEqual(self.anon.post(reverse('register'), {}, format='json').status_code, 400)
        self.assertEqual(User.objects.filter(email__icontains='example.com').count(), 3)  # only the fixtures

    def login(self, identifier, password='pw12345!'):
        return self.anon.post(reverse('token_obtain_pair'),
                              {'username': identifier, 'password': password}, format='json')

    def test_login_with_username_or_email(self):
        self.assertEqual(self.login('ama').status_code, 200)
        res = self.login('AMA@example.com')
        self.assertEqual(res.status_code, 200)
        self.assertIn('access', res.data)
        self.assertEqual(self.login('ama@example.com', 'wrong').status_code, 401)
        self.assertEqual(self.login('nobody@example.com').status_code, 401)

    def test_register_then_login_with_email(self):
        self.register()
        res = self.login('new.user@example.com', self.STRONG)
        self.assertEqual(res.status_code, 200)

    def test_me_get_and_patch(self):
        self.assertEqual(APIClient().get(reverse('me')).status_code, 401)
        res = self.client.get(reverse('me'))  # fixture user has no profile row yet
        self.assertEqual((res.data['username'], res.data['phone']), ('ama', ''))
        res = self.client.patch(reverse('me'), {'phone': '0244000000', 'first_name': 'Ama',
                                                'email': 'hacker@example.com', 'username': 'x'}, format='json')
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.data['phone'], '0244000000')
        self.user.refresh_from_db()
        self.assertEqual((self.user.first_name, self.user.email, self.user.username),
                         ('Ama', 'ama@example.com', 'ama'))  # email/username untouched

    def test_register_is_rate_limited(self):
        from api.views import RegisterThrottle
        with patch.object(RegisterThrottle, 'rate', '3/hour'):
            for i in range(3):
                self.assertEqual(self.register(email=f'bulk{i}@example.com').status_code, 201)
            self.assertEqual(self.register(email='one-too-many@example.com').status_code, 429)


class NotificationTests(BaseCase):
    def test_user_cannot_mark_someone_elses_notification_as_read(self):
        note = Notification.objects.create(user=self.other, title="Hi", message="Test")
        res = self.client.patch(reverse('notification-read', args=[note.id]), format='json')
        self.assertEqual(res.status_code, 404)
        note.refresh_from_db()
        self.assertEqual(note.is_read, False)

    def test_user_can_mark_own_notification_as_read(self):
        note = Notification.objects.create(user=self.user, title="Hi", message="Test")
        res = self.client.patch(reverse('notification-read', args=[note.id]), format='json')
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.data['is_read'], True)
        note.refresh_from_db()
        self.assertEqual(note.is_read, True)

    def test_missing_notification_returns_404(self):
        res = self.client.patch(reverse('notification-read', args=[9999]), format='json')
        self.assertEqual(res.status_code, 404)

    def test_list_shows_only_own_notifications_newest_first(self):
        first = Notification.objects.create(user=self.user, title="First", message="a")
        second = Notification.objects.create(user=self.user, title="Second", message="b")
        Notification.objects.create(user=self.other, title="Not mine", message="c")
        res = self.client.get(reverse('notification-list'))
        self.assertEqual(res.status_code, 200)
        self.assertEqual([n['id'] for n in res.data], [second.id, first.id])

    def test_login_required(self):
        anon = APIClient()
        self.assertEqual(anon.get(reverse('notification-list')).status_code, 401)
        self.assertEqual(anon.patch(reverse('notification-read', args=[1])).status_code, 401)



class CancelUnpaidOrderTests(BaseCase):
    def test_cancel_restores_product_and_combo_stock(self):
        from api.services import cancel_unpaid_order
        order = self.make_order([{'product_id': self.shampoo.id, 'quantity': 2},
                                 {'combo_id': self.combo.id, 'quantity': 1}])
        self.shampoo.refresh_from_db(); self.oil.refresh_from_db()
        self.assertEqual((self.shampoo.stock, self.oil.stock), (7, 4))  # 10-2-1, 5-1

        self.assertTrue(cancel_unpaid_order(order.id))
        self.shampoo.refresh_from_db(); self.oil.refresh_from_db(); order.refresh_from_db()
        self.assertEqual((self.shampoo.stock, self.oil.stock), (10, 5))
        self.assertEqual(order.status, 'CANCELLED')

    def test_cancelling_twice_does_not_restore_twice(self):
        from api.services import cancel_unpaid_order
        order = self.make_order()
        self.assertTrue(cancel_unpaid_order(order.id))
        self.assertFalse(cancel_unpaid_order(order.id))
        self.shampoo.refresh_from_db()
        self.assertEqual(self.shampoo.stock, 10)

    def test_paid_or_shipped_orders_are_left_alone(self):
        from api.services import cancel_unpaid_order
        paid = self.make_order(); paid.is_paid = True; paid.save()
        shipped = self.make_order(); shipped.status = 'IN_TRANSIT'; shipped.save()
        self.assertFalse(cancel_unpaid_order(paid.id))
        self.assertFalse(cancel_unpaid_order(shipped.id))
        self.assertFalse(cancel_unpaid_order(99999))
        paid.refresh_from_db(); shipped.refresh_from_db()
        self.assertEqual((paid.status, shipped.status), ('PENDING', 'IN_TRANSIT'))


class CancelAbandonedOrdersCommandTests(BaseCase):
    def age(self, order, minutes):
        from datetime import timedelta
        from django.utils import timezone
        Order.objects.filter(pk=order.pk).update(created_at=timezone.now() - timedelta(minutes=minutes))

    def run_command(self, *args):
        from io import StringIO
        from django.core.management import call_command
        out = StringIO()
        call_command('cancel_abandoned_orders', *args, stdout=out)
        return out.getvalue()

    def test_cancels_only_old_unpaid_pending_orders(self):
        old = self.make_order(); self.age(old, 120)
        recent = self.make_order(); self.age(recent, 5)
        old_paid = self.make_order(); self.age(old_paid, 120)
        old_paid.is_paid = True; old_paid.save()

        output = self.run_command()
        self.assertIn('Cancelled 1', output)
        for order in (old, recent, old_paid):
            order.refresh_from_db()
        self.assertEqual((old.status, recent.status, old_paid.status),
                         ('CANCELLED', 'PENDING', 'PENDING'))
        self.shampoo.refresh_from_db()
        self.assertEqual(self.shampoo.stock, 10 - 6 + 2)  # 3 orders of 2 took 6; only `old` came back

    def test_minutes_option_and_dry_run(self):
        order = self.make_order(); self.age(order, 30)
        self.assertIn('Would cancel 1', self.run_command('--minutes', '10', '--dry-run'))
        order.refresh_from_db()
        self.assertEqual(order.status, 'PENDING')  # dry run changed nothing
        self.assertIn('Cancelled 0', self.run_command())  # 30 min is under the 60 default
        self.assertIn('Cancelled 1', self.run_command('--minutes', '10'))


class LatePaymentTests(BaseCase):
    def cancelled_order(self):
        from api.services import cancel_unpaid_order
        order = self.make_order([{'product_id': self.shampoo.id, 'quantity': 2}])
        cancel_unpaid_order(order.id)
        order.refresh_from_db()
        return order

    def pay(self, order):
        with patch('api.paystack.verify_transaction', return_value=self.paystack_data(order)):
            with self.captureOnCommitCallbacks(execute=True):
                return self.client.post(reverse('payment-verify'), {'reference': 'R'}, format='json')

    def test_late_payment_revives_order_when_stock_is_available(self):
        order = self.cancelled_order()
        self.shampoo.refresh_from_db()
        self.assertEqual(self.shampoo.stock, 10)

        res = self.pay(order)
        self.assertTrue(res.data['paid'])
        order.refresh_from_db(); self.shampoo.refresh_from_db()
        self.assertEqual((order.status, order.is_paid), ('PENDING', True))
        self.assertEqual(self.shampoo.stock, 8)  # taken again
        self.assertEqual(Notification.objects.get().title, "Payment Confirmed!")

    def test_late_payment_after_sellout_is_recorded_and_flagged_for_refund(self):
        order = self.cancelled_order()
        Product.objects.filter(pk=self.shampoo.pk).update(stock=1)  # someone else bought it

        with self.assertLogs('api.services', level='ERROR') as logs:
            res = self.pay(order)
        self.assertIn('REFUND NEEDED', logs.output[0])
        self.assertTrue(res.data['paid'])
        order.refresh_from_db(); self.shampoo.refresh_from_db()
        self.assertEqual((order.status, order.is_paid), ('CANCELLED', True))
        self.assertIsNotNone(order.paystack_reference)
        self.assertEqual(self.shampoo.stock, 1)  # untouched, no partial deduction
        self.assertIn('refund', Notification.objects.get().title.lower())
        self.assertIn('refund', mail.outbox[0].subject.lower())

        admin_client = APIClient(); admin_client.force_authenticate(self.admin)
        res = admin_client.patch(reverse('admin-order-update', args=[order.id]),
                                 {'status': 'DELIVERED'}, format='json')
        self.assertEqual(res.status_code, 400)  # a refunded order can never ship

    def test_cannot_start_a_new_payment_on_a_cancelled_order(self):
        order = self.cancelled_order()
        with patch('api.paystack.initialize_transaction') as init:
            res = self.client.post(reverse('payment-initialize'), {'order_id': order.id}, format='json')
        self.assertEqual(res.status_code, 400)
        init.assert_not_called()

class PermissionDefaultTests(BaseCase):
    def test_login_is_the_default_for_every_endpoint(self):
        from rest_framework.settings import api_settings
        self.assertEqual([c.__name__ for c in api_settings.DEFAULT_PERMISSION_CLASSES],
                         ['IsAuthenticated'])

    def test_public_endpoints_stay_open(self):
        anon = APIClient()
        self.assertEqual(anon.get(reverse('product-list')).status_code, 200)
        self.assertEqual(anon.get(reverse('combo-list')).status_code, 200)

    def test_token_refresh_and_login_work_without_logging_in_first(self):
        anon = APIClient()
        login = anon.post(reverse('token_obtain_pair'), {'username': 'ama', 'password': 'pw12345!'}, format='json')
        self.assertEqual(login.status_code, 200)
        refreshed = anon.post(reverse('token_refresh'), {'refresh': login.data['refresh']}, format='json')
        self.assertEqual(refreshed.status_code, 200)

    def test_private_endpoints_refuse_visitors(self):
        anon = APIClient()
        for name, args in (('order-list', []), ('notification-list', []), ('me', []),
                           ('order-detail', [1]), ('notification-read', [1])):
            method = anon.patch if name == 'notification-read' else anon.get
            self.assertEqual(method(reverse(name, args=args)).status_code, 401, name)
        for name in ('customer-checkout', 'payment-initialize', 'payment-verify'):
            self.assertEqual(anon.post(reverse(name), {}, format='json').status_code, 401, name)