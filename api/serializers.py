from rest_framework import serializers
from django.contrib.auth.models import User
from .models import Product, Combo, Order, OrderItem, Notification


class UserSerializer(serializers.ModelSerializer):
    class Meta:
        model = User
        fields = ['id', 'username', 'email']


class ProductSerializer(serializers.ModelSerializer):
    class Meta:
        model = Product
        fields = ['id', 'name', 'description', 'price', 'stock', 'image_url', 'created_at']


class ComboSerializer(serializers.ModelSerializer):
    product_details = ProductSerializer(source='products', many=True, read_only=True)

    class Meta:
        model = Combo
        fields = ['id', 'name', 'description', 'discount_price', 'products',
                  'product_details', 'is_active', 'image_url']


class OrderItemSerializer(serializers.ModelSerializer):
    product_name = serializers.CharField(source='product.name', read_only=True, default=None)
    combo_name = serializers.CharField(source='combo.name', read_only=True, default=None)

    class Meta:
        model = OrderItem
        fields = ['id', 'product', 'product_name', 'combo', 'combo_name', 'quantity', 'price']
        read_only_fields = fields


class OrderSerializer(serializers.ModelSerializer):
    items = OrderItemSerializer(many=True, read_only=True)

    class Meta:
        model = Order
        fields = ['id', 'user', 'created_at', 'total_amount', 'status', 'delivery_address',
                  'phone', 'payment_method', 'is_paid', 'paid_at', 'rider_name',
                  'rider_phone', 'items']
        read_only_fields = fields


class NotificationSerializer(serializers.ModelSerializer):
    class Meta:
        model = Notification
        fields = ['id', 'title', 'message', 'is_read', 'created_at']
        read_only_fields = fields
