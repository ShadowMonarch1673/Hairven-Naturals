from django.urls import path
from rest_framework_simplejwt.views import TokenRefreshView
from .views import (ProductListView, ComboListView, AdminProductCreateView, CheckoutView,
                    AdminOrderUpdateView, OrderListView, OrderDetailView,
                    PaymentInitializeView, PaymentVerifyView, PaystackWebhookView,
                    LoginView, RegisterView, MeView,
                    NotificationListView, NotificationMarkReadView, ProductDetailView,ComboDetailView)

urlpatterns = [
    # Auth
    path('auth/register/', RegisterView.as_view(), name='register'),
    path('auth/login/', LoginView.as_view(), name='token_obtain_pair'),
    path('auth/login/refresh/', TokenRefreshView.as_view(), name='token_refresh'),
    path('auth/me/', MeView.as_view(), name='me'),

    # Catalogue (public)
    path('products/', ProductListView.as_view(), name='product-list'),
    path('combos/', ComboListView.as_view(), name='combo-list'),
    path('products/<int:pk>/', ProductDetailView.as_view(), name='product-detail'),
    path('combos/<int:pk>/', ComboDetailView.as_view(), name='combo-detail'),
    # Customer: checkout and orders
    path('checkout/', CheckoutView.as_view(), name='customer-checkout'),
    path('orders/', OrderListView.as_view(), name='order-list'),
    path('orders/<int:pk>/', OrderDetailView.as_view(), name='order-detail'),

    # Notifications
    path('notifications/', NotificationListView.as_view(), name='notification-list'),
    path('notifications/<int:pk>/read/', NotificationMarkReadView.as_view(), name='notification-read'),

    # Paystack payments
    path('payments/initialize/', PaymentInitializeView.as_view(), name='payment-initialize'),
    path('payments/verify/', PaymentVerifyView.as_view(), name='payment-verify'),
    path('payments/webhook/', PaystackWebhookView.as_view(), name='paystack-webhook'),

    # Admin
    path('admin/products/create/', AdminProductCreateView.as_view(), name='admin-product-create'),
    path('admin/orders/<int:order_id>/update/', AdminOrderUpdateView.as_view(), name='admin-order-update'),
]
