from django import forms
from django.contrib import admin
from .models import Product, Combo, Order, OrderItem, UserProfile, Notification


class ProductAdmin(admin.ModelAdmin):
    list_display = ('id', 'name', 'price', 'stock', 'created_at')
    list_filter = ('created_at',)
    search_fields = ('name', 'description')


class ComboAdmin(admin.ModelAdmin):
    list_display = ('id', 'name', 'discount_price', 'is_active')
    search_fields = ('name',)
    filter_horizontal = ('products',)


class OrderItemForm(forms.ModelForm):
    class Meta:
        model = OrderItem
        fields = '__all__'

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['price'].required = False  # filled from the product/combo if blank

    def clean(self):
        cleaned = super().clean()
        product, combo = cleaned.get('product'), cleaned.get('combo')
        if product and combo:
            raise forms.ValidationError("Choose either a product or a combo, not both.")
        if not product and not combo:
            raise forms.ValidationError("Choose a product or a combo.")
        if cleaned.get('price') is None:
            cleaned['price'] = product.price if product else combo.discount_price
        return cleaned


class OrderItemInline(admin.TabularInline):
    model = OrderItem
    form = OrderItemForm
    extra = 1
    autocomplete_fields = ['product', 'combo']


class OrderAdmin(admin.ModelAdmin):
    list_display = ('id', 'user', 'total_amount', 'status', 'payment_method', 'is_paid', 'created_at')
    list_filter = ('status', 'payment_method', 'is_paid', 'created_at')
    search_fields = ('id', 'user__username', 'user__email', 'rider_name', 'paystack_reference')
    readonly_fields = ('created_at', 'total_amount', 'paystack_reference', 'paid_at')
    autocomplete_fields = ['user']
    inlines = [OrderItemInline]

    def save_model(self, request, obj, form, change):
        if not change:
            obj.total_amount = 0  # real total is computed once the items are saved
        super().save_model(request, obj, form, change)

    def save_related(self, request, form, formsets, change):
        super().save_related(request, form, formsets, change)
        order = form.instance
        order.total_amount = sum(i.price * i.quantity for i in order.items.all())
        order.save(update_fields=['total_amount'])


admin.site.register(Product, ProductAdmin)
admin.site.register(Combo, ComboAdmin)
admin.site.register(Order, OrderAdmin)
admin.site.register(Notification)
admin.site.register(UserProfile)
