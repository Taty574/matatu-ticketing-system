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


## Person A: Booking and Seats – Tuesday

### Booking Error Table

| Error | Category | What happens / when | How to detect | How to handle |
|---|---|---|---|---|
| Double-booking | Data / Concurrency | Two passengers attempt to book the same seat on the same trip at the same time. | Check for duplicate trip and seat combinations or catch a database integrity error. | Add a UNIQUE constraint on `(trip_id, seat_no)` and catch `sqlite3.IntegrityError` to display "Seat already taken". |
| Seat number out of range | Runtime / Validation | A passenger selects a seat below 1 or above the vehicle's capacity. | Validate the seat number against the vehicle capacity. | Reject the booking and display the valid seat range. |
| Off-by-one seat numbering | Logic | The system incorrectly accepts seat 0 or rejects the last valid seat because of incorrect indexing. | Test the first seat (1), last seat (14), and invalid boundary values. | Use consistent seat numbering from 1 to the vehicle capacity. |
| Missing passenger ID | Runtime / Validation | A booking is submitted with a missing or empty passenger ID. | Check whether the passenger ID is `None` or contains only whitespace. | Reject the booking before inserting it into the database. |
| Incorrect trip date or timezone | Logic / Time | A trip is treated as being in the past or future because of an incorrect date or timezone conversion. | Compare trip dates and timezone-aware timestamps; test date boundaries. | Store timestamps consistently in UTC and convert them to East Africa Time (EAT) for display. |
| Expired seat hold not released | Logic / State | A pending seat hold remains active after five minutes, preventing another passenger from booking it. | Find pending bookings whose `expires_at` time has passed. | Release expired holds before allowing another booking. |
| Incorrect peak or off-peak fare | Logic / Business Rules | The passenger is charged the wrong fare for the departure time or travel period. | Test fare calculations using known peak and off-peak examples. | Centralise fare calculations and apply the approved fare rules. |
| Database unavailable or locked | Runtime / Environment | The booking database cannot be accessed because it is missing, locked, or has incorrect permissions. | Catch `sqlite3.OperationalError` and inspect application logs. | Check the database path and permissions; retry temporary lock errors and show a friendly error message. |
| SQL injection | Security | Untrusted passenger input is joined directly into an SQL statement. | Review database queries and test how they handle malicious input. | Use parameterised SQL queries instead of joining user input into SQL strings. |
| Invalid trip date | Runtime / Validation | A passenger supplies an incorrectly formatted date or selects a trip date in the past. | Validate the date format and compare it with today's date. | Reject invalid or past dates and display a clear validation message. |

### Tuesday Evidence

- `Q5/screenshots/A2-fixed.png` – database constraint prevents double-booking.
- `Q5/screenshots/A3-validation.png` – invalid seat number, passenger ID and trip date are rejected.
- `Q5/screenshots/A4-seat-hold.png` – five-minute pending seat hold expires and the seat becomes available again.