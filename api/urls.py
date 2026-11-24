from django.urls import path
from rest_framework_simplejwt.views import TokenObtainPairView, TokenRefreshView
from .views import ProductListView, ComboListView,AdminProductCreateView, CheckoutView, AdminOrderUpdateView, PaymentCaptureView

urlpatterns = [

    # --- TRADITIONAL USER AUTHENTICATION & LOGIN PORTS
    # Traditional Login Endpoint 
    path('auth/login/', TokenObtainPairView.as_view(), name='token_obtain_pair'),

    path('auth/login/refresh/', TokenRefreshView.as_view(), name='token_refresh'),
    #1. Road for anyone to browse individual hair products
    path('products/', ProductListView.as_view(), name='product-list'),

    #2. Road for anyone to see promotional bundle sets
    path('combos/', ComboListView.as_view(), name='combo-list'),

    #3. Secure road for ONLY the admin to create new products
    path('admin/products/create/', AdminProductCreateView.as_view(), name='admin-product-create'),

    #4. Checkout Transaction Road Here
    path('checkout/', CheckoutView.as_view(), name='customer-checkout'),

    #5. Order Life-cycle Admin Update Road
    path('admin/orders/<int:order_id>/update/', AdminOrderUpdateView.as_view(), name='admin-order-update'),

    #6. Payment Capture Road
    path('checkout/payment-capture/', PaymentCaptureView.as_view(), name='payment-capture'),
]
