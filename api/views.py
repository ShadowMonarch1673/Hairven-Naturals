from rest_framework import generics, status
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import AllowAny, IsAdminUser, IsAuthenticated
from .models import Product, Combo, Order, OrderItem, Notification
from .serializers import ProductSerializer, ComboSerializer, OrderSerializer
from django.core.mail import send_mail
from decimal import Decimal

# Create your views here.
class ProductListView(generics.ListAPIView):
    queryset = Product.objects.filter(stock__gt=0)
    serializer_class = ProductSerializer
    permission_classes = [AllowAny]


class ComboListView(generics.ListAPIView):
    queryset = Combo.objects.filter(is_active=True)
    serializer_class = ComboSerializer
    permission_classes = [AllowAny]


class AdminProductCreateView(generics.CreateAPIView):
    queryset = Product.objects.all()
    serializer_class = ProductSerializer
    permission_classes = [IsAdminUser]


class CheckoutView(APIView):
    permission_classes = [IsAuthenticated]  # Security rule: A client must be logged in to place an order

    def post(self, request):
        user = request.user
        data = request.data #Captures incomingJSON details from the React frontend

        #1. Read layout properties passed from the React client cart layout
        payment_method = data.get('payment_method')   #Expects: 'PAYPAL' or 'MOMO'
        cart_items = data.get('cart_items', []) #Expects a list array of chosen items

        if not cart_items:
            return Response({'error': 'Your shopping cart is empty'}, status=status.HTTP_400_BAD_REQUEST)


        #2. Initialize a baseline Order Header invoice record row inside the database
        order = Order.objects.create(
            user=user,
            total_amount=0.00, #Starts at zero, will calculate the sum dynamically below
            payment_method=payment_method,
            status='PENDING'
        )

        running_total = 0

        #3. Step through every individual item sent from the client shopping cart
        for item in cart_items:
            quantity = item.get('quantity', 1)

            #Check if this item is a single individual product (shampoo, conditioner, oil, etc)
            if 'product_id' in item:
                try:
                    product = Product.objects.get(id=item['product_id'])

                    #INVENTORY SAFETY CHECK: Ensure we actually have enough physical stock
                    if product.stock < quantity:
                        return Response({'error': f"Not  enough stock available for {product.name}"}, status=status.HTTP_400_BAD_REQUEST)

                    #Deducct from inventory stock columns natively
                    product.stock -= quantity
                    product.save()

                    item_price = product.price * quantity
                    running_total += item_price

                    OrderItem.objects.create(
                        order = order,
                        product = product,
                        quantity = quantity,
                        price = product.price
                    )
                except Product.DoesNotExist:
                    return Response({"error": "Product not found"}, status=status.HTTP_404_NOT_FOUND)

            #Check if this item is a promotional set combo bundle
            elif 'combo_id' in item:
                try:
                    combo = Combo.objects.get(id=item['combo_id'])

                    #Log item combo calculation snapshots
                    item_price = combo.discount_price * quantity
                    running_total += item_price

                    OrderItem.objects.create(
                        order = order,
                        combo = combo,
                        quantity = quantity,
                        price = combo.discount_price
                    )
                except Combo.DoesNotExist:
                    return Response({"error": "Promotional set combo bundle not found"}, status=status.HTTP_404_NOT_FOUND)
                


        #4. Finalize calculations: Write down the true combinedorder ledger
        order.total_amount = running_total
        order.save()

        serializer = OrderSerializer(order)
        return Response(serializer.data, status=status.HTTP_201_CREATED)


class AdminOrderUpdateView(APIView):
    permission_classes = [IsAdminUser]

    def patch(self, request, order_id):
        try:
            #1. Look up the specific invoice row by its unique ID serial number
            order = Order.objects.get(id=order_id)

        except Order.DoesNotExist:
            return Response({"error": "Order not found"}, status=status.HTTP_404_NOT_FOUND)

        data = request.data
        new_status = data.get('status')  #Expects: 'IN TRANSIT' or "DELIVERED"

        #2. Handle the 'IN TRANSIT' delivery logic
        if new_status == 'IN TRANSIT':
            rider_name = data.get('rider_name')
            rider_phone = data.get('rider_phone')

            #Ensure admin didn't forget to fill in rider's contact info
            if not rider_name or not rider_phone:
                return Response({"error": "Rider name and phone number are STRICTLY required when shipping an order!"}, status=status.HTTP_400_BAD_REQUEST)


            #Save delivery details straight to database order columns
            order.rider_name = rider_name
            order.rider_phone = rider_phone
            order.status = 'IN_TRANSIT'
            order.save()

            #3. Create In-App Notification Alert Card for client view
            Notification.objects.create(
                user=order.user,
                title="Your HairvenNaturals Order is on the way!",
                message=f"Your package has been handed over to our delivery rider, {rider_name}. You can reach them directly at {rider_phone} to coordinate dropoff locations."
            )

            # Fire BREVO Delivery Confirmation Email
            subject = f"Haiven Naturals: Order #{order.id} is In Transit!"
            email_body = (
                f"Hello {order.user.username}, \n\n"
                f"Great news! Your Hairven Naturals order #{order.id} has been securely packaged and is currently in transit.\n\n"
                f"────── DELIVERY RIDER DETAILS ────── \n"
                f"Rider Name: {rider_name}\n"
                f"Contact Phone Number: {rider_phone}\n\n"
                f"Please keep your phone active so  the rider can contact you directly to coordinate your delivery dropoff point.\n\n"
                f"Total Paid: ₵{order.total_amount} via {order.payment_method}\n\n"
                f"Thank you for shopping with us!\n"
                f"~The Hairven Naturals Team"
            )
            send_mail(
                subject=subject,
                message=email_body,
                from_email=None,
                recipient_list=[order.user.email],
                fail_silently = False
            )
        
        #4. Handle the completion tracking step
        elif new_status == 'DELIVERED':
            order.status = 'DELIVERED'
            order.save()

            Notification.objects.create(
                user = order.user,
                title = "Order Delivered!",
                message = f"Your order #{order.id} has been marked as successfully delivered. Thank you for shopping with Hairven Naturals!"
            )

            # Fire BREVO Delivery Confirmation Email
            subject = f"Hairven Naturals: Order#{order.id} Successfully delivered!"
            email_body = (
                f"Hello {order.user.username}, \n\n"
                f"Your order #{order.id} from Hairven Naturals has been successfully delivered!"
                f"We hope you enjoy your new hair products.\n\n"
                f"────── TRANSACTION SUMMARY ──────\n"
                f"Order ID: #{order.id}\n"
                f"Payment Method:{order.payment_method}\n\n"
                f"If you have aby feedback or concerns regarding your products, feel free to reply directly to this email.\n\n"
                f"Thank you for choosing Hairven Naturals \n"
                f"~The Hairven Naturals Team"
            )
            send_mail(
                subject=subject,
                message=email_body,
                from_email=None,
                recipient_list=[order.user.email],
                fail_silently=False
            )

        else:
            return Response({"error": "Invalid tracking status update choice provided"}, status=status.HTTP_400_BAD_REQUEST)


        serializer = OrderSerializer(order)
        return Response(serializer.data, status=status.HTTP_200_OK)


class PaymentCaptureView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self,request):
        user = request.user
        data = request.data

        # 1. Pull confirmation tokens sent from the frontend script hooks
        order_id = data.get('order_id')
        transaction_id = data.get('transaction_id')  #
        payment_method = data.get('payment_method') # 'PAYPAL' or 'MOMO'

        if not order_id or not transaction_id:
            return Response(
                {"error": "Missing mandatory transaction parameters required to process payment verification"}, 
                status=status.HTTP_400_BAD_REQUEST
            )

        try:
            #2. Look up the specific invoice tracking row matching this user session
            order = Order.objects.get(id=order_id, user=user)

        except Order.DoesNotExist:
            return Response({"error": "Target transaction order not found"}, status=status.HTTP_404_NOT_FOUND)

        #3. Guardrail: If the order has already been cleared, don't execute transaction logic again
        if order.is_paid:
            return Response({"message": "This order ledger has already been flagged as successfully processed."}, status=status.HTTP_200_OK)

        #5. Flip the authorization payment fladgs to locked success states
        order.is_paid = True
        order.payment_method = payment_method
        order.save()

        #6. In-App Notification Confirmation Alert Entry Row
        Notification.objects.create(
            user=user,
            title="Payment Confirmed!",
            message=f"Thank you! Your payment of ₵{order.total_amount} for Order #{order.id} was cleared successfully via {payment_method}. We are preparing your product pacakge for shipping."
        )

        #7. Fire an Instant Transactional Receipt Email via BREVO
        subject = f"Hairven Naturals: Payment Received! Invoice #{order.id}"
        email_body = (
            f"Hello {user.username}, \n\n"
            f"Thank you for your purchase! We have successfully processed your payment for Order #{order.id}.\n\n"
            f"────── TRANSACTION RECEIPT ──────\n"
            f"Order TrackingID: #{order.id}\n"
            f"Total Cleared: ₵{order.total_amount}\n"
            f"Payment Platform Method: {payment_method}\n"
            f"Transaction Authorization Reference ID: {transaction_id}\n\n"
            f"Our warehouse is currently packing your order. The moment it is handed off to a delivery rider, we will send you an updated notication containing their telephone number.\n\n"
            f"Stay beautiful, 😘\n"
            f"~ The Hairven Naturals Team"

        )

        send_mail(
            subject=subject,
            message=email_body,
            from_email=None,
            recipient_list=[user.email],
            fail_silently=False
        )

        serializer = OrderSerializer(order)
        return Response({
            "message": "Your transaction has been processes successfully", 
            "order": serializer.date
            }, status=status.HTTP_200_OK)

    
        
        

        