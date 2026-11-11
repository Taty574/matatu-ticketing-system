# Payments and Tracking: Error Table (Person B)

Categories follow the shared Q5 framework: Syntax, Runtime, Logic, Data/Concurrency, Integration/External, Network/Environment, Security.

| # | Error | Category | When it happens in our system | How we detect it | How we handle it |
|---|---|---|---|---|---|
| 1 | Callback arrives twice | Integration | Network retry or a duplicate send, so Wanjiku could get two tickets for one payment | Log shows the same CheckoutRequestID twice; ticket count higher than payments | `processed` set ignores repeats; in production a unique database key on the ID |
| 2 | Callback never arrives | Integration / Network | Our server is down or unreachable when Safaricom sends it | Booking stays PENDING for over 2 minutes | `check_payment_status()` queries Daraja; a daily reconciliation compares our records with M-Pesa statements |
| 3 | Callback arrives after the seat hold expired | Integration / Logic | Wanjiku pays at minute 6 but the hold lasted 5 minutes, so the seat may be sold to someone else | Paid callback for a booking whose status is RELEASED | Re-check seat availability: confirm if still free, otherwise refund and notify the customer |
| 4 | Customer cancels or fails to pay | Integration | ResultCode 1032, 1 (insufficient balance), 1037 (timeout) or 2001 (wrong PIN) | ResultCode is not 0 | Release the seat, show a clear message, offer to retry |
| 5 | Payment succeeded but no ticket issued | Logic / Integration | Server crashes between marking paid and creating the ticket | Booking is PAID with no ticket | Do both steps in one transaction; a periodic job finds paid bookings without tickets and issues them |
| 6 | Expired or invalid access token | Integration | Daraja token has timed out when we send a request | API returns an authorisation error | Request a new token and retry once; alert if it fails again |
| 7 | Callback URL not publicly reachable | Network / Environment | Testing on a laptop, because Safaricom cannot reach 127.0.0.1 | No callbacks arrive during testing | Use a tunnel tool such as ngrok for testing; use a public HTTPS URL in production |
| 8 | GPS data gaps in poor network | Network / Environment | Bus is in a dead zone on a highway, so the owner's map freezes | No ping from a vehicle for N minutes | Show "last seen" time instead of a live dot; the device stores pings and uploads them when it reconnects |
| 9 | Duplicate or out-of-order location pings | Data | Reconnected device sends old pings after new ones, so the bus appears to jump backwards | Timestamp older than the latest stored one | Keep the latest by timestamp and discard stale or duplicate pings |
| 10 | Forged callback or hard-coded keys | Security | Someone posts a fake success callback, or a key leaks through GitHub | Unknown ID, missing fields, or a callback with no matching payment request | Validate fields, reject unknown IDs, confirm uncertain payments via the status query, keep keys in environment variables, rotate leaked keys |

Sources: Daraja portal (ResultCodes 0 and 1032); developer guides (ResultCodes 1, 1037 and 2001, not confirmed on the portal page).