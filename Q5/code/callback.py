from flask import Flask, request, jsonify

app = Flask(__name__)

@app.route("/mpesa/callback", methods=["POST"])
def mpesa_callback():
    data = request.get_json(silent=True)
    print("Callback received:")
    print(data)
    # Daraja expects a quick 200 response acknowledging receipt
    return jsonify({"ResultCode": 0, "ResultDesc": "Accepted"})

if __name__ == "__main__":
    app.run(port=5000, debug=True)