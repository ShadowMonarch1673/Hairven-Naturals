import re
import secrets

from django.contrib.auth.models import User
from django.contrib.auth.password_validation import validate_password
from django.contrib.auth.validators import UnicodeUsernameValidator
from django.core.exceptions import ValidationError as DjangoValidationError
from rest_framework import serializers
from rest_framework_simplejwt.serializers import TokenObtainPairSerializer

from .models import Product, Combo, Order, OrderItem, Notification, UserProfile


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


# --------------------------------------------------------------------------- #
# Accounts
# --------------------------------------------------------------------------- #
class RegisterSerializer(serializers.Serializer):
    email = serializers.EmailField(max_length=254)
    password = serializers.CharField(write_only=True, trim_whitespace=False, max_length=128)
    username = serializers.CharField(required=False, allow_blank=True, max_length=150)
    first_name = serializers.CharField(required=False, allow_blank=True, max_length=150)
    last_name = serializers.CharField(required=False, allow_blank=True, max_length=150)
    phone = serializers.CharField(required=False, allow_blank=True, max_length=50)

    def validate_email(self, value):
        value = value.strip().lower()
        if User.objects.filter(email__iexact=value).exists():
            raise serializers.ValidationError("An account with this email already exists.")
        return value

    def validate_username(self, value):
        value = value.strip()
        if not value:
            return ''
        if '@' in value:
            # keeps "log in with email" unambiguous
            raise serializers.ValidationError("Username cannot contain '@'.")
        try:
            UnicodeUsernameValidator()(value)
        except DjangoValidationError as exc:
            raise serializers.ValidationError(exc.messages)
        if User.objects.filter(username__iexact=value).exists():
            raise serializers.ValidationError("This username is already taken.")
        return value

    def validate(self, attrs):
        candidate = User(username=attrs.get('username') or '', email=attrs['email'],
                         first_name=attrs.get('first_name', ''), last_name=attrs.get('last_name', ''))
        try:
            validate_password(attrs['password'], user=candidate)
        except DjangoValidationError as exc:
            raise serializers.ValidationError({'password': list(exc.messages)})
        return attrs

    @staticmethod
    def _generate_username(email):
        base = re.sub(r'[^\w.+-]', '', email.split('@')[0])[:130] or 'user'
        candidate = base
        while User.objects.filter(username__iexact=candidate).exists():
            candidate = f"{base}{secrets.randbelow(10000):04d}"
        return candidate

    def create(self, validated_data):
        username = validated_data.get('username') or self._generate_username(validated_data['email'])
        user = User.objects.create_user(
            username=username,
            email=validated_data['email'],
            password=validated_data['password'],
            first_name=validated_data.get('first_name', ''),
            last_name=validated_data.get('last_name', ''),
        )
        UserProfile.objects.create(user=user, phone_number=validated_data.get('phone') or None)
        return user


class MeSerializer(serializers.ModelSerializer):
    phone = serializers.CharField(required=False, allow_blank=True, max_length=50)

    class Meta:
        model = User
        fields = ['id', 'username', 'email', 'first_name', 'last_name', 'phone']
        read_only_fields = ['id', 'username', 'email']  # email changes need verification first

    def to_representation(self, instance):
        data = super().to_representation(instance)
        profile = UserProfile.objects.filter(user=instance).first()
        data['phone'] = (profile.phone_number if profile else '') or ''
        return data

    def update(self, instance, validated_data):
        phone = validated_data.pop('phone', None)
        instance = super().update(instance, validated_data)
        if phone is not None:
            profile, _ = UserProfile.objects.get_or_create(user=instance)
            profile.phone_number = phone or None
            profile.save(update_fields=['phone_number'])
        return instance


class EmailOrUsernameTokenSerializer(TokenObtainPairSerializer):
    """Login with either a username or an email address in the `username` field."""

    def validate(self, attrs):
        identifier = (attrs.get(self.username_field) or '').strip()
        if '@' in identifier:
            matches = list(User.objects.filter(email__iexact=identifier)[:2])
            if len(matches) == 1:
                attrs[self.username_field] = matches[0].get_username()
        return super().validate(attrs)
