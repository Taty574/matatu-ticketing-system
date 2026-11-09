from flask import Flask, request, jsonify

app = Flask(__name__)

# Fake in-memory data for the demo.
# Key = CheckoutRequestID (taken from our sample JSON files).
bookings = {
    "ws_CO_191220191020363925": {"seat": 14, "trip": "Nairobi-Kisumu", "status": "PENDING"},
    "ws_CO_21072024125243250722943992": {"seat": 15, "trip": "Nairobi-Kisumu", "status": "PENDING"},
}
tickets = []  # every ticket we issue is added here


@app.route("/mpesa/callback", methods=["POST"])
def mpesa_callback():
    data = request.get_json(silent=True)
    callback = data["Body"]["stkCallback"]
    checkout_id = callback["CheckoutRequestID"]

    # NAIVE: if a callback arrives, assume the payment worked
    bookings[checkout_id]["status"] = "PAID"
    tickets.append({"checkout_id": checkout_id, "seat": bookings[checkout_id]["seat"]})
    print(f"Ticket issued for seat {bookings[checkout_id]['seat']} ({checkout_id})")
    print(f"Total tickets issued so far: {len(tickets)}")

    return jsonify({"ResultCode": 0, "ResultDesc": "Accepted"})


@app.route("/tickets", methods=["GET"])
def list_tickets():
    return jsonify(tickets)


if __name__ == "__main__":
    app.run(port=5000, debug=True)