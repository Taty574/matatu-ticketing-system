Source: Safaricom Daraja Developer Portal, M-PESA Express Simulate
Endpoint: POST https://sandbox.safaricom.co.ke/mpesa/stkpush/v1/processrequest
Page: https://developer.safaricom.co.ke/APIs/MpesaExpressSimulate
Date accessed: 9 November 2026

Callback structure: Body.stkCallback with MerchantRequestID, CheckoutRequestID,
ResultCode, ResultDesc, and CallbackMetadata.Item (success only).

Confirmed from docs:
- ResultCode 0 = success (CallbackMetadata present)
- ResultCode 1032 = "Request cancelled by user" (no CallbackMetadata)
- Initial API response uses ResponseCode (0 = request accepted), NOT the payment result

