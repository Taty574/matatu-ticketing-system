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

## ResultCodes we use
Official (Daraja portal, M-PESA Express Simulate page):
- 0    Success (CallbackMetadata present)
- 1032 Request cancelled by user (no CallbackMetadata)

Secondary sources (developer guides):
- 1    Insufficient balance
- 1037 DS timeout (phone unreachable / no response)
- 2001 Wrong PIN entered

Sources:
- https://developer.safaricom.co.ke/APIs/MpesaExpressSimulate
- https://www.mctaba.com/kb/stk-push-explained
- https://github.com/symo101/daraja-stk-push-docs
Date accessed: 9 November 2026

Note: the 1037 timeout is described as about 60 seconds in one guide. 
Daraja does not retry failed callbacks, so we need the Transaction Status Query as a fallback.

## Person A: Booking and Seats – Monday

### A1: Double-booking race condition

The double-booking problem occurs because the program checks whether a seat is available and inserts the booking in two separate operations. When two passengers attempt to book the same seat simultaneously, both threads can check the database before either booking is inserted, so both believe the seat is available. This race condition allows both bookings to succeed, resulting in two passengers being assigned the same seat on the same trip.

**Evidence:** `Q5/screenshots/A1-double-booking.png`

**Test result:** Both passengers successfully booked seat 14 on trip 1, resulting in two booking records.