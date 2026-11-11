import os
import logging
from flask import Flask, request, jsonify

app = Flask(__name__)
logging.basicConfig(level=logging.INFO)

# Secrets come from environment variables, never from the code
CONSUMER_KEY = os.environ.get("DARAJA_CONSUMER_KEY")
CONSUMER_SECRET = os.environ.get("DARAJA_CONSUMER_SECRET")

# Demo data (a real system would use a database)
bookings = {
    "ws_CO_191220191020363925": {"seat": 14, "trip": "Nairobi-Kisumu", "status": "PENDING"},
    "ws_CO_21072024125243250722943992": {"seat": 15, "trip": "Nairobi-Kisumu", "status": "PENDING"},
}
tickets = []
processed = set()  # CheckoutRequestIDs we have already handled

FAILURE_MESSAGES = {
    1: "Insufficient M-Pesa balance",
    1032: "Payment cancelled by customer",
    1037: "Phone unreachable, request timed out",
    2001: "Wrong PIN entered",
}


def ack():
    return jsonify({"ResultCode": 0, "ResultDesc": "Accepted"})


def parse_callback(data):
    """Return (checkout_id, result_code, result_desc) or None if malformed."""
    try:
        cb = data["Body"]["stkCallback"]
        return cb["CheckoutRequestID"], int(cb["ResultCode"]), cb.get("ResultDesc", "")
    except (KeyError, TypeError, ValueError):
        return None


@app.route("/mpesa/callback", methods=["POST"])
def mpesa_callback():
    parsed = parse_callback(request.get_json(silent=True))
    if parsed is None:
        app.logger.error("Malformed callback rejected: %s", request.get_data(as_text=True))
        return jsonify({"error": "Malformed callback"}), 400

    checkout_id, result_code, result_desc = parsed
    booking = bookings.get(checkout_id)
    if booking is None:
        app.logger.warning("Unknown CheckoutRequestID: %s", checkout_id)
        return jsonify({"error": "Unknown transaction"}), 404

    if checkout_id in processed:  # FIX 1: ignore repeats (idempotency)
        app.logger.info("Duplicate callback ignored: %s", checkout_id)
        return ack()
    processed.add(checkout_id)

    if result_code == 0:  # FIX 2: only a ResultCode of 0 means paid
        booking["status"] = "PAID"
        tickets.append({"checkout_id": checkout_id, "seat": booking["seat"]})
        print(f"Ticket issued for seat {booking['seat']} ({checkout_id})")
    else:
        booking["status"] = "RELEASED"
        reason = FAILURE_MESSAGES.get(result_code, result_desc)
        print(f"Payment failed ({result_code}): {reason}. Seat {booking['seat']} released.")
    return ack()


def check_payment_status(checkout_id):
    """Fallback for lost callbacks (stub). Called if no callback arrives within 2 minutes.
    The real version sends Daraja's M-Pesa Express Query request with the shortcode,
    password, timestamp and CheckoutRequestID, using a token from CONSUMER_KEY/SECRET."""
    print(f"No callback for {checkout_id}. Asking Daraja for the payment status...")


@app.route("/tickets", methods=["GET"])
def list_tickets():
    return jsonify(tickets)


if __name__ == "__main__":
    app.run(port=5000, debug=True)