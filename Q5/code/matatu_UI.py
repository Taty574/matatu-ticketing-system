#!/usr/bin/env python3
"""
Matatu Ticketing System - web user interface (Flask + SQLite).

Run:   pip install flask qrcode
       python matatu_app.py
Open:  http://127.0.0.1:5001

Built from the project's requirements (Q3 docs) and rules (Q5 booking.py,
callback.py, error tables). It does NOT modify any of your existing files.
Demo accounts (password for all: Demo@1234):
    wanjiku (passenger)  conductor1 (crew)  admin  owner1 (vehicle owner)  auditor
"""
import base64, csv, hashlib, hmac, io, json, os, re, secrets, sqlite3, time, zipfile
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from functools import wraps

from flask import (Flask, Response, abort, flash, g, get_flashed_messages, has_request_context,
                   jsonify, redirect, render_template, request, session, url_for)
from jinja2 import DictLoader
from markupsafe import Markup
from werkzeug.security import check_password_hash, generate_password_hash

try:                                   # optional: draws the ticket QR code
    import qrcode
    import qrcode.image.svg
except ImportError:                    # the app still works; the code is shown as text
    qrcode = None

# ----------------------------------------------------------------------------
# Configuration
# ----------------------------------------------------------------------------
HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("MATATU_DB", os.path.join(HERE, "matatu_ui.db"))
KEY_PATH = os.path.join(HERE, "matatu_ui.key")
DEMO_MODE = os.environ.get("MATATU_DEMO", "1") == "1"   # shows demo logins + M-Pesa simulator
FMT = "%Y-%m-%dT%H:%M:%S"                               # all times stored in UTC
EAT = timezone(timedelta(hours=3))                      # East Africa Time (display)
HOLD_MINUTES = 5                                        # same as booking.py
DEFAULT_CAPACITY = 14                                   # same as booking.py
IDLE_SECONDS = 15 * 60                                  # NFR-001: sessions expire when idle
PEAK_WINDOWS = [(6, 9), (16, 20)]                       # NFR-007 peak hours (EAT)
STALE_MINUTES = 5                                       # GPS "last seen" threshold
PENDING_QUERY_MINUTES = 2                               # callback.py: query status after 2 minutes

app = Flask(__name__)
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax")


def _load_master_key():
    env = os.environ.get("MATATU_MASTER_KEY")
    if env:
        return env.encode()
    if not os.path.exists(KEY_PATH):
        with open(KEY_PATH, "wb") as f:
            f.write(secrets.token_bytes(32))
        try:
            os.chmod(KEY_PATH, 0o600)
        except OSError:
            pass
    with open(KEY_PATH, "rb") as f:
        return f.read()


MASTER = _load_master_key()
def _derive(label): return hmac.new(MASTER, label.encode(), hashlib.sha256).digest()
PHONE_KEY, TICKET_KEY = _derive("phone"), _derive("ticket")
app.secret_key = _derive("flask-session")

# ----------------------------------------------------------------------------
# Small helpers: time, money, phone numbers, encryption
# ----------------------------------------------------------------------------
def now(): return datetime.now(timezone.utc).replace(microsecond=0)
def ts(d): return d.astimezone(timezone.utc).strftime(FMT)
def pts(s): return datetime.strptime(s, FMT).replace(tzinfo=timezone.utc)
def eat_date(d=None): return (d or now()).astimezone(EAT).strftime("%Y-%m-%d")


def day_range_utc(day):
    start = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=EAT)
    return ts(start), ts(start + timedelta(days=1))


@app.template_filter("eat")
def f_eat(s, fmt="%a %d %b, %H:%M"):
    return pts(s).astimezone(EAT).strftime(fmt) if s else ""


@app.template_filter("kes")
def f_kes(x): return "KES {:,}".format(int(x or 0))


def _keystream(key, nonce, n):
    out, c = b"", 0
    while len(out) < n:
        out += hmac.new(key, nonce + c.to_bytes(4, "big"), hashlib.sha256).digest()
        c += 1
    return out[:n]


def enc(text):
    """Encrypt (stream cipher + HMAC tag). Demo-grade: use a vetted library / KMS in production."""
    if not text:
        return None
    nonce, data = os.urandom(12), text.encode()
    ct = bytes(a ^ b for a, b in zip(data, _keystream(PHONE_KEY, nonce, len(data))))
    tag = hmac.new(PHONE_KEY, b"t" + nonce + ct, hashlib.sha256).digest()[:16]
    return base64.urlsafe_b64encode(nonce + tag + ct).decode()


def dec(token):
    if not token:
        return None
    raw = base64.urlsafe_b64decode(token.encode())
    nonce, tag, ct = raw[:12], raw[12:28], raw[28:]
    if not hmac.compare_digest(tag, hmac.new(PHONE_KEY, b"t" + nonce + ct, hashlib.sha256).digest()[:16]):
        return None
    return bytes(a ^ b for a, b in zip(ct, _keystream(PHONE_KEY, nonce, len(ct)))).decode()


def normalize_phone(raw):
    p = re.sub(r"[\s\-+]", "", raw or "")
    if re.fullmatch(r"0[17]\d{8}", p): return "254" + p[1:]
    if re.fullmatch(r"254[17]\d{8}", p): return p
    if re.fullmatch(r"[17]\d{8}", p): return "254" + p
    return None


def mask_phone(token):
    """NFR-002: crew-facing screens only ever show 07XX XXX 123."""
    m = dec(token)
    if not m:
        return "-"
    local = "0" + m[3:]
    return f"{local[:2]}XX XXX {local[-3:]}"


def pct_of(x, p):
    s = -1 if x < 0 else 1
    return s * (abs(x) * p // 100)


# ----------------------------------------------------------------------------
# Translations (NFR-011: English + Kiswahili). Please have a Kiswahili speaker review.
# ----------------------------------------------------------------------------
T = {
"en": {
 "app_name": "Matatu Tickets", "nav_home": "Book a trip", "nav_tickets": "My tickets", "nav_account": "Account",
 "nav_login": "Log in", "nav_register": "Register", "nav_logout": "Log out", "nav_crew": "Crew desk",
 "search": "Search", "route": "Route", "date": "Date", "all_routes": "All routes", "departs": "Departs",
 "fare": "Fare", "seats_left": "seats left", "peak": "Peak fare", "offpeak": "Standard fare",
 "select_seat": "Choose your seat", "hold_seat": "Hold seat for 5 minutes", "driver": "Driver",
 "legend_free": "Free", "legend_taken": "Taken", "legend_held": "On hold", "legend_mine": "Yours",
 "no_trips": "No trips found for this date. Try another day or route.", "login_to_book": "Log in to book",
 "pay_title": "Pay with M-Pesa", "phone": "M-Pesa phone number", "pay_btn": "Send M-Pesa prompt",
 "time_left": "Seat held for", "stk_sent": "Check your phone and enter your M-Pesa PIN.",
 "waiting": "Waiting for payment confirmation...", "paid_ok": "Payment received. Your ticket is ready.",
 "show_ticket": "Show ticket", "my_tickets": "My tickets", "no_tickets": "You have no tickets yet.",
 "ticket_code": "Ticket code", "valid_window": "Valid", "ticket_note": "Show this QR code to the conductor. It works once.",
 "status": "Status", "seat": "Seat", "vehicle": "Vehicle", "login": "Log in", "username": "Username",
 "password": "Password", "register": "Create account", "full_name": "Full name", "try_again": "Try again",
 "welcome": "Welcome", "logged_out": "You have been logged out.", "session_expired": "Your session expired. Please log in again.",
 "bad_login": "Wrong username or password.", "locked": "Too many attempts. Wait 5 minutes and try again.",
 "seat_taken": "That seat is already taken. Please choose another seat.",
 "seat_range": "Invalid seat number. Choose between 1 and {n}.",
 "trip_past": "This trip has already left. Choose a later trip.",
 "trip_unavailable": "This trip is not available.",
 "hold_expired": "Your seat hold expired. Please choose a seat again.",
 "bad_phone": "Enter a valid Safaricom number, for example 0712 345 678.",
 "too_many_holds": "You already hold 4 seats. Pay for them or wait for the hold to expire.",
 "pay_1": "Your M-Pesa balance is too low. Top up and try again.",
 "pay_1032": "You cancelled the payment. You can try again.",
 "pay_1037": "Your phone could not be reached. Try again.",
 "pay_2001": "Wrong M-Pesa PIN. Try again.",
 "pay_other": "The payment did not go through. Try again.",
 "refund_due": "Your payment arrived after the hold expired and the seat was sold. You will be refunded.",
 "validate": "Check ticket", "validate_help": "Scan or paste the ticket code, then tap Check.",
 "cash_sale": "Cash ticket", "sell": "Sell ticket", "manifest": "Passengers on this trip",
 "share_location": "Share my location", "no_vehicle": "No vehicle is assigned to you. Ask your SACCO admin.",
 "ok_valid": "VALID - let the passenger board", "v_forged": "REJECTED - fake or edited ticket",
 "v_wrong_vehicle": "REJECTED - this ticket is for another vehicle", "v_expired": "REJECTED - ticket expired",
 "v_early": "REJECTED - too early for this trip", "v_used": "REJECTED - ticket already used",
 "v_cancelled": "REJECTED - ticket was cancelled/refunded", "v_unknown": "REJECTED - ticket not found",
 "v_malformed": "REJECTED - not a ticket code", "cash_ok": "Cash ticket issued for seat {n}.",
 "gps_ok": "Location sent.", "gps_old": "Old location ignored.",
},
"sw": {
 "app_name": "Tiketi za Matatu", "nav_home": "Kata tiketi", "nav_tickets": "Tiketi zangu", "nav_account": "Akaunti",
 "nav_login": "Ingia", "nav_register": "Jisajili", "nav_logout": "Toka", "nav_crew": "Dawati la wafanyakazi",
 "search": "Tafuta", "route": "Njia", "date": "Tarehe", "all_routes": "Njia zote", "departs": "Kuondoka",
 "fare": "Nauli", "seats_left": "viti vimebaki", "peak": "Nauli ya kilele", "offpeak": "Nauli ya kawaida",
 "select_seat": "Chagua kiti chako", "hold_seat": "Shikilia kiti kwa dakika 5", "driver": "Dereva",
 "legend_free": "Wazi", "legend_taken": "Kimechukuliwa", "legend_held": "Kimeshikiliwa", "legend_mine": "Chako",
 "no_trips": "Hakuna safari siku hii. Jaribu siku au njia nyingine.", "login_to_book": "Ingia ili kukata tiketi",
 "pay_title": "Lipa kwa M-Pesa", "phone": "Namba ya simu ya M-Pesa", "pay_btn": "Tuma ombi la M-Pesa",
 "time_left": "Kiti kimeshikiliwa kwa", "stk_sent": "Angalia simu yako na uweke PIN ya M-Pesa.",
 "waiting": "Tunasubiri uthibitisho wa malipo...", "paid_ok": "Malipo yamepokelewa. Tiketi yako iko tayari.",
 "show_ticket": "Onyesha tiketi", "my_tickets": "Tiketi zangu", "no_tickets": "Huna tiketi bado.",
 "ticket_code": "Msimbo wa tiketi", "valid_window": "Halali", "ticket_note": "Mwonyeshe kondakta msimbo huu wa QR. Hutumika mara moja.",
 "status": "Hali", "seat": "Kiti", "vehicle": "Gari", "login": "Ingia", "username": "Jina la mtumiaji",
 "password": "Nenosiri", "register": "Fungua akaunti", "full_name": "Jina kamili", "try_again": "Jaribu tena",
 "welcome": "Karibu", "logged_out": "Umetoka.", "session_expired": "Muda wako umeisha. Tafadhali ingia tena.",
 "bad_login": "Jina la mtumiaji au nenosiri si sahihi.", "locked": "Majaribio mengi mno. Subiri dakika 5.",
 "seat_taken": "Kiti hicho kimeshachukuliwa. Tafadhali chagua kingine.",
 "seat_range": "Namba ya kiti si sahihi. Chagua kati ya 1 na {n}.",
 "trip_past": "Safari hii imeshaondoka. Chagua safari ya baadaye.",
 "trip_unavailable": "Safari hii haipatikani.",
 "hold_expired": "Muda wa kushikilia kiti umeisha. Chagua kiti tena.",
 "bad_phone": "Weka namba sahihi ya Safaricom, mfano 0712 345 678.",
 "too_many_holds": "Tayari umeshikilia viti 4. Lipia au subiri muda uishe.",
 "pay_1": "Salio lako la M-Pesa halitoshi. Weka pesa na ujaribu tena.",
 "pay_1032": "Umeghairi malipo. Unaweza kujaribu tena.",
 "pay_1037": "Simu yako haikupatikana. Jaribu tena.",
 "pay_2001": "PIN ya M-Pesa si sahihi. Jaribu tena.",
 "pay_other": "Malipo hayakukamilika. Jaribu tena.",
 "refund_due": "Malipo yako yalifika baada ya muda wa kushikilia kuisha na kiti kikauzwa. Utarejeshewa pesa.",
 "validate": "Hakiki tiketi", "validate_help": "Changanua au bandika msimbo wa tiketi, kisha bonyeza Hakiki.",
 "cash_sale": "Tiketi ya pesa taslimu", "sell": "Uza tiketi", "manifest": "Abiria wa safari hii",
 "share_location": "Tuma mahali nilipo", "no_vehicle": "Hujapewa gari. Muulize msimamizi wa SACCO.",
 "ok_valid": "HALALI - ruhusu abiria apande", "v_forged": "IMEKATALIWA - tiketi ya bandia au iliyobadilishwa",
 "v_wrong_vehicle": "IMEKATALIWA - tiketi hii ni ya gari lingine", "v_expired": "IMEKATALIWA - muda wa tiketi umeisha",
 "v_early": "IMEKATALIWA - bado mapema kwa safari hii", "v_used": "IMEKATALIWA - tiketi imeshatumika",
 "v_cancelled": "IMEKATALIWA - tiketi ilighairiwa/imerejeshewa", "v_unknown": "IMEKATALIWA - tiketi haipatikani",
 "v_malformed": "IMEKATALIWA - huu si msimbo wa tiketi", "cash_ok": "Tiketi ya pesa taslimu imetolewa kwa kiti {n}.",
 "gps_ok": "Mahali pametumwa.", "gps_old": "Mahali pa zamani pamepuuzwa.",
}}


def t(key, **kw):
    lang = session.get("lang", "en") if has_request_context() else "en"
    s = T.get(lang, {}).get(key) or T["en"].get(key, key)
    return s.format(**kw) if kw else s


# ----------------------------------------------------------------------------
# Database
# ----------------------------------------------------------------------------
SCHEMA = """
CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT UNIQUE NOT NULL,
  pw_hash TEXT NOT NULL, role TEXT NOT NULL CHECK(role IN ('passenger','conductor','admin','owner','auditor')),
  full_name TEXT NOT NULL, phone_enc TEXT, vehicle_id INTEGER, active INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS routes(id INTEGER PRIMARY KEY, name TEXT NOT NULL, short_name TEXT NOT NULL, color TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS stops(id INTEGER PRIMARY KEY AUTOINCREMENT, route_id INTEGER NOT NULL, seq INTEGER NOT NULL,
  name TEXT NOT NULL, lat REAL NOT NULL, lon REAL NOT NULL);
CREATE TABLE IF NOT EXISTS vehicles(id INTEGER PRIMARY KEY AUTOINCREMENT, plate TEXT UNIQUE NOT NULL,
  capacity INTEGER NOT NULL DEFAULT 14 CHECK(capacity BETWEEN 1 AND 60), owner_id INTEGER NOT NULL,
  route_id INTEGER NOT NULL, active INTEGER NOT NULL DEFAULT 1);
CREATE TABLE IF NOT EXISTS fares(route_id INTEGER PRIMARY KEY, off_peak INTEGER NOT NULL, peak INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS trips(id INTEGER PRIMARY KEY AUTOINCREMENT, route_id INTEGER NOT NULL, vehicle_id INTEGER NOT NULL,
  depart_at TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'SCHEDULED', UNIQUE(vehicle_id, depart_at));
CREATE TABLE IF NOT EXISTS bookings(id INTEGER PRIMARY KEY AUTOINCREMENT, trip_id INTEGER NOT NULL, seat_no INTEGER NOT NULL,
  passenger_id INTEGER NOT NULL, status TEXT NOT NULL, fare INTEGER NOT NULL, created_at TEXT NOT NULL, expires_at TEXT,
  UNIQUE(trip_id, seat_no));
CREATE TABLE IF NOT EXISTS payments(id INTEGER PRIMARY KEY AUTOINCREMENT, checkout_id TEXT UNIQUE NOT NULL, trip_id INTEGER NOT NULL,
  seat_no INTEGER NOT NULL, passenger_id INTEGER NOT NULL, amount INTEGER NOT NULL, method TEXT NOT NULL, phone_enc TEXT,
  status TEXT NOT NULL, result_code INTEGER, result_desc TEXT, receipt TEXT, hidden_result INTEGER,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS tickets(id INTEGER PRIMARY KEY AUTOINCREMENT, payment_id INTEGER UNIQUE NOT NULL, trip_id INTEGER NOT NULL,
  seat_no INTEGER NOT NULL, passenger_id INTEGER NOT NULL, code TEXT UNIQUE NOT NULL, valid_from TEXT NOT NULL,
  valid_to TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'VALID', used_at TEXT, used_by INTEGER);
CREATE TABLE IF NOT EXISTS validations(id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT NOT NULL, ticket_payment_id INTEGER,
  result TEXT NOT NULL, reason TEXT, conductor_id INTEGER, vehicle_id INTEGER);
CREATE TABLE IF NOT EXISTS ledger(id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT NOT NULL, vehicle_id INTEGER NOT NULL,
  kind TEXT NOT NULL, amount INTEGER NOT NULL, payment_id INTEGER, note TEXT);
CREATE TABLE IF NOT EXISTS settlements(id INTEGER PRIMARY KEY AUTOINCREMENT, vehicle_id INTEGER NOT NULL, day TEXT NOT NULL,
  collected INTEGER NOT NULL, owner_amt INTEGER NOT NULL, sacco_amt INTEGER NOT NULL, crew_amt INTEGER NOT NULL,
  created_at TEXT NOT NULL, created_by INTEGER, UNIQUE(vehicle_id, day));
CREATE TABLE IF NOT EXISTS pings(id INTEGER PRIMARY KEY AUTOINCREMENT, vehicle_id INTEGER NOT NULL, lat REAL NOT NULL,
  lon REAL NOT NULL, ts TEXT NOT NULL, received_at TEXT NOT NULL, UNIQUE(vehicle_id, ts));
CREATE TABLE IF NOT EXISTS audit_log(id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT NOT NULL, user_id INTEGER, username TEXT,
  action TEXT NOT NULL, detail TEXT, device TEXT);
"""
APPEND_ONLY = ("audit_log", "ledger", "validations", "settlements")   # NFR-013
for _t in APPEND_ONLY:
    SCHEMA += (f"CREATE TRIGGER IF NOT EXISTS {_t}_no_update BEFORE UPDATE ON {_t} BEGIN SELECT RAISE(ABORT,'{_t} is append-only'); END;"
               f"CREATE TRIGGER IF NOT EXISTS {_t}_no_delete BEFORE DELETE ON {_t} BEGIN SELECT RAISE(ABORT,'{_t} is append-only'); END;")


def connect():
    c = sqlite3.connect(DB_PATH, timeout=15, isolation_level=None)
    c.row_factory = sqlite3.Row
    return c


def db():
    if "db" not in g:
        g.db = connect()
    return g.db


@app.teardown_appcontext
def close_db(_):
    d = g.pop("db", None)
    if d:
        d.close()


@contextmanager
def tx(d):
    d.execute("BEGIN IMMEDIATE")
    try:
        yield
        d.execute("COMMIT")
    except BaseException:
        d.execute("ROLLBACK")
        raise


def setting(d, key, default=""):
    r = d.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return r["value"] if r else default


def set_setting(d, key, value):
    d.execute("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, str(value)))


class UserError(Exception):
    def __init__(self, key, **kw):
        super().__init__(key)
        self.key, self.kw = key, kw


def audit(action, detail=""):
    d = db()
    if has_request_context():
        u = g.get("user")
        dev = f"{request.remote_addr} | {(request.headers.get('User-Agent') or '')[:70]}"
    else:
        u, dev = None, "system"
    d.execute("INSERT INTO audit_log(at,user_id,username,action,detail,device) VALUES(?,?,?,?,?,?)",
              (ts(now()), u["id"] if u else None, u["username"] if u else "system/mpesa", action, detail, dev))


# ----------------------------------------------------------------------------
# Business rules (mirrors Q5 booking.py and callback.py)
# ----------------------------------------------------------------------------
def release_expired_holds(d):
    d.execute("DELETE FROM bookings WHERE status='PENDING' AND expires_at<=?", (ts(now()),))


def fare_for(d, route_id, depart_utc):
    f = d.execute("SELECT * FROM fares WHERE route_id=?", (route_id,)).fetchone()
    hour = pts(depart_utc).astimezone(EAT).hour
    peak = any(a <= hour < b for a, b in PEAK_WINDOWS)
    base = f["peak"] if peak else f["off_peak"]
    surge = int(setting(d, "surge_pct", "100"))
    return (base * surge + 50) // 100, peak


def hold_seat(d, trip_id, seat_no, passenger_id):
    release_expired_holds(d)
    trip = d.execute("SELECT t.*, v.capacity FROM trips t JOIN vehicles v ON v.id=t.vehicle_id WHERE t.id=?", (trip_id,)).fetchone()
    if not trip or trip["status"] != "SCHEDULED":
        raise UserError("trip_unavailable")
    if not isinstance(seat_no, int) or not 1 <= seat_no <= trip["capacity"]:
        raise UserError("seat_range", n=trip["capacity"])
    if trip["depart_at"] <= ts(now()):
        raise UserError("trip_past")
    if d.execute("SELECT COUNT(*) c FROM bookings WHERE passenger_id=? AND status='PENDING'", (passenger_id,)).fetchone()["c"] >= 4:
        raise UserError("too_many_holds")
    fare, _ = fare_for(d, trip["route_id"], trip["depart_at"])
    n = now()
    try:
        with tx(d):
            cur = d.execute("INSERT INTO bookings(trip_id,seat_no,passenger_id,status,fare,created_at,expires_at) VALUES(?,?,?,?,?,?,?)",
                            (trip_id, seat_no, passenger_id, "PENDING", fare, ts(n), ts(n + timedelta(minutes=HOLD_MINUTES))))
            audit("seat_hold", f"trip={trip_id} seat={seat_no}")
        return cur.lastrowid
    except sqlite3.IntegrityError:          # UNIQUE(trip_id, seat_no) stops double-booking
        raise UserError("seat_taken")


def ledger_add(d, vehicle_id, kind, amount, payment_id, note=""):
    d.execute("INSERT INTO ledger(at,vehicle_id,kind,amount,payment_id,note) VALUES(?,?,?,?,?,?)",
              (ts(now()), vehicle_id, kind, amount, payment_id, note))


def sign(body): return hmac.new(TICKET_KEY, body.encode(), hashlib.sha256).hexdigest()[:24]
def b64e(s): return base64.urlsafe_b64encode(s.encode()).decode().rstrip("=")
def b64d(s): return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4)).decode()


def issue_ticket(d, pay_id, trip, seat_no, passenger_id):
    """NFR-003: signed code tied to route, vehicle and time window. One ticket per payment (UNIQUE)."""
    dep = pts(trip["depart_at"])
    vf, vt = ts(dep - timedelta(minutes=60)), ts(dep + timedelta(minutes=90))
    body = b64e("|".join(["M1", str(pay_id), str(trip["id"]), str(trip["vehicle_id"]), str(trip["route_id"]), vf, vt, secrets.token_hex(6)]))
    code = body + "~" + sign(body)
    d.execute("INSERT INTO tickets(payment_id,trip_id,seat_no,passenger_id,code,valid_from,valid_to) VALUES(?,?,?,?,?,?,?)",
              (pay_id, trip["id"], seat_no, passenger_id, code, vf, vt))


def initiate_payment(d, booking_id, passenger_id, phone_raw):
    release_expired_holds(d)
    b = d.execute("SELECT * FROM bookings WHERE id=? AND passenger_id=? AND status='PENDING'", (booking_id, passenger_id)).fetchone()
    if not b:
        raise UserError("hold_expired")
    msisdn = normalize_phone(phone_raw)
    if not msisdn:
        raise UserError("bad_phone")
    old = d.execute("SELECT id FROM payments WHERE trip_id=? AND seat_no=? AND passenger_id=? AND status='PENDING'",
                    (b["trip_id"], b["seat_no"], passenger_id)).fetchone()
    if old:
        return old["id"]
    n = ts(now())
    checkout = "ws_CO_" + now().strftime("%Y%m%d%H%M%S") + secrets.token_hex(4)
    with tx(d):
        cur = d.execute("INSERT INTO payments(checkout_id,trip_id,seat_no,passenger_id,amount,method,phone_enc,status,created_at,updated_at)"
                        " VALUES(?,?,?,?,?,?,?,?,?,?)", (checkout, b["trip_id"], b["seat_no"], passenger_id, b["fare"], "MPESA", enc(msisdn), "PENDING", n, n))
        audit("stk_push_requested", f"payment={cur.lastrowid} checkout={checkout}")
    # Real system: POST to Daraja /mpesa/stkpush/v1/processrequest here (keys from environment variables).
    return cur.lastrowid


def _confirm_paid(d, pay, receipt):
    trip = d.execute("SELECT * FROM trips WHERE id=?", (pay["trip_id"],)).fetchone()
    b = d.execute("SELECT * FROM bookings WHERE trip_id=? AND seat_no=?", (pay["trip_id"], pay["seat_no"])).fetchone()
    refund = False
    if b and b["passenger_id"] == pay["passenger_id"] and b["status"] == "PENDING":
        d.execute("UPDATE bookings SET status='CONFIRMED', expires_at=NULL WHERE id=?", (b["id"],))
    elif b is None:                         # error table #3: paid after the hold was released
        try:
            d.execute("INSERT INTO bookings(trip_id,seat_no,passenger_id,status,fare,created_at) VALUES(?,?,?,?,?,?)",
                      (pay["trip_id"], pay["seat_no"], pay["passenger_id"], "CONFIRMED", pay["amount"], ts(now())))
        except sqlite3.IntegrityError:
            refund = True
    else:
        refund = True
    status = "REFUND_DUE" if refund else "SUCCESS"
    d.execute("UPDATE payments SET status=?, result_code=0, result_desc=?, receipt=?, updated_at=? WHERE id=?",
              (status, "paid", receipt, ts(now()), pay["id"]))
    ledger_add(d, trip["vehicle_id"], "PAYMENT", pay["amount"], pay["id"], "M-Pesa " + (receipt or ""))
    if refund:
        audit("late_payment_seat_gone", f"payment={pay['id']} -> refund due")
    else:
        issue_ticket(d, pay["id"], trip, pay["seat_no"], pay["passenger_id"])   # same transaction (error table #5)
        audit("ticket_issued", f"payment={pay['id']}")


def handle_callback(d, data):
    """Same checks as Q5 callback.py: malformed -> 400, unknown -> 404, duplicate -> ack, ResultCode 0 only = paid."""
    try:
        cb = data["Body"]["stkCallback"]
        cid, code, desc = cb["CheckoutRequestID"], int(cb["ResultCode"]), cb.get("ResultDesc", "")
        meta = {i["Name"]: i.get("Value") for i in (cb.get("CallbackMetadata") or {}).get("Item", [])}
    except (KeyError, TypeError, ValueError, AttributeError):
        audit("callback_malformed", "rejected")
        return 400, {"error": "Malformed callback"}
    pay = d.execute("SELECT * FROM payments WHERE checkout_id=?", (cid,)).fetchone()
    if not pay:
        audit("callback_unknown", cid)
        return 404, {"error": "Unknown transaction"}
    ack = {"ResultCode": 0, "ResultDesc": "Accepted"}
    with tx(d):
        pay = d.execute("SELECT * FROM payments WHERE id=?", (pay["id"],)).fetchone()
        if pay["status"] != "PENDING":      # idempotency: one ticket per payment
            audit("callback_duplicate_ignored", cid)
            return 200, ack
        if code == 0:
            amt = meta.get("Amount")
            if amt is not None and int(float(amt)) != pay["amount"]:
                d.execute("UPDATE payments SET status='REVIEW', result_code=0, result_desc='amount mismatch', updated_at=? WHERE id=?", (ts(now()), pay["id"]))
                audit("callback_amount_mismatch", cid)
            else:
                _confirm_paid(d, pay, meta.get("MpesaReceiptNumber"))
        else:
            d.execute("UPDATE payments SET status='FAILED', result_code=?, result_desc=?, updated_at=? WHERE id=?", (code, desc, ts(now()), pay["id"]))
            d.execute("DELETE FROM bookings WHERE trip_id=? AND seat_no=? AND passenger_id=? AND status='PENDING'",
                      (pay["trip_id"], pay["seat_no"], pay["passenger_id"]))
            audit("payment_failed", f"{cid} code={code}")
    return 200, ack


def daraja_json(pay, code, receipt=None):
    cb = {"MerchantRequestID": "MR-" + secrets.token_hex(4), "CheckoutRequestID": pay["checkout_id"], "ResultCode": code,
          "ResultDesc": "The service request is processed successfully." if code == 0 else "Request failed"}
    if code == 0:
        cb["CallbackMetadata"] = {"Item": [{"Name": "Amount", "Value": float(pay["amount"])},
                                           {"Name": "MpesaReceiptNumber", "Value": receipt or "S" + secrets.token_hex(4).upper()},
                                           {"Name": "PhoneNumber", "Value": 254700000000}]}
    return {"Body": {"stkCallback": cb}}


def check_payment_status(d, pay):
    """Fallback for lost callbacks (NFR-009). Demo: uses the hidden result chosen in the simulator.
    A real version sends Daraja's M-Pesa Express Query using CheckoutRequestID."""
    if pay["status"] != "PENDING":
        return "Already resolved."
    if pay["hidden_result"] is None:
        return "Safaricom reports the payment is still pending."
    handle_callback(d, daraja_json(pay, pay["hidden_result"], "QRY" + secrets.token_hex(3).upper()))
    audit("status_query_resolved", f"payment={pay['id']}")
    return "Status query resolved the payment."


def validate_ticket(d, code, vehicle_id, conductor_id):
    """NFR-003/005. Returns (ok, reason_key, info)."""
    def log(res, reason, pid=None):
        d.execute("INSERT INTO validations(at,ticket_payment_id,result,reason,conductor_id,vehicle_id) VALUES(?,?,?,?,?,?)",
                  (ts(now()), pid, res, reason, conductor_id, vehicle_id))
    code = (code or "").strip()
    try:
        body, sig = code.rsplit("~", 1)
        parts = b64d(body).split("|")
        ver, pid, trip_id, veh, route, vf, vt, _ = parts
        if ver != "M1":
            raise ValueError
    except Exception:
        log("REJECTED", "malformed")
        return False, "v_malformed", {}
    if not hmac.compare_digest(sig, sign(body)):
        log("REJECTED", "forged")
        return False, "v_forged", {}
    tk = d.execute("SELECT * FROM tickets WHERE payment_id=?", (int(pid),)).fetchone()
    if not tk or tk["code"] != code:
        log("REJECTED", "unknown", int(pid))
        return False, "v_unknown", {}
    info = {"seat": tk["seat_no"]}
    n = ts(now())
    if str(vehicle_id) != veh:
        log("REJECTED", "wrong_vehicle", tk["payment_id"]); return False, "v_wrong_vehicle", info
    if tk["status"] == "CANCELLED":
        log("REJECTED", "cancelled", tk["payment_id"]); return False, "v_cancelled", info
    if n > tk["valid_to"]:
        log("REJECTED", "expired", tk["payment_id"]); return False, "v_expired", info
    if n < tk["valid_from"]:
        log("REJECTED", "too_early", tk["payment_id"]); return False, "v_early", info
    with tx(d):
        cur = d.execute("UPDATE tickets SET status='USED', used_at=?, used_by=? WHERE id=? AND status='VALID' AND used_at IS NULL", (n, conductor_id, tk["id"]))
        if cur.rowcount != 1:               # replay: second scan fails
            log("REJECTED", "already_used", tk["payment_id"])
            return False, "v_used", info
        log("VALID", "ok", tk["payment_id"])
    return True, "ok_valid", info


def cash_sale(d, user, trip_id, seat_no, phone_raw):
    trip = d.execute("SELECT t.*, v.capacity FROM trips t JOIN vehicles v ON v.id=t.vehicle_id WHERE t.id=?", (trip_id,)).fetchone()
    if not trip or trip["vehicle_id"] != user["vehicle_id"] or trip["status"] != "SCHEDULED":
        raise UserError("trip_unavailable")
    if not 1 <= seat_no <= trip["capacity"]:
        raise UserError("seat_range", n=trip["capacity"])
    release_expired_holds(d)
    msisdn = normalize_phone(phone_raw) if phone_raw else None
    if phone_raw and not msisdn:
        raise UserError("bad_phone")
    fare, _ = fare_for(d, trip["route_id"], trip["depart_at"])
    walkin = d.execute("SELECT id FROM users WHERE username='walkin'").fetchone()["id"]
    n = ts(now())
    try:
        with tx(d):
            d.execute("INSERT INTO bookings(trip_id,seat_no,passenger_id,status,fare,created_at) VALUES(?,?,?,?,?,?)",
                      (trip_id, seat_no, walkin, "CONFIRMED", fare, n))
            cur = d.execute("INSERT INTO payments(checkout_id,trip_id,seat_no,passenger_id,amount,method,phone_enc,status,result_code,receipt,created_at,updated_at)"
                            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", ("CASH-" + secrets.token_hex(5), trip_id, seat_no, walkin, fare, "CASH", enc(msisdn), "SUCCESS", 0, "CASH", n, n))
            ledger_add(d, trip["vehicle_id"], "PAYMENT", fare, cur.lastrowid, "cash")
            issue_ticket(d, cur.lastrowid, trip, seat_no, walkin)
            audit("cash_ticket", f"trip={trip_id} seat={seat_no} amount={fare}")
    except sqlite3.IntegrityError:
        raise UserError("seat_taken")
    return fare


def refund_payment(d, pay_id):
    pay = d.execute("SELECT * FROM payments WHERE id=?", (pay_id,)).fetchone()
    if not pay or pay["status"] not in ("SUCCESS", "REFUND_DUE"):
        raise UserError("trip_unavailable")
    tk = d.execute("SELECT * FROM tickets WHERE payment_id=?", (pay_id,)).fetchone()
    if tk and tk["status"] == "USED":
        raise ValueError("The ticket has already been used, so it cannot be refunded.")
    trip = d.execute("SELECT * FROM trips WHERE id=?", (pay["trip_id"],)).fetchone()
    with tx(d):
        ledger_add(d, trip["vehicle_id"], "REFUND", -pay["amount"], pay_id, "reversing entry")   # never edit history
        d.execute("UPDATE payments SET status='REFUNDED', updated_at=? WHERE id=?", (ts(now()), pay_id))
        if tk:
            d.execute("UPDATE tickets SET status='CANCELLED' WHERE id=?", (tk["id"],))
            d.execute("DELETE FROM bookings WHERE trip_id=? AND seat_no=? AND passenger_id=? AND status='CONFIRMED'",
                      (pay["trip_id"], pay["seat_no"], pay["passenger_id"]))
        audit("refund", f"payment={pay_id} amount={pay['amount']}")
    # Real system: also call the M-Pesa reversal API here.


def ingest_ping(d, vehicle_id, lat, lon, when=None):
    """Error table #9: keep the latest by timestamp, discard stale or duplicate pings."""
    when = when or ts(now())
    last = d.execute("SELECT MAX(ts) m FROM pings WHERE vehicle_id=?", (vehicle_id,)).fetchone()["m"]
    if last and when <= last:
        return False
    d.execute("INSERT INTO pings(vehicle_id,lat,lon,ts,received_at) VALUES(?,?,?,?,?)", (vehicle_id, lat, lon, when, ts(now())))
    return True


def seat_states(d, trip, me=None):
    release_expired_holds(d)
    st = {n: "free" for n in range(1, trip["capacity"] + 1)}
    for b in d.execute("SELECT seat_no, status, passenger_id FROM bookings WHERE trip_id=?", (trip["id"],)):
        if b["seat_no"] in st:
            st[b["seat_no"]] = "mine" if (me and b["passenger_id"] == me and b["status"] == "PENDING") else ("taken" if b["status"] == "CONFIRMED" else "held")
    return st


def triangle(x):
    x %= 2
    return x if x <= 1 else 2 - x


def pos_on_route(stops, f):
    p = f * (len(stops) - 1)
    i = min(int(p), len(stops) - 2)
    fr = p - i
    a, b = stops[i], stops[i + 1]
    return a["lat"] + (b["lat"] - a["lat"]) * fr, a["lon"] + (b["lon"] - a["lon"]) * fr


def simulate_movement(d):
    vehicles = d.execute("SELECT * FROM vehicles WHERE active=1 ORDER BY id").fetchall()
    moved = 0
    for v in vehicles[:-1]:                 # the last vehicle stays silent to show "last seen" handling
        stops = d.execute("SELECT * FROM stops WHERE route_id=? ORDER BY seq", (v["route_id"],)).fetchall()
        k = d.execute("SELECT COUNT(*) c FROM pings WHERE vehicle_id=?", (v["id"],)).fetchone()["c"]
        lat, lon = pos_on_route(stops, triangle(k * 0.09 + v["id"] * 0.37))
        moved += ingest_ping(d, v["id"], lat, lon, ts(now() + timedelta(seconds=k % 3)))
    return moved


# ----------------------------------------------------------------------------
# Seed data
# ----------------------------------------------------------------------------
ROUTES = [  # name, short, colour, (peak, off-peak fare), stops (approximate demo coordinates)
 ("CBD - Thika Road - Thika", "THK", "#c0392b", (100, 150), [("Nairobi CBD", -1.2864, 36.8172), ("Pangani", -1.2645, 36.8352), ("Roysambu", -1.2190, 36.8870),
   ("Githurai 45", -1.1930, 36.9170), ("Ruiru", -1.1460, 36.9600), ("Juja", -1.1040, 37.0120), ("Thika", -1.0332, 37.0693)]),
 ("CBD - Mombasa Road - Athi River", "MSA", "#2471a3", (100, 150), [("Nairobi CBD", -1.2864, 36.8172), ("Nyayo Stadium", -1.3040, 36.8270),
   ("Bellevue", -1.3300, 36.8500), ("Cabanas", -1.3380, 36.8780), ("Syokimau", -1.3600, 36.9370), ("Athi River", -1.4560, 36.9780)]),
 ("CBD - Ngong Road - Ngong", "NGO", "#7d3c98", (100, 140), [("Nairobi CBD", -1.2864, 36.8172), ("Kilimani", -1.2900, 36.7900),
   ("Adams Arcade", -1.3000, 36.7800), ("Dagoretti Corner", -1.2990, 36.7490), ("Karen", -1.3300, 36.7100), ("Ngong", -1.3667, 36.6667)]),
 ("CBD - Jogoo Road - Donholm", "JGO", "#1e8449", (60, 80), [("Nairobi CBD", -1.2864, 36.8172), ("Makadara", -1.2960, 36.8600),
   ("Jericho", -1.2990, 36.8740), ("Donholm", -1.2975, 36.8860), ("Kayole", -1.2750, 36.9150)]),
]
DEMO_PASSWORD = "Demo@1234"


def seed(d):
    if d.execute("SELECT COUNT(*) c FROM users").fetchone()["c"]:
        return
    n = ts(now())
    pw = generate_password_hash(DEMO_PASSWORD)

    def user(u, role, name, phone=None, veh=None, login=True):
        return d.execute("INSERT INTO users(username,pw_hash,role,full_name,phone_enc,vehicle_id,active,created_at) VALUES(?,?,?,?,?,?,?,?)",
                         (u, pw if login else generate_password_hash(secrets.token_hex(16)), role, name, enc(phone), veh, 1 if login else 0, n)).lastrowid
    user("wanjiku", "passenger", "Wanjiku Kamau", "254712345678")
    user("admin", "admin", "SACCO Administrator")
    user("auditor", "auditor", "Auditor (read-only)")
    o1, o2 = user("owner1", "owner", "Peter Otieno"), user("owner2", "owner", "Mary Achieng")
    user("walkin", "passenger", "Walk-in (cash) passenger", login=False)
    for i, (name, short, col, (off, peak), stops) in enumerate(ROUTES, 1):
        d.execute("INSERT INTO routes(id,name,short_name,color) VALUES(?,?,?,?)", (i, name, short, col))
        d.execute("INSERT INTO fares(route_id,off_peak,peak) VALUES(?,?,?)", (i, off, peak))
        for s, (sn, la, lo) in enumerate(stops):
            d.execute("INSERT INTO stops(route_id,seq,name,lat,lon) VALUES(?,?,?,?,?)", (i, s, sn, la, lo))
    letters, num = "ABCDEFGH", 101
    for i in range(1, 5):
        for k in range(2):
            vid = d.execute("INSERT INTO vehicles(plate,capacity,owner_id,route_id) VALUES(?,?,?,?)",
                            (f"KDX {num}{letters[(i - 1) * 2 + k]}", DEFAULT_CAPACITY, o1 if k == 0 else o2, i)).lastrowid
            num += 1
            if i == 1:
                user(f"conductor{k + 1}", "conductor", f"Conductor {k + 1}", veh=vid)
    for k, (key, val) in enumerate([("surge_pct", "100"), ("split_owner", "70"), ("split_sacco", "10"), ("split_crew", "20")]):
        set_setting(d, key, val)
    vehicles = d.execute("SELECT * FROM vehicles ORDER BY id").fetchall()
    for idx, v in enumerate(vehicles):          # initial GPS pings (the last vehicle is "last seen 42 min ago")
        stops = d.execute("SELECT * FROM stops WHERE route_id=? ORDER BY seq", (v["route_id"],)).fetchall()
        lat, lon = pos_on_route(stops, triangle(v["id"] * 0.37))
        age = 42 * 60 if idx == len(vehicles) - 1 else 60 + idx * 20
        ingest_ping(d, v["id"], lat, lon, ts(now() - timedelta(seconds=age)))


def ensure_trips(d):
    """Create trips for today and the next 2 days (safe to run on every start)."""
    today = datetime.now(EAT).replace(hour=0, minute=0, second=0, microsecond=0)
    for v in d.execute("SELECT * FROM vehicles WHERE active=1").fetchall():
        times = []
        for day in range(3):
            for h, m in ((6, 30), (7, 30), (12, 0), (16, 30), (18, 0)):
                times.append(today + timedelta(days=day, hours=h, minutes=m))
        soon = now() + timedelta(minutes=20 + (v["id"] % 4) * 10)
        if not d.execute("SELECT 1 FROM trips WHERE vehicle_id=? AND depart_at BETWEEN ? AND ?",
                         (v["id"], ts(now()), ts(now() + timedelta(hours=3)))).fetchone():
            times.append(soon.replace(second=0))
        for dt in times:
            if dt.astimezone(timezone.utc) > now() - timedelta(hours=2):
                d.execute("INSERT OR IGNORE INTO trips(route_id,vehicle_id,depart_at) VALUES(?,?,?)", (v["route_id"], v["id"], ts(dt)))


def init_db():
    d = connect()
    d.executescript(SCHEMA)
    seed(d)
    ensure_trips(d)
    d.close()


# ----------------------------------------------------------------------------
# Request handling: sessions, roles, CSRF
# ----------------------------------------------------------------------------
CSRF_EXEMPT = {"mpesa_callback"}
FAILS = {}


@app.before_request
def guard():
    g.user = None
    session.setdefault("csrf", secrets.token_urlsafe(16))
    uid = session.get("uid")
    if uid:
        if time.time() - session.get("last", 0) > IDLE_SECONDS:       # NFR-001
            lang = session.get("lang")
            session.clear()
            if lang: session["lang"] = lang
            flash(t("session_expired"), "info")
        else:
            session["last"] = time.time()
            g.user = db().execute("SELECT * FROM users WHERE id=? AND active=1", (uid,)).fetchone()
            if not g.user:
                session.clear()
    if request.method == "POST" and request.endpoint not in CSRF_EXEMPT:
        tok = request.form.get("_csrf") or request.headers.get("X-CSRF", "")
        if not secrets.compare_digest(tok, session.get("csrf", "")):
            abort(400)


@app.context_processor
def inject():
    return dict(t=t, user=g.get("user"), lang=session.get("lang", "en"), csrf=session.get("csrf"), DEMO=DEMO_MODE,
                role=(g.user["role"] if g.get("user") else None))


def needs(*roles):
    def deco(f):
        @wraps(f)
        def w(*a, **k):
            if not g.user:
                return redirect(url_for("login", next=request.path))
            if roles and g.user["role"] not in roles:
                audit("access_denied", request.path)
                abort(403)
            return f(*a, **k)
        return w
    return deco


@app.errorhandler(403)
def e403(_): return render_template("error.html", msg="You do not have access to this page."), 403
@app.errorhandler(404)
def e404(_): return render_template("error.html", msg="Page not found."), 404
@app.errorhandler(400)
def e400(_): return render_template("error.html", msg="Bad request (the form expired - go back and try again)."), 400


HOME = {"passenger": "/", "conductor": "/crew", "admin": "/admin", "owner": "/owner", "auditor": "/audit"}


def fail(e):
    flash(t(e.key, **e.kw), "bad")


# ----------------------------------------------------------------------------
# Public + passenger pages
# ----------------------------------------------------------------------------
@app.route("/lang/<code>")
def lang(code):
    if code in T:
        session["lang"] = code
    return redirect(request.referrer or "/")


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        u = request.form.get("username", "").strip().lower()
        recent = [x for x in FAILS.get(u, []) if time.time() - x < 300]
        if len(recent) >= 5:
            flash(t("locked"), "bad")
            return render_template("login.html")
        row = db().execute("SELECT * FROM users WHERE username=? AND active=1", (u,)).fetchone()
        if row and check_password_hash(row["pw_hash"], request.form.get("password", "")):
            lang_keep = session.get("lang")
            session.clear()
            session.update(uid=row["id"], last=time.time(), csrf=secrets.token_urlsafe(16))
            if lang_keep: session["lang"] = lang_keep
            g.user = row
            audit("login")
            nxt = request.args.get("next", "")
            return redirect(nxt if nxt.startswith("/") and not nxt.startswith("//") else HOME[row["role"]])
        FAILS[u] = recent + [time.time()]
        db().execute("INSERT INTO audit_log(at,user_id,username,action,detail,device) VALUES(?,?,?,?,?,?)",
                     (ts(now()), None, u[:40], "login_failed", "", request.remote_addr))
        flash(t("bad_login"), "bad")
    return render_template("login.html")


@app.route("/logout", methods=["POST"])
def logout():
    if g.user:
        audit("logout")
    lang_keep = session.get("lang")
    session.clear()
    if lang_keep: session["lang"] = lang_keep
    flash(t("logged_out"), "info")
    return redirect("/")


@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        f = request.form
        u, name, pw = f.get("username", "").strip().lower(), f.get("full_name", "").strip(), f.get("password", "")
        msisdn = normalize_phone(f.get("phone"))
        if not re.fullmatch(r"[a-z0-9_]{3,30}", u): flash("Username: 3-30 letters, numbers or underscore.", "bad")
        elif not name: flash("Enter your full name.", "bad")
        elif not msisdn: flash(t("bad_phone"), "bad")
        elif len(pw) < 8: flash("Password must be at least 8 characters.", "bad")
        else:
            try:
                db().execute("INSERT INTO users(username,pw_hash,role,full_name,phone_enc,created_at) VALUES(?,?,?,?,?,?)",
                             (u, generate_password_hash(pw), "passenger", name, enc(msisdn), ts(now())))
                flash("Account created. Please log in.", "ok")
                return redirect("/login")
            except sqlite3.IntegrityError:
                flash("That username is taken.", "bad")
    return render_template("register.html")


@app.route("/")
def home():
    d = db()
    release_expired_holds(d)
    today = eat_date()
    day = request.args.get("date") or today
    try:
        datetime.strptime(day, "%Y-%m-%d")
    except ValueError:
        day = today
    if day < today:
        day = today
    route_id = request.args.get("route", type=int)
    a, b = day_range_utc(day)
    q = ("SELECT t.*, r.name rname, r.short_name, v.plate, v.capacity, "
         "(SELECT COUNT(*) FROM bookings b WHERE b.trip_id=t.id) used FROM trips t JOIN routes r ON r.id=t.route_id "
         "JOIN vehicles v ON v.id=t.vehicle_id WHERE t.status='SCHEDULED' AND t.depart_at>=? AND t.depart_at<? AND t.depart_at>? ")
    args = [a, b, ts(now())]
    if route_id:
        q += "AND t.route_id=? "
        args.append(route_id)
    trips = []
    for tr in d.execute(q + "ORDER BY t.depart_at", args).fetchall():
        fare, peak = fare_for(d, tr["route_id"], tr["depart_at"])
        trips.append(dict(tr, fare=fare, peak=peak, left=tr["capacity"] - tr["used"]))
    return render_template("home.html", trips=trips, routes=d.execute("SELECT * FROM routes").fetchall(), day=day, today=today, route_id=route_id)


@app.route("/trip/<int:trip_id>")
def trip_page(trip_id):
    d = db()
    tr = d.execute("SELECT t.*, r.name rname, v.plate, v.capacity FROM trips t JOIN routes r ON r.id=t.route_id JOIN vehicles v ON v.id=t.vehicle_id WHERE t.id=?", (trip_id,)).fetchone()
    if not tr:
        abort(404)
    fare, peak = fare_for(d, tr["route_id"], tr["depart_at"])
    return render_template("seats.html", tr=tr, fare=fare, peak=peak, states=seat_states(d, tr, g.user["id"] if g.user else None))


@app.route("/trip/<int:trip_id>/hold", methods=["POST"])
@needs("passenger")
def trip_hold(trip_id):
    try:
        seat = int(request.form.get("seat_no", ""))
    except ValueError:
        seat = 0
    try:
        bid = hold_seat(db(), trip_id, seat, g.user["id"])
        return redirect(url_for("pay", booking_id=bid))
    except UserError as e:
        fail(e)
        return redirect(url_for("trip_page", trip_id=trip_id))


@app.route("/pay/<int:booking_id>", methods=["GET", "POST"])
@needs("passenger")
def pay(booking_id):
    d = db()
    release_expired_holds(d)
    b = d.execute("SELECT b.*, t.depart_at, t.vehicle_id, r.name rname, v.plate FROM bookings b JOIN trips t ON t.id=b.trip_id "
                  "JOIN routes r ON r.id=t.route_id JOIN vehicles v ON v.id=t.vehicle_id WHERE b.id=? AND b.passenger_id=? AND b.status='PENDING'",
                  (booking_id, g.user["id"])).fetchone()
    if not b:
        flash(t("hold_expired"), "bad")
        return redirect("/")
    if request.method == "POST":
        try:
            pid = initiate_payment(d, booking_id, g.user["id"], request.form.get("phone"))
            return redirect(url_for("payment_page", pid=pid))
        except UserError as e:
            fail(e)
            if e.key == "hold_expired":
                return redirect("/")
    secs = max(0, int((pts(b["expires_at"]) - now()).total_seconds()))
    phone = dec(g.user["phone_enc"]) or ""
    return render_template("pay.html", b=b, secs=secs, phone=("0" + phone[3:]) if phone else "")


@app.route("/payment/<int:pid>")
@needs("passenger", "admin")
def payment_page(pid):
    d = db()
    p = d.execute("SELECT p.*, t.depart_at, r.name rname FROM payments p JOIN trips t ON t.id=p.trip_id JOIN routes r ON r.id=t.route_id WHERE p.id=?", (pid,)).fetchone()
    if not p or (g.user["role"] == "passenger" and p["passenger_id"] != g.user["id"]):
        abort(404)
    tk = d.execute("SELECT id FROM tickets WHERE payment_id=?", (pid,)).fetchone()
    reason = None
    if p["status"] == "FAILED":
        reason = t("pay_%s" % p["result_code"]) if ("pay_%s" % p["result_code"]) in T["en"] else t("pay_other")
    return render_template("payment.html", p=p, tk=tk, reason=reason)


@app.route("/payment/<int:pid>/status")
@needs("passenger", "admin")
def payment_status(pid):
    p = db().execute("SELECT status, passenger_id FROM payments WHERE id=?", (pid,)).fetchone()
    if not p or (g.user["role"] == "passenger" and p["passenger_id"] != g.user["id"]):
        abort(404)
    return jsonify(status=p["status"])


@app.route("/sim/<int:pid>/<outcome>", methods=["POST"])
@needs("passenger", "admin")
def simulate(pid, outcome):
    """Sandbox only: plays the part of Safaricom so the whole flow can be demonstrated without Daraja."""
    if not DEMO_MODE:
        abort(404)
    d = db()
    p = d.execute("SELECT * FROM payments WHERE id=?", (pid,)).fetchone()
    if not p or (g.user["role"] == "passenger" and p["passenger_id"] != g.user["id"]):
        abort(404)
    codes = {"cancel": 1032, "insufficient": 1, "timeout": 1037, "wrongpin": 2001}
    if outcome == "success":
        handle_callback(d, daraja_json(p, 0))
    elif outcome in codes:
        handle_callback(d, daraja_json(p, codes[outcome]))
    elif outcome == "duplicate":
        rcpt = "DUP" + secrets.token_hex(3).upper()
        for _ in range(3):
            handle_callback(d, daraja_json(p, 0, rcpt))
        flash("Same success callback sent 3 times - only one ticket was issued.", "info")
    elif outcome == "lost":
        d.execute("UPDATE payments SET hidden_result=0 WHERE id=?", (pid,))
        flash("Callback 'lost'. The payment stays pending until the admin runs a status query.", "info")
    elif outcome == "late":
        d.execute("UPDATE bookings SET expires_at=? WHERE trip_id=? AND seat_no=? AND status='PENDING'", (ts(now() - timedelta(seconds=1)), p["trip_id"], p["seat_no"]))
        release_expired_holds(d)
        handle_callback(d, daraja_json(p, 0))
        flash("Payment arrived after the hold expired (late callback case).", "info")
    return redirect(url_for("payment_page", pid=pid))


@app.route("/mpesa/callback", methods=["POST"])
def mpesa_callback():
    status, body = handle_callback(db(), request.get_json(silent=True))
    return jsonify(body), status


def qr_svg(code):
    if not qrcode:
        return None
    img = qrcode.make(code, image_factory=qrcode.image.svg.SvgPathImage, box_size=8, border=2)
    buf = io.BytesIO()
    img.save(buf)
    return Markup(buf.getvalue().decode())


@app.route("/ticket/<int:tid>")
@needs("passenger", "admin")
def ticket_page(tid):
    d = db()
    tk = d.execute("SELECT k.*, t.depart_at, r.name rname, v.plate, p.amount, p.receipt, p.phone_enc FROM tickets k JOIN trips t ON t.id=k.trip_id "
                   "JOIN routes r ON r.id=t.route_id JOIN vehicles v ON v.id=t.vehicle_id JOIN payments p ON p.id=k.payment_id WHERE k.id=?", (tid,)).fetchone()
    if not tk or (g.user["role"] == "passenger" and tk["passenger_id"] != g.user["id"]):
        abort(404)
    return render_template("ticket.html", tk=tk, qr=qr_svg(tk["code"]), phone=mask_phone(tk["phone_enc"]))


@app.route("/my")
@needs("passenger")
def my_tickets():
    d = db()
    release_expired_holds(d)
    rows = d.execute("SELECT k.id, k.seat_no, k.status, k.valid_to, t.depart_at, r.name rname, v.plate FROM tickets k JOIN trips t ON t.id=k.trip_id "
                     "JOIN routes r ON r.id=t.route_id JOIN vehicles v ON v.id=t.vehicle_id WHERE k.passenger_id=? ORDER BY t.depart_at DESC", (g.user["id"],)).fetchall()
    holds = d.execute("SELECT b.id, b.seat_no, b.expires_at, t.depart_at, r.name rname FROM bookings b JOIN trips t ON t.id=b.trip_id JOIN routes r ON r.id=t.route_id "
                      "WHERE b.passenger_id=? AND b.status='PENDING'", (g.user["id"],)).fetchall()
    due = d.execute("SELECT id FROM payments WHERE passenger_id=? AND status='REFUND_DUE'", (g.user["id"],)).fetchall()
    return render_template("my.html", rows=rows, holds=holds, due=due)


@app.route("/account", methods=["GET", "POST"])
@needs("passenger")
def account():
    if request.method == "POST":          # NFR-002: data-subject deletion
        if not check_password_hash(g.user["pw_hash"], request.form.get("password", "")):
            flash(t("bad_login"), "bad")
        else:
            uid = g.user["id"]
            audit("account_deleted", f"user={uid}")
            db().execute("UPDATE users SET username=?, full_name='Deleted user', phone_enc=NULL, pw_hash=?, active=0 WHERE id=?",
                         (f"deleted-{uid}", generate_password_hash(secrets.token_hex(16)), uid))
            db().execute("UPDATE payments SET phone_enc=NULL WHERE passenger_id=?", (uid,))
            session.clear()
            flash("Your personal data was deleted.", "ok")
            return redirect("/")
    return render_template("account.html", phone=mask_phone(g.user["phone_enc"]))


@app.route("/account/export")
@needs("passenger")
def account_export():
    d, uid = db(), g.user["id"]
    data = {"username": g.user["username"], "full_name": g.user["full_name"], "phone": dec(g.user["phone_enc"]),
            "tickets": [dict(r) for r in d.execute("SELECT id, trip_id, seat_no, status, valid_from, valid_to FROM tickets WHERE passenger_id=?", (uid,))],
            "payments": [dict(r) for r in d.execute("SELECT checkout_id, amount, method, status, receipt, created_at FROM payments WHERE passenger_id=?", (uid,))]}
    audit("data_export")
    return Response(json.dumps(data, indent=2), mimetype="application/json", headers={"Content-Disposition": "attachment; filename=my-data.json"})


# ----------------------------------------------------------------------------
# Conductor / crew desk
# ----------------------------------------------------------------------------
@app.route("/crew")
@needs("conductor")
def crew():
    d = db()
    veh = d.execute("SELECT v.*, r.name rname FROM vehicles v JOIN routes r ON r.id=v.route_id WHERE v.id=?", (g.user["vehicle_id"],)).fetchone() if g.user["vehicle_id"] else None
    if not veh:
        return render_template("crew.html", veh=None)
    release_expired_holds(d)
    trips = d.execute("SELECT * FROM trips WHERE vehicle_id=? AND status='SCHEDULED' AND depart_at>? AND depart_at<? ORDER BY depart_at",
                      (veh["id"], ts(now() - timedelta(hours=2)), ts(now() + timedelta(hours=30)))).fetchall()
    sel = request.args.get("trip", type=int) or (next((x["id"] for x in trips if x["depart_at"] >= ts(now() - timedelta(minutes=90))), trips[0]["id"] if trips else None))
    manifest, states = [], {}
    if sel and any(x["id"] == sel for x in trips):
        manifest = d.execute("SELECT b.seat_no, k.status tstatus, k.used_at, p.phone_enc, p.method FROM bookings b "
                             "LEFT JOIN tickets k ON k.trip_id=b.trip_id AND k.seat_no=b.seat_no AND k.status!='CANCELLED' "
                             "LEFT JOIN payments p ON p.id=k.payment_id WHERE b.trip_id=? AND b.status='CONFIRMED' ORDER BY b.seat_no", (sel,)).fetchall()
        states = seat_states(d, {"id": sel, "capacity": veh["capacity"]})
    else:
        sel = None
    stats = d.execute("SELECT SUM(result='VALID') ok, SUM(result='REJECTED') bad FROM validations WHERE vehicle_id=? AND at>=?",
                      (veh["id"], day_range_utc(eat_date())[0])).fetchone()
    return render_template("crew.html", veh=veh, trips=trips, sel=sel, manifest=[dict(m, phone=mask_phone(m["phone_enc"])) for m in manifest],
                           states=states, stats=stats)


@app.route("/crew/validate", methods=["POST"])
@needs("conductor")
def crew_validate():
    ok, key, info = validate_ticket(db(), request.form.get("code"), g.user["vehicle_id"], g.user["id"])
    msg = t(key) + (f"  ({t('seat')} {info['seat']})" if info.get("seat") else "")
    flash(msg, "ok" if ok else "bad")
    return redirect(url_for("crew", trip=request.form.get("trip", type=int)))


@app.route("/crew/cash", methods=["POST"])
@needs("conductor")
def crew_cash():
    trip = request.form.get("trip", type=int)
    try:
        seat = int(request.form.get("seat_no", ""))
        cash_sale(db(), g.user, trip, seat, request.form.get("phone", "").strip())
        flash(t("cash_ok", n=seat), "ok")
    except ValueError:
        flash(t("seat_range", n=DEFAULT_CAPACITY), "bad")
    except UserError as e:
        fail(e)
    return redirect(url_for("crew", trip=trip))


@app.route("/api/v1/ping", methods=["POST"])
@needs("conductor")
def api_ping():
    j = request.get_json(silent=True) or {}
    try:
        lat, lon = float(j["lat"]), float(j["lon"])
        assert -90 <= lat <= 90 and -180 <= lon <= 180
        when = ts(datetime.fromtimestamp(float(j["ts"]), timezone.utc)) if j.get("ts") else None
    except Exception:
        return jsonify(error="bad location"), 400
    if not g.user["vehicle_id"]:
        return jsonify(error="no vehicle"), 400
    return jsonify(stored=ingest_ping(db(), g.user["vehicle_id"], lat, lon, when))


# ----------------------------------------------------------------------------
# Admin / SACCO
# ----------------------------------------------------------------------------
@app.route("/admin")
@needs("admin")
def admin():
    d = db()
    a, b = day_range_utc(eat_date())
    k = {
     "tickets": d.execute("SELECT COUNT(*) c FROM tickets k JOIN payments p ON p.id=k.payment_id WHERE p.updated_at>=? AND p.updated_at<?", (a, b)).fetchone()["c"],
     "revenue": d.execute("SELECT COALESCE(SUM(amount),0) s FROM ledger WHERE at>=? AND at<?", (a, b)).fetchone()["s"],
     "pending": d.execute("SELECT COUNT(*) c FROM payments WHERE status='PENDING'").fetchone()["c"],
     "attention": d.execute("SELECT COUNT(*) c FROM payments WHERE status IN ('REFUND_DUE','REVIEW')").fetchone()["c"],
     "vehicles": d.execute("SELECT COUNT(*) c FROM vehicles WHERE active=1").fetchone()["c"],
     "trips": d.execute("SELECT COUNT(*) c FROM trips WHERE depart_at>=? AND depart_at<? AND status='SCHEDULED'", (a, b)).fetchone()["c"],
     "validations": d.execute("SELECT COALESCE(SUM(result='VALID'),0) ok, COALESCE(SUM(result='REJECTED'),0) bad FROM validations WHERE at>=? AND at<?", (a, b)).fetchone()}
    m = build_map(d)
    return render_template("admin.html", k=k, live=sum(1 for v in m["vehicles"] if v["live"]), total=len(m["vehicles"]))


@app.route("/admin/fleet", methods=["GET", "POST"])
@needs("admin")
def admin_fleet():
    d = db()
    if request.method == "POST":
        f, act = request.form, request.form.get("action")
        try:
            if act == "vehicle":
                cap = int(f["capacity"])
                d.execute("INSERT INTO vehicles(plate,capacity,owner_id,route_id) VALUES(?,?,?,?)", (f["plate"].strip().upper(), cap, int(f["owner_id"]), int(f["route_id"])))
                audit("vehicle_added", f["plate"])
                flash("Vehicle added.", "ok")
            elif act == "user":
                role = f["role"]
                if role not in ("conductor", "owner", "auditor", "admin") or len(f["password"]) < 8 or not re.fullmatch(r"[a-z0-9_]{3,30}", f["username"].strip().lower()):
                    raise ValueError("Check username (3-30 letters/numbers), role and password (8+ characters).")
                veh = int(f["vehicle_id"]) if role == "conductor" and f.get("vehicle_id") else None
                d.execute("INSERT INTO users(username,pw_hash,role,full_name,vehicle_id,created_at) VALUES(?,?,?,?,?,?)",
                          (f["username"].strip().lower(), generate_password_hash(f["password"]), role, f["full_name"].strip() or f["username"], veh, ts(now())))
                audit("user_added", f"{f['username']} ({role})")
                flash("User added.", "ok")
            elif act == "toggle":
                d.execute("UPDATE vehicles SET active=1-active WHERE id=?", (int(f["vehicle_id"]),))
                audit("vehicle_toggled", f["vehicle_id"])
        except sqlite3.IntegrityError:
            flash("That plate number or username already exists.", "bad")
        except (ValueError, KeyError) as e:
            flash(str(e) if isinstance(e, ValueError) and e.args and len(str(e)) > 20 else "Please fill in all fields correctly.", "bad")
        return redirect(url_for("admin_fleet"))
    vehicles = d.execute("SELECT v.*, r.short_name, u.full_name owner, (SELECT GROUP_CONCAT(username) FROM users c WHERE c.vehicle_id=v.id) crew "
                         "FROM vehicles v JOIN routes r ON r.id=v.route_id JOIN users u ON u.id=v.owner_id ORDER BY v.id").fetchall()
    users = d.execute("SELECT id, username, role, full_name, active FROM users WHERE username!='walkin' AND role!='passenger' ORDER BY role, username").fetchall()
    return render_template("admin_fleet.html", vehicles=vehicles, users=users, routes=d.execute("SELECT * FROM routes").fetchall(),
                           owners=d.execute("SELECT id, full_name FROM users WHERE role='owner'").fetchall())


@app.route("/admin/trips", methods=["GET", "POST"])
@needs("admin")
def admin_trips():
    d = db()
    if request.method == "POST":
        f = request.form
        try:
            if f["action"] == "create":
                v = d.execute("SELECT * FROM vehicles WHERE id=? AND active=1", (int(f["vehicle_id"]),)).fetchone()
                dep = datetime.strptime(f["depart"], "%Y-%m-%dT%H:%M").replace(tzinfo=EAT)
                if not v: raise ValueError("Choose an active vehicle.")
                if dep.astimezone(timezone.utc) <= now(): raise ValueError("The departure time is in the past.")
                d.execute("INSERT INTO trips(route_id,vehicle_id,depart_at) VALUES(?,?,?)", (v["route_id"], v["id"], ts(dep)))
                audit("trip_created", f"{v['plate']} {f['depart']}")
                flash("Trip created.", "ok")
            elif f["action"] == "cancel":
                tid = int(f["trip_id"])
                if d.execute("SELECT 1 FROM bookings WHERE trip_id=? AND status='CONFIRMED'", (tid,)).fetchone():
                    raise ValueError("Tickets were already sold for this trip. Refund them first.")
                d.execute("UPDATE trips SET status='CANCELLED' WHERE id=?", (tid,))
                audit("trip_cancelled", str(tid))
                flash("Trip cancelled.", "ok")
        except sqlite3.IntegrityError:
            flash("That vehicle already has a trip at this time.", "bad")
        except (ValueError, KeyError) as e:
            flash(str(e) if len(str(e)) > 25 else "Please fill in the form correctly.", "bad")
        return redirect(url_for("admin_trips"))
    trips = d.execute("SELECT t.*, r.short_name, v.plate, (SELECT COUNT(*) FROM bookings b WHERE b.trip_id=t.id AND b.status='CONFIRMED') sold FROM trips t "
                      "JOIN routes r ON r.id=t.route_id JOIN vehicles v ON v.id=t.vehicle_id WHERE t.depart_at>? ORDER BY t.depart_at LIMIT 150", (ts(now() - timedelta(hours=1)),)).fetchall()
    return render_template("admin_trips.html", trips=trips, vehicles=d.execute("SELECT v.id, v.plate, r.short_name FROM vehicles v JOIN routes r ON r.id=v.route_id WHERE v.active=1").fetchall())


@app.route("/admin/fares", methods=["GET", "POST"])
@needs("admin")
def admin_fares():
    d = db()
    if request.method == "POST":
        f = request.form
        try:
            surge, so, sa, sc = int(f["surge_pct"]), int(f["split_owner"]), int(f["split_sacco"]), int(f["split_crew"])
            if not 100 <= surge <= 200: raise ValueError("Surge must be between 100% and 200%.")
            if min(so, sa, sc) < 0 or so + sa + sc != 100: raise ValueError("Owner + SACCO + crew shares must add up to exactly 100.")
            rows = []
            for r in d.execute("SELECT id FROM routes"):
                off, peak = int(f[f"off_{r['id']}"]), int(f[f"peak_{r['id']}"])
                if off < 0 or peak < 0: raise ValueError("Fares cannot be negative.")
                rows.append((off, peak, r["id"]))
            with tx(d):
                for off, peak, rid in rows:
                    d.execute("UPDATE fares SET off_peak=?, peak=? WHERE route_id=?", (off, peak, rid))
                for key, val in (("surge_pct", surge), ("split_owner", so), ("split_sacco", sa), ("split_crew", sc)):
                    set_setting(d, key, val)
                audit("fares_changed", f"surge={surge} split={so}/{sa}/{sc} fares={[(r[2], r[0], r[1]) for r in rows]}")
            flash("Fares and splits saved. New fares apply to new bookings only.", "ok")
        except (ValueError, KeyError) as e:
            flash(str(e) if len(str(e)) > 25 else "Enter whole numbers in every box.", "bad")
        return redirect(url_for("admin_fares"))
    fares = d.execute("SELECT f.*, r.name FROM fares f JOIN routes r ON r.id=f.route_id").fetchall()
    return render_template("admin_fares.html", fares=fares, s={k: setting(d, k) for k in ("surge_pct", "split_owner", "split_sacco", "split_crew")})


def reconciliation(d):
    pend = d.execute("SELECT p.*, r.short_name FROM payments p JOIN trips t ON t.id=p.trip_id JOIN routes r ON r.id=t.route_id WHERE p.status='PENDING' AND p.created_at<? ORDER BY p.created_at",
                     (ts(now() - timedelta(minutes=PENDING_QUERY_MINUTES)),)).fetchall()
    attention = d.execute("SELECT * FROM payments WHERE status IN ('REFUND_DUE','REVIEW') ORDER BY created_at").fetchall()
    no_ticket = d.execute("SELECT p.* FROM payments p WHERE p.status='SUCCESS' AND NOT EXISTS (SELECT 1 FROM tickets k WHERE k.payment_id=p.id)").fetchall()
    ledger = d.execute("SELECT COALESCE(SUM(amount),0) s FROM ledger").fetchone()["s"]
    expect = d.execute("SELECT COALESCE(SUM(CASE WHEN status IN ('SUCCESS','REFUND_DUE') THEN amount ELSE 0 END),0) s FROM payments").fetchone()["s"]
    return dict(pending=pend, attention=attention, no_ticket=no_ticket, ledger=ledger, expect=expect, variance=ledger - expect)


@app.route("/admin/payments")
@needs("admin", "auditor")
def admin_payments():
    d = db()
    rows = d.execute("SELECT p.*, r.short_name, v.plate FROM payments p JOIN trips t ON t.id=p.trip_id JOIN routes r ON r.id=t.route_id JOIN vehicles v ON v.id=t.vehicle_id ORDER BY p.id DESC LIMIT 100").fetchall()
    return render_template("admin_payments.html", rows=[dict(r, masked=mask_phone(r["phone_enc"])) for r in rows], rec=reconciliation(d))


@app.route("/admin/payments/<int:pid>/<action>", methods=["POST"])
@needs("admin")
def admin_payment_action(pid, action):
    d = db()
    try:
        if action == "query":
            flash(check_payment_status(d, d.execute("SELECT * FROM payments WHERE id=?", (pid,)).fetchone()), "info")
        elif action == "refund":
            refund_payment(d, pid)
            flash("Refund recorded as a reversing entry. Remember to complete the M-Pesa reversal.", "ok")
    except (ValueError, UserError) as e:
        flash(str(e), "bad")
    return redirect(url_for("admin_payments"))


@app.route("/settlement", methods=["GET", "POST"])
@needs("admin", "owner", "auditor")
def settlement():
    d = db()
    day = request.values.get("day") or eat_date()
    try:
        datetime.strptime(day, "%Y-%m-%d")
    except ValueError:
        day = eat_date()
    a, b = day_range_utc(day)
    split = {k: int(setting(d, "split_" + k, "0")) for k in ("owner", "sacco", "crew")}
    if request.method == "POST":                      # only admins reach this: owners/auditors are read-only
        if g.user["role"] != "admin":
            abort(403)
        made = 0
        for r in d.execute("SELECT vehicle_id, SUM(amount) c FROM ledger WHERE at>=? AND at<? GROUP BY vehicle_id", (a, b)).fetchall():
            if d.execute("SELECT 1 FROM settlements WHERE vehicle_id=? AND day=?", (r["vehicle_id"], day)).fetchone():
                continue
            sacco, crew = pct_of(r["c"], split["sacco"]), pct_of(r["c"], split["crew"])
            owner = r["c"] - sacco - crew                 # owner takes the rounding remainder: splits always sum exactly
            d.execute("INSERT INTO settlements(vehicle_id,day,collected,owner_amt,sacco_amt,crew_amt,created_at,created_by) VALUES(?,?,?,?,?,?,?,?)",
                      (r["vehicle_id"], day, r["c"], owner, sacco, crew, ts(now()), g.user["id"]))
            made += 1
        audit("settlement_run", f"day={day} vehicles={made}")
        flash(f"Settlement run for {day}: {made} vehicle(s) settled.", "ok")
        return redirect(url_for("settlement", day=day))
    q = ("SELECT v.id, v.plate, u.full_name owner, COALESCE((SELECT SUM(amount) FROM ledger l WHERE l.vehicle_id=v.id AND l.at>=? AND l.at<?),0) collected, "
         "(SELECT COUNT(*) FROM ledger l WHERE l.vehicle_id=v.id AND l.at>=? AND l.at<?) n FROM vehicles v JOIN users u ON u.id=v.owner_id ")
    args = [a, b, a, b]
    if g.user["role"] == "owner":
        q += "WHERE v.owner_id=? "
        args.append(g.user["id"])
    rows = []
    for r in d.execute(q + "ORDER BY v.plate", args).fetchall():
        s = d.execute("SELECT * FROM settlements WHERE vehicle_id=? AND day=?", (r["id"], day)).fetchone()
        sacco, crew = pct_of(r["collected"], split["sacco"]), pct_of(r["collected"], split["crew"])
        rows.append(dict(r, settled=s, owner_amt=s["owner_amt"] if s else r["collected"] - sacco - crew, sacco_amt=s["sacco_amt"] if s else sacco, crew_amt=s["crew_amt"] if s else crew))
    totals = {k: sum(r[k] for r in rows) for k in ("collected", "owner_amt", "sacco_amt", "crew_amt")}
    return render_template("settlement.html", rows=rows, day=day, split=split, totals=totals, rec=reconciliation(d) if g.user["role"] != "owner" else None)


@app.route("/owner")
@needs("owner")
def owner():
    d = db()
    a, b = day_range_utc(eat_date())
    vs = d.execute("SELECT v.*, r.short_name, COALESCE((SELECT SUM(amount) FROM ledger l WHERE l.vehicle_id=v.id AND l.at>=? AND l.at<?),0) today FROM vehicles v "
                   "JOIN routes r ON r.id=v.route_id WHERE v.owner_id=? ORDER BY v.plate", (a, b, g.user["id"])).fetchall()
    last = {m["id"]: m for m in build_map(d, g.user["id"])["vehicles"]}
    hist = d.execute("SELECT s.*, v.plate FROM settlements s JOIN vehicles v ON v.id=s.vehicle_id WHERE v.owner_id=? ORDER BY s.day DESC, v.plate LIMIT 30", (g.user["id"],)).fetchall()
    return render_template("owner.html", vs=vs, last=last, hist=hist)


@app.route("/audit")
@needs("admin", "auditor")
def audit_page():
    d = db()
    q, args = "SELECT * FROM audit_log ", []
    if request.args.get("q"):
        q += "WHERE action LIKE ? OR detail LIKE ? OR username LIKE ? "
        args = ["%" + request.args["q"] + "%"] * 3
    rows = d.execute(q + "ORDER BY id DESC LIMIT 200", args).fetchall()
    val = d.execute("SELECT * FROM validations ORDER BY id DESC LIMIT 30").fetchall()
    return render_template("audit.html", rows=rows, val=val, q=request.args.get("q", ""))


def build_map(d, owner_id=None):
    stops = d.execute("SELECT s.*, r.color, r.short_name FROM stops s JOIN routes r ON r.id=s.route_id ORDER BY s.route_id, s.seq").fetchall()
    W, H, P = 760, 520, 45
    lat0, lat1 = min(s["lat"] for s in stops), max(s["lat"] for s in stops)
    lon0, lon1 = min(s["lon"] for s in stops), max(s["lon"] for s in stops)

    def xy(la, lo): return round(P + (lo - lon0) / (lon1 - lon0) * (W - 2 * P), 1), round(P + (lat1 - la) / (lat1 - lat0) * (H - 2 * P), 1)
    routes = {}
    for s in stops:
        r = routes.setdefault(s["route_id"], dict(color=s["color"], name=s["short_name"], pts=[], stops=[]))
        x, y = xy(s["lat"], s["lon"])
        r["pts"].append(f"{x},{y}")
        r["stops"].append(dict(x=x, y=y, name=s["name"], end=s["seq"] == 0 or s["seq"] == max(q["seq"] for q in stops if q["route_id"] == s["route_id"])))
    q = "SELECT v.*, r.short_name, r.color FROM vehicles v JOIN routes r ON r.id=v.route_id WHERE v.active=1 "
    args = []
    if owner_id:
        q += "AND v.owner_id=? "
        args.append(owner_id)
    vehicles = []
    for v in d.execute(q, args).fetchall():
        p = d.execute("SELECT * FROM pings WHERE vehicle_id=? ORDER BY ts DESC LIMIT 1", (v["id"],)).fetchone()
        if not p:
            vehicles.append(dict(id=v["id"], plate=v["plate"], route=v["short_name"], color=v["color"], x=None, y=None, live=False, seen="never")); continue
        age = (now() - pts(p["ts"])).total_seconds() / 60
        x, y = xy(p["lat"], p["lon"])
        seen = "just now" if age < 1 else (f"{int(age)} min ago" if age < 120 else f"{int(age // 60)} h ago")
        vehicles.append(dict(id=v["id"], plate=v["plate"], route=v["short_name"], color=v["color"], x=x, y=y, live=age < STALE_MINUTES, seen=seen))
    return dict(W=W, H=H, routes=list(routes.values()), vehicles=vehicles)


@app.route("/tracking")
@needs("admin", "owner", "auditor")
def tracking():
    return render_template("tracking.html", m=build_map(db(), g.user["id"] if g.user["role"] == "owner" else None))


@app.route("/tracking/simulate", methods=["POST"])
@needs("admin")
def tracking_sim():
    flash(f"{simulate_movement(db())} vehicle(s) moved. The last vehicle stays silent to demonstrate a dead zone.", "info")
    return redirect(url_for("tracking"))


# ----------------------------------------------------------------------------
# REST API + GTFS export (NFR-014)
# ----------------------------------------------------------------------------
@app.route("/api/v1/routes")
def api_routes():
    d = db()
    return jsonify([dict(r, stops=[dict(s) for s in d.execute("SELECT name, lat, lon, seq FROM stops WHERE route_id=? ORDER BY seq", (r["id"],))]) for r in d.execute("SELECT id, name, short_name FROM routes")])


@app.route("/api/v1/trips")
def api_trips():
    d = db()
    day = request.args.get("date") or eat_date()
    try:
        a, b = day_range_utc(day)
    except ValueError:
        return jsonify(error="date must be YYYY-MM-DD"), 400
    q, args = "SELECT id, route_id, vehicle_id, depart_at FROM trips WHERE status='SCHEDULED' AND depart_at>=? AND depart_at<?", [a, b]
    if request.args.get("route_id"):
        q += " AND route_id=?"
        args.append(request.args.get("route_id", type=int))
    out = []
    for r in d.execute(q + " ORDER BY depart_at", args):
        fare, peak = fare_for(d, r["route_id"], r["depart_at"])
        out.append(dict(r, fare_kes=fare, peak=peak))
    return jsonify(out)


@app.route("/api/v1/trips/<int:trip_id>/seats")
def api_seats(trip_id):
    d = db()
    tr = d.execute("SELECT t.id, v.capacity FROM trips t JOIN vehicles v ON v.id=t.vehicle_id WHERE t.id=?", (trip_id,)).fetchone()
    if not tr:
        return jsonify(error="not found"), 404
    return jsonify({str(k): v for k, v in seat_states(d, tr).items()})


@app.route("/admin/gtfs.zip")
@needs("admin", "auditor")
def gtfs():
    d = db()
    files, stop_ids = {}, {}

    def table(name, header, rows):
        s = io.StringIO()
        w = csv.writer(s)
        w.writerow(header)
        w.writerows(rows)
        files[name] = s.getvalue()
    table("agency.txt", ["agency_id", "agency_name", "agency_url", "agency_timezone"], [["SACCO1", "Nairobi Demo SACCO", "https://example.org", "Africa/Nairobi"]])
    stops, times = [], []
    routes = d.execute("SELECT * FROM routes").fetchall()
    for r in routes:
        for i, s in enumerate(d.execute("SELECT * FROM stops WHERE route_id=? ORDER BY seq", (r["id"],))):
            if s["name"] not in stop_ids:
                stop_ids[s["name"]] = f"S{len(stop_ids) + 1}"
                stops.append([stop_ids[s["name"]], s["name"], s["lat"], s["lon"]])
            mins = 6 * 60 + i * 6
            hhmm = f"{mins // 60:02d}:{mins % 60:02d}:00"
            times.append([f"T{r['id']}", hhmm, hhmm, stop_ids[s["name"]], i + 1])
    table("stops.txt", ["stop_id", "stop_name", "stop_lat", "stop_lon"], stops)
    # continuous_pickup / continuous_drop_off = 0 means "continuous stopping" (flexible boarding along the road)
    table("routes.txt", ["route_id", "agency_id", "route_short_name", "route_long_name", "route_type", "continuous_pickup", "continuous_drop_off"],
          [[f"R{r['id']}", "SACCO1", r["short_name"], r["name"], 3, 0, 0] for r in routes])
    table("trips.txt", ["route_id", "service_id", "trip_id"], [[f"R{r['id']}", "DAILY", f"T{r['id']}"] for r in routes])
    table("stop_times.txt", ["trip_id", "arrival_time", "departure_time", "stop_id", "stop_sequence"], times)
    today = datetime.now(EAT)
    table("calendar.txt", ["service_id", "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday", "start_date", "end_date"],
          [["DAILY", 1, 1, 1, 1, 1, 1, 1, today.strftime("%Y%m%d"), (today + timedelta(days=365)).strftime("%Y%m%d")]])
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for n, c in files.items():
            z.writestr(n, c)
    audit("gtfs_export")
    return Response(buf.getvalue(), mimetype="application/zip", headers={"Content-Disposition": "attachment; filename=matatu-gtfs.zip"})


# ----------------------------------------------------------------------------
# Templates
# ----------------------------------------------------------------------------
TEMPLATES = {}
TEMPLATES["base.html"] = """<!doctype html><html lang="{{ lang }}"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>{% block title %}{{ t('app_name') }}{% endblock %}</title>
<style>
:root{--g:#0b5d3b;--g2:#0e7a4d;--a:#f5a623;--bg:#f2f5f2;--ink:#1d2b24;--mut:#66756c;--bad:#b3261e;--ok:#1b7f43;--line:#dde5df}
*{box-sizing:border-box}body{margin:0;font:16px/1.45 system-ui,Segoe UI,Roboto,sans-serif;background:var(--bg);color:var(--ink)}
header.top{background:var(--g);color:#fff;display:flex;flex-wrap:wrap;align-items:center;gap:6px 18px;padding:10px 16px;border-bottom:4px solid var(--a)}
.brand{color:#fff;font-weight:800;font-size:1.15rem;text-decoration:none}
nav{display:flex;flex-wrap:wrap;gap:4px 14px;align-items:center;flex:1}nav a,nav button.link{color:#e8f5ee;text-decoration:none;background:none;border:0;font:inherit;cursor:pointer;padding:4px 0}
nav a:hover,nav button.link:hover{color:#fff;text-decoration:underline}.lang a{padding:2px 6px;border-radius:4px}.lang a.on{background:var(--a);color:#222}
main{max-width:1000px;margin:18px auto;padding:0 14px}h1{margin:.2em 0 .5em;font-size:1.5rem}h2{font-size:1.15rem;margin:1.2em 0 .5em}
.card{background:#fff;border:1px solid var(--line);border-radius:12px;padding:16px;margin:12px 0;box-shadow:0 1px 2px rgba(0,0,0,.04)}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:12px}.kpi{background:#fff;border:1px solid var(--line);border-radius:12px;padding:14px}
.kpi b{display:block;font-size:1.7rem}.kpi span{color:var(--mut);font-size:.9rem}
label{display:block;font-weight:600;margin:10px 0 4px}input,select,textarea{width:100%;padding:11px;border:1px solid #b9c7bf;border-radius:8px;font:inherit;background:#fff}
textarea{min-height:80px;font-family:ui-monospace,monospace;font-size:.85rem}
.btn{display:inline-block;background:var(--g2);color:#fff;border:0;border-radius:10px;padding:12px 18px;font:inherit;font-weight:700;cursor:pointer;text-decoration:none;min-height:46px}
.btn:hover{background:var(--g)}.btn.alt{background:var(--a);color:#222}.btn.sec{background:#fff;color:var(--g);border:1px solid var(--g)}.btn.red{background:var(--bad)}.btn.sm{padding:6px 10px;min-height:0;font-size:.85rem}
.row{display:flex;flex-wrap:wrap;gap:10px;align-items:end}.row>*{flex:1;min-width:140px}.row>.btn{flex:0 0 auto}
table{width:100%;border-collapse:collapse;background:#fff;border-radius:10px;overflow:hidden;font-size:.93rem}th,td{padding:9px 10px;border-bottom:1px solid var(--line);text-align:left;vertical-align:top}
th{background:#e6efe9;font-size:.8rem;text-transform:uppercase;letter-spacing:.03em}.scroll{overflow-x:auto}td.n,th.n{text-align:right}
.flash{padding:12px 14px;border-radius:10px;margin:10px 0;font-weight:600}.flash.ok{background:#dff3e6;color:#0d5a2d;border:1px solid #a9d9b9;font-size:1.15rem}
.flash.bad{background:#fde6e4;color:var(--bad);border:1px solid #f3b8b3;font-size:1.05rem}.flash.info{background:#fff3d6;color:#6b4b00;border:1px solid #f0d58a}
.tag{display:inline-block;padding:2px 8px;border-radius:99px;font-size:.78rem;font-weight:700;background:#e6efe9;color:var(--g)}.tag.peak{background:#fff0cf;color:#8a5a00}
.tag.bad{background:#fde6e4;color:var(--bad)}.tag.ok{background:#dff3e6;color:var(--ok)}.tag.warn{background:#fff3d6;color:#8a5a00}.mut{color:var(--mut)}
.seatmap{display:grid;grid-template-columns:repeat(4,64px);gap:10px;justify-content:center;padding:18px;background:#e9efe9;border-radius:24px 24px 12px 12px;border:3px solid #9fb3a7;width:max-content;max-width:100%;margin:10px auto}
.seatmap label{margin:0}.seatmap input{display:none}.seat{display:flex;align-items:center;justify-content:center;width:64px;height:56px;border-radius:10px;font-weight:800;border:2px solid #6c8d7a;background:#fff;cursor:pointer}
.seat.taken{background:#cfd6d1;border-color:#aab5ae;color:#8a958e;cursor:not-allowed;text-decoration:line-through}.seat.held{background:#ffe9b8;border-color:#e0b050;color:#8a5a00;cursor:not-allowed}
.seat.mine{background:#cfe8ff;border-color:#4c8fd6}.seat.driver{background:none;border:0;cursor:default;font-size:1.6rem}input:checked+.seat{background:var(--g2);color:#fff;border-color:var(--g);transform:scale(1.06)}
.legend{display:flex;gap:14px;justify-content:center;flex-wrap:wrap;font-size:.85rem;color:var(--mut)}.legend i{display:inline-block;width:14px;height:14px;border-radius:4px;border:2px solid #6c8d7a;vertical-align:-2px;margin-right:4px}
.big{font-size:2rem;font-weight:800}.center{text-align:center}.qr svg{width:240px;height:240px;max-width:100%}.code{font:12px ui-monospace,monospace;word-break:break-all;background:#f3f6f4;padding:8px;border-radius:8px}
.sim{border:2px dashed var(--a);background:#fffaf0}.sim .btn{margin:4px 4px 0 0}footer{text-align:center;color:var(--mut);font-size:.8rem;padding:24px}
@media(max-width:560px){h1{font-size:1.3rem}.seatmap{grid-template-columns:repeat(4,58px)}.seat{width:58px}}
</style>{% block head %}{% endblock %}</head><body>
<header class="top"><a class="brand" href="/">&#128656; {{ t('app_name') }}</a>
<nav>
{% if not user %}<a href="/">{{ t('nav_home') }}</a><a href="/login">{{ t('nav_login') }}</a><a href="/register">{{ t('nav_register') }}</a>
{% elif role=='passenger' %}<a href="/">{{ t('nav_home') }}</a><a href="/my">{{ t('nav_tickets') }}</a><a href="/account">{{ t('nav_account') }}</a>
{% elif role=='conductor' %}<a href="/crew">{{ t('nav_crew') }}</a>
{% elif role=='admin' %}<a href="/admin">Dashboard</a><a href="/admin/fleet">Fleet &amp; staff</a><a href="/admin/trips">Trips</a><a href="/admin/fares">Fares</a><a href="/admin/payments">Payments</a><a href="/settlement">Settlement</a><a href="/tracking">Tracking</a><a href="/audit">Audit</a>
{% elif role=='owner' %}<a href="/owner">My vehicles</a><a href="/settlement">Settlement</a><a href="/tracking">Tracking</a>
{% elif role=='auditor' %}<a href="/audit">Audit log</a><a href="/admin/payments">Payments</a><a href="/settlement">Settlement</a><a href="/tracking">Tracking</a>{% endif %}
<span style="flex:1"></span>
<span class="lang"><a href="/lang/en" class="{{ 'on' if lang=='en' }}">EN</a><a href="/lang/sw" class="{{ 'on' if lang=='sw' }}">SW</a></span>
{% if user %}<span style="opacity:.85">{{ user.full_name }} ({{ role }})</span>
<form method="post" action="/logout" style="display:inline"><input type="hidden" name="_csrf" value="{{ csrf }}"><button class="link">{{ t('nav_logout') }}</button></form>{% endif %}
</nav></header>
<main>{% for c,m in get_flashed_messages(with_categories=true) %}<div class="flash {{ c }}">{{ m }}</div>{% endfor %}
{% block content %}{% endblock %}</main>
<footer>Matatu Ticketing System &middot; times shown in East Africa Time (EAT){% if DEMO %} &middot; demo mode{% endif %}</footer></body></html>"""

TEMPLATES["error.html"] = """{% extends "base.html" %}{% block content %}<div class="card"><h1>Oops</h1><p>{{ msg }}</p><a class="btn" href="/">Home</a></div>{% endblock %}"""

TEMPLATES["login.html"] = """{% extends "base.html" %}{% block content %}
<div class="card" style="max-width:420px;margin:30px auto"><h1>{{ t('login') }}</h1>
<form method="post"><input type="hidden" name="_csrf" value="{{ csrf }}">
<label>{{ t('username') }}</label><input name="username" autocomplete="username" required autofocus>
<label>{{ t('password') }}</label><input name="password" type="password" autocomplete="current-password" required>
<p><button class="btn" style="width:100%">{{ t('login') }}</button></p></form>
<p class="mut"><a href="/register">{{ t('nav_register') }}</a></p></div>
{% if DEMO %}<div class="card" style="max-width:420px;margin:auto"><b>Demo accounts</b> <span class="mut">(password: Demo@1234)</span>
<table><tr><td>wanjiku</td><td>passenger</td></tr><tr><td>conductor1</td><td>crew (vehicle KDX 101A)</td></tr><tr><td>admin</td><td>SACCO admin</td></tr>
<tr><td>owner1</td><td>vehicle owner</td></tr><tr><td>auditor</td><td>read-only</td></tr></table></div>{% endif %}{% endblock %}"""

TEMPLATES["register.html"] = """{% extends "base.html" %}{% block content %}
<div class="card" style="max-width:460px;margin:30px auto"><h1>{{ t('register') }}</h1>
<form method="post"><input type="hidden" name="_csrf" value="{{ csrf }}">
<label>{{ t('full_name') }}</label><input name="full_name" required>
<label>{{ t('username') }}</label><input name="username" required pattern="[A-Za-z0-9_]{3,30}">
<label>{{ t('phone') }}</label><input name="phone" inputmode="tel" placeholder="0712 345 678" required>
<label>{{ t('password') }}</label><input name="password" type="password" minlength="8" required>
<p class="mut" style="font-size:.85rem">Your phone number is stored encrypted and is never shown in full to crews.</p>
<p><button class="btn" style="width:100%">{{ t('register') }}</button></p></form></div>{% endblock %}"""

TEMPLATES["home.html"] = """{% extends "base.html" %}{% block content %}
<h1>{{ t('nav_home') }}</h1>
<form class="card row" method="get"><div><label>{{ t('route') }}</label><select name="route"><option value="">{{ t('all_routes') }}</option>
{% for r in routes %}<option value="{{ r.id }}" {{ 'selected' if r.id==route_id }}>{{ r.name }}</option>{% endfor %}</select></div>
<div><label>{{ t('date') }}</label><input type="date" name="date" value="{{ day }}" min="{{ today }}"></div><button class="btn">{{ t('search') }}</button></form>
{% if not trips %}<div class="card">{{ t('no_trips') }}</div>{% endif %}
{% for x in trips %}<div class="card row" style="align-items:center"><div style="flex:3"><b>{{ x.rname }}</b><br>
<span class="mut">{{ t('departs') }} {{ x.depart_at|eat('%a %d %b, %H:%M') }} &middot; {{ x.plate }}</span></div>
<div><b>{{ x.fare|kes }}</b><br><span class="tag {{ 'peak' if x.peak }}">{{ t('peak') if x.peak else t('offpeak') }}</span></div>
<div>{{ x.left }} {{ t('seats_left') }}</div>
<a class="btn {{ 'sec' if x.left==0 }}" href="/trip/{{ x.id }}" style="flex:0 0 auto">{{ t('select_seat') if user else t('login_to_book') }}</a></div>{% endfor %}
{% endblock %}"""

TEMPLATES["seats.html"] = """{% extends "base.html" %}{% block content %}
<h1>{{ tr.rname }}</h1><p class="mut">{{ t('departs') }} {{ tr.depart_at|eat }} &middot; {{ tr.plate }} &middot; <b>{{ fare|kes }}</b>
<span class="tag {{ 'peak' if peak }}">{{ t('peak') if peak else t('offpeak') }}</span></p>
<div class="card"><h2 class="center">{{ t('select_seat') }}</h2>
<form method="post" action="/trip/{{ tr.id }}/hold"><input type="hidden" name="_csrf" value="{{ csrf }}">
<div class="seatmap"><div class="seat driver">&#129489;</div><div></div>
{% for n in range(1, tr.capacity+1) %}{% set s=states[n] %}<label><input type="radio" name="seat_no" value="{{ n }}" {{ 'disabled' if s in ('taken','held') }} required><span class="seat {{ s }}">{{ n }}</span></label>{% endfor %}</div>
<div class="legend"><span><i></i>{{ t('legend_free') }}</span><span><i style="background:#cfd6d1"></i>{{ t('legend_taken') }}</span><span><i style="background:#ffe9b8"></i>{{ t('legend_held') }}</span><span><i style="background:#cfe8ff"></i>{{ t('legend_mine') }}</span></div>
<p class="center">{% if user and role=='passenger' %}<button class="btn alt">{{ t('hold_seat') }}</button>{% elif not user %}<a class="btn" href="/login?next=/trip/{{ tr.id }}">{{ t('login_to_book') }}</a>{% else %}<span class="mut">Only passengers can book.</span>{% endif %}</p></form></div>{% endblock %}"""

TEMPLATES["pay.html"] = """{% extends "base.html" %}{% block content %}
<h1>{{ t('pay_title') }}</h1>
<div class="card"><b>{{ b.rname }}</b><br><span class="mut">{{ b.depart_at|eat }} &middot; {{ b.plate }} &middot; {{ t('seat') }} {{ b.seat_no }}</span>
<p class="big">{{ b.fare|kes }}</p><p>{{ t('time_left') }} <b id="cd">--:--</b></p>
<form method="post"><input type="hidden" name="_csrf" value="{{ csrf }}"><label>{{ t('phone') }}</label>
<input name="phone" value="{{ phone }}" inputmode="tel" placeholder="0712 345 678" required><p><button class="btn">{{ t('pay_btn') }}</button></p></form></div>
<script>var s={{ secs }};function tick(){var m=Math.floor(s/60),r=s%60;document.getElementById('cd').textContent=m+':'+(r<10?'0':'')+r;
if(s<=0){location.href='/';return}s--;setTimeout(tick,1000)}tick();</script>{% endblock %}"""

TEMPLATES["payment.html"] = """{% extends "base.html" %}{% block content %}
<h1>{{ t('pay_title') }}</h1><div class="card"><b>{{ p.rname }}</b> &middot; {{ t('seat') }} {{ p.seat_no }} &middot; {{ p.amount|kes }}<br><span class="mut">{{ p.depart_at|eat }}</span></div>
{% if p.status=='PENDING' %}<div class="card center"><p class="big">&#128241;</p><p><b>{{ t('stk_sent') }}</b></p><p class="mut">{{ t('waiting') }}</p></div>
{% if DEMO %}<div class="card sim"><b>Sandbox: pretend to be Safaricom</b><p class="mut" style="margin:.2em 0 .6em">Demo only. In production the real M-Pesa callback arrives at /mpesa/callback.</p>
{% for k,l in [('success','PIN entered - success'),('cancel','Customer cancels (1032)'),('insufficient','Insufficient balance (1)'),('wrongpin','Wrong PIN (2001)'),('timeout','Phone unreachable (1037)'),('duplicate','Send success callback 3 times'),('lost','Callback never arrives'),('late','Pay after hold expired')] %}
<form method="post" action="/sim/{{ p.id }}/{{ k }}" style="display:inline"><input type="hidden" name="_csrf" value="{{ csrf }}"><button class="btn sec sm">{{ l }}</button></form>{% endfor %}</div>{% endif %}
<script>setInterval(function(){fetch('/payment/{{ p.id }}/status').then(r=>r.json()).then(j=>{if(j.status!=='PENDING')location.reload()})},3000)</script>
{% elif p.status=='SUCCESS' %}<div class="flash ok">{{ t('paid_ok') }}</div><p>M-Pesa receipt: <b>{{ p.receipt }}</b></p>{% if tk %}<a class="btn alt" href="/ticket/{{ tk.id }}">{{ t('show_ticket') }}</a>{% endif %}
{% elif p.status=='FAILED' %}<div class="flash bad">{{ reason }}</div><a class="btn" href="/">{{ t('try_again') }}</a>
{% elif p.status=='REFUND_DUE' %}<div class="flash info">{{ t('refund_due') }}</div>
{% else %}<div class="flash info">Status: {{ p.status }}</div>{% endif %}{% endblock %}"""

TEMPLATES["ticket.html"] = """{% extends "base.html" %}{% block content %}
<div class="card center" style="max-width:480px;margin:16px auto"><h1>{{ tk.rname }}</h1>
<p class="mut">{{ tk.depart_at|eat }} &middot; {{ tk.plate }}</p><p class="big">{{ t('seat') }} {{ tk.seat_no }}</p>
<div class="qr">{% if qr %}{{ qr }}{% endif %}</div>
<p><span class="tag {{ 'ok' if tk.status=='VALID' else 'bad' }}">{{ tk.status }}</span></p><p>{{ t('ticket_note') }}</p>
<p class="mut" style="font-size:.85rem">{{ t('valid_window') }}: {{ tk.valid_from|eat('%d %b %H:%M') }} - {{ tk.valid_to|eat('%d %b %H:%M') }}<br>{{ tk.amount|kes }} &middot; {{ tk.receipt }} &middot; {{ phone }}</p>
<details><summary class="mut">{{ t('ticket_code') }}</summary><div class="code">{{ tk.code }}</div></details>
{% if not qr %}<p class="mut" style="font-size:.8rem">Install the optional qrcode package (pip install qrcode) to draw the QR image.</p>{% endif %}</div>{% endblock %}"""

TEMPLATES["my.html"] = """{% extends "base.html" %}{% block content %}<h1>{{ t('my_tickets') }}</h1>
{% for h in holds %}<div class="card row" style="align-items:center"><div style="flex:3"><b>{{ h.rname }}</b> &middot; {{ t('seat') }} {{ h.seat_no }}<br><span class="mut">{{ h.depart_at|eat }}</span></div><span class="tag warn">{{ t('legend_held') }}</span><a class="btn alt sm" href="/pay/{{ h.id }}">{{ t('pay_title') }}</a></div>{% endfor %}
{% if due %}<div class="flash info">{{ t('refund_due') }}</div>{% endif %}
{% for r in rows %}<div class="card row" style="align-items:center"><div style="flex:3"><b>{{ r.rname }}</b> &middot; {{ t('seat') }} {{ r.seat_no }}<br><span class="mut">{{ r.depart_at|eat }} &middot; {{ r.plate }}</span></div>
<span class="tag {{ 'ok' if r.status=='VALID' else 'bad' }}">{{ r.status }}</span><a class="btn sm" href="/ticket/{{ r.id }}">{{ t('show_ticket') }}</a></div>
{% else %}{% if not holds %}<div class="card">{{ t('no_tickets') }}</div>{% endif %}{% endfor %}{% endblock %}"""

TEMPLATES["account.html"] = """{% extends "base.html" %}{% block content %}<h1>{{ t('nav_account') }}</h1>
<div class="card"><p><b>{{ user.full_name }}</b> ({{ user.username }})<br>{{ t('phone') }}: {{ phone }}</p>
<a class="btn sec" href="/account/export">Download my data</a></div>
<div class="card"><h2>Delete my personal data</h2><p class="mut">Your name and phone number are erased. Ticket and payment records stay (without your details) for accounting.</p>
<form method="post"><input type="hidden" name="_csrf" value="{{ csrf }}"><label>{{ t('password') }}</label><input type="password" name="password" required>
<p><button class="btn red">Delete my data</button></p></form></div>{% endblock %}"""

TEMPLATES["crew.html"] = """{% extends "base.html" %}{% block content %}
{% if not veh %}<div class="card">{{ t('no_vehicle') }}</div>{% else %}
<h1>{{ veh.plate }} <span class="mut" style="font-size:1rem">{{ veh.rname }}</span></h1>
<form method="get" class="card row"><div><label>{{ t('departs') }}</label><select name="trip" onchange="this.form.submit()">
{% for x in trips %}<option value="{{ x.id }}" {{ 'selected' if x.id==sel }}>{{ x.depart_at|eat }}</option>{% endfor %}</select></div>
<div class="center"><span class="tag ok">&#10003; {{ stats.ok or 0 }}</span> <span class="tag bad">&#10007; {{ stats.bad or 0 }}</span></div></form>
<div class="card"><h2>{{ t('validate') }}</h2><p class="mut">{{ t('validate_help') }}</p>
<form method="post" action="/crew/validate"><input type="hidden" name="_csrf" value="{{ csrf }}"><input type="hidden" name="trip" value="{{ sel or '' }}">
<textarea name="code" required autofocus placeholder="{{ t('ticket_code') }}"></textarea><p><button class="btn" style="width:100%;font-size:1.2rem">{{ t('validate') }}</button></p></form></div>
{% if sel %}<div class="card"><h2>{{ t('manifest') }} ({{ manifest|length }}/{{ states|length }})</h2><div class="scroll"><table><tr><th>{{ t('seat') }}</th><th>{{ t('phone') }}</th><th></th><th>{{ t('status') }}</th></tr>
{% for m in manifest %}<tr><td><b>{{ m.seat_no }}</b></td><td>{{ m.phone }}</td><td>{{ 'cash' if m.method=='CASH' else 'M-Pesa' }}</td><td><span class="tag {{ 'ok' if m.tstatus=='USED' else 'warn' }}">{{ 'BOARDED' if m.tstatus=='USED' else 'NOT YET' }}</span></td></tr>
{% else %}<tr><td colspan="4" class="mut">-</td></tr>{% endfor %}</table></div></div>
<div class="card"><h2>{{ t('cash_sale') }}</h2><form method="post" action="/crew/cash" class="row"><input type="hidden" name="_csrf" value="{{ csrf }}"><input type="hidden" name="trip" value="{{ sel }}">
<div><label>{{ t('seat') }}</label><select name="seat_no" required>{% for n,s in states.items() %}{% if s=='free' %}<option>{{ n }}</option>{% endif %}{% endfor %}</select></div>
<div><label>{{ t('phone') }} (optional)</label><input name="phone" inputmode="tel"></div><button class="btn alt">{{ t('sell') }}</button></form></div>{% endif %}
<div class="card"><button class="btn sec" onclick="loc()">&#128205; {{ t('share_location') }}</button> <span id="gps" class="mut"></span></div>
<script>function loc(){navigator.geolocation.getCurrentPosition(function(p){fetch('/api/v1/ping',{method:'POST',headers:{'Content-Type':'application/json','X-CSRF':'{{ csrf }}'},
body:JSON.stringify({lat:p.coords.latitude,lon:p.coords.longitude,ts:p.timestamp/1000})}).then(r=>r.json()).then(j=>{document.getElementById('gps').textContent=j.stored?'{{ t("gps_ok") }}':'{{ t("gps_old") }}'})},
function(e){document.getElementById('gps').textContent=e.message})}</script>{% endif %}{% endblock %}"""

TEMPLATES["admin.html"] = """{% extends "base.html" %}{% block content %}<h1>SACCO dashboard</h1>
<div class="grid"><div class="kpi"><b>{{ k.revenue|kes }}</b><span>Collected today</span></div><div class="kpi"><b>{{ k.tickets }}</b><span>Tickets issued today</span></div>
<div class="kpi"><b>{{ k.trips }}</b><span>Trips scheduled today</span></div><div class="kpi"><b>{{ live }}/{{ total }}</b><span>Vehicles reporting location</span></div>
<div class="kpi"><b>{{ k.validations.ok }} / {{ k.validations.bad }}</b><span>Boardings OK / rejected today</span></div>
<div class="kpi"><b>{{ k.pending }}</b><span>Payments pending</span></div></div>
{% if k.attention %}<div class="flash info">{{ k.attention }} payment(s) need attention (late payments / amount mismatch). <a href="/admin/payments">Open payments</a></div>{% endif %}
<h2>Quick links</h2><div class="grid"><a class="btn sec" href="/admin/fleet">Fleet &amp; staff</a><a class="btn sec" href="/admin/trips">Trips</a><a class="btn sec" href="/admin/fares">Fares &amp; surge</a>
<a class="btn sec" href="/admin/payments">Payments &amp; reconciliation</a><a class="btn sec" href="/settlement">Revenue settlement</a><a class="btn sec" href="/tracking">Fleet tracking</a>
<a class="btn sec" href="/audit">Audit log</a><a class="btn sec" href="/admin/gtfs.zip">Export GTFS</a></div>
<h2>REST API (read-only)</h2><div class="card mut"><code>GET /api/v1/routes</code> &middot; <code>GET /api/v1/trips?date=YYYY-MM-DD&amp;route_id=1</code> &middot; <code>GET /api/v1/trips/&lt;id&gt;/seats</code> &middot; <code>POST /mpesa/callback</code></div>{% endblock %}"""

TEMPLATES["admin_fleet.html"] = """{% extends "base.html" %}{% block content %}<h1>Fleet &amp; staff</h1>
<div class="card scroll"><table><tr><th>Plate</th><th>Route</th><th>Seats</th><th>Owner</th><th>Crew login</th><th>Active</th><th></th></tr>
{% for v in vehicles %}<tr><td><b>{{ v.plate }}</b></td><td>{{ v.short_name }}</td><td>{{ v.capacity }}</td><td>{{ v.owner }}</td><td>{{ v.crew or '-' }}</td><td>{{ 'Yes' if v.active else 'No' }}</td>
<td><form method="post"><input type="hidden" name="_csrf" value="{{ csrf }}"><input type="hidden" name="action" value="toggle"><input type="hidden" name="vehicle_id" value="{{ v.id }}"><button class="btn sec sm">{{ 'Deactivate' if v.active else 'Activate' }}</button></form></td></tr>{% endfor %}</table></div>
<div class="card"><h2>Add vehicle</h2><form method="post" class="row"><input type="hidden" name="_csrf" value="{{ csrf }}"><input type="hidden" name="action" value="vehicle">
<div><label>Plate</label><input name="plate" required placeholder="KDA 123B"></div><div><label>Seats</label><input name="capacity" type="number" min="1" max="60" value="14" required></div>
<div><label>Route</label><select name="route_id">{% for r in routes %}<option value="{{ r.id }}">{{ r.short_name }} - {{ r.name }}</option>{% endfor %}</select></div>
<div><label>Owner</label><select name="owner_id">{% for o in owners %}<option value="{{ o.id }}">{{ o.full_name }}</option>{% endfor %}</select></div><button class="btn">Add</button></form></div>
<div class="card"><h2>Add staff login</h2><form method="post" class="row"><input type="hidden" name="_csrf" value="{{ csrf }}"><input type="hidden" name="action" value="user">
<div><label>Full name</label><input name="full_name" required></div><div><label>Username</label><input name="username" required></div><div><label>Password (8+)</label><input name="password" type="password" minlength="8" required></div>
<div><label>Role</label><select name="role"><option>conductor</option><option>owner</option><option>auditor</option><option>admin</option></select></div>
<div><label>Vehicle (conductors)</label><select name="vehicle_id"><option value="">-</option>{% for v in vehicles %}<option value="{{ v.id }}">{{ v.plate }}</option>{% endfor %}</select></div><button class="btn">Add</button></form></div>
<div class="card scroll"><h2>Staff accounts</h2><table><tr><th>Username</th><th>Name</th><th>Role</th></tr>{% for u in users %}<tr><td>{{ u.username }}</td><td>{{ u.full_name }}</td><td>{{ u.role }}</td></tr>{% endfor %}</table></div>{% endblock %}"""

TEMPLATES["admin_trips.html"] = """{% extends "base.html" %}{% block content %}<h1>Trips</h1>
<div class="card"><form method="post" class="row"><input type="hidden" name="_csrf" value="{{ csrf }}"><input type="hidden" name="action" value="create">
<div><label>Vehicle</label><select name="vehicle_id">{% for v in vehicles %}<option value="{{ v.id }}">{{ v.plate }} ({{ v.short_name }})</option>{% endfor %}</select></div>
<div><label>Departure (EAT)</label><input type="datetime-local" name="depart" required></div><button class="btn">Create trip</button></form></div>
<div class="card scroll"><table><tr><th>Departs</th><th>Route</th><th>Vehicle</th><th class="n">Sold</th><th>Status</th><th></th></tr>
{% for x in trips %}<tr><td>{{ x.depart_at|eat }}</td><td>{{ x.short_name }}</td><td>{{ x.plate }}</td><td class="n">{{ x.sold }}</td><td>{{ x.status }}</td><td>
{% if x.status=='SCHEDULED' %}<form method="post"><input type="hidden" name="_csrf" value="{{ csrf }}"><input type="hidden" name="action" value="cancel"><input type="hidden" name="trip_id" value="{{ x.id }}"><button class="btn sec sm">Cancel</button></form>{% endif %}</td></tr>{% endfor %}</table></div>{% endblock %}"""

TEMPLATES["admin_fares.html"] = """{% extends "base.html" %}{% block content %}<h1>Fares &amp; settlement rules</h1>
<form method="post"><input type="hidden" name="_csrf" value="{{ csrf }}"><div class="card scroll"><table><tr><th>Route</th><th>Standard (KES)</th><th>Peak 06-09 &amp; 16-20 (KES)</th></tr>
{% for f in fares %}<tr><td>{{ f.name }}</td><td><input name="off_{{ f.route_id }}" type="number" min="0" value="{{ f.off_peak }}"></td><td><input name="peak_{{ f.route_id }}" type="number" min="0" value="{{ f.peak }}"></td></tr>{% endfor %}</table></div>
<div class="card"><h2>Surge pricing (weather / traffic disruption)</h2><label>Multiplier % (100 = normal, max 200)</label><input name="surge_pct" type="number" min="100" max="200" value="{{ s.surge_pct }}"></div>
<div class="card"><h2>Revenue split (must total 100%)</h2><div class="row"><div><label>Owner %</label><input name="split_owner" type="number" min="0" max="100" value="{{ s.split_owner }}"></div>
<div><label>SACCO %</label><input name="split_sacco" type="number" min="0" max="100" value="{{ s.split_sacco }}"></div><div><label>Crew %</label><input name="split_crew" type="number" min="0" max="100" value="{{ s.split_crew }}"></div></div>
<p class="mut">Rounding rule: SACCO and crew amounts are rounded down to whole shillings; the owner receives the remainder, so the three parts always add up exactly.</p></div>
<button class="btn">Save</button></form>{% endblock %}"""

TEMPLATES["admin_payments.html"] = """{% extends "base.html" %}{% block content %}<h1>Payments &amp; reconciliation</h1>
<div class="card"><h2>Reconciliation checks</h2>
<p>Ledger total <b>{{ rec.ledger|kes }}</b> vs payments that should be in the ledger <b>{{ rec.expect|kes }}</b> &rarr;
{% if rec.variance==0 %}<span class="tag ok">no variance</span>{% else %}<span class="tag bad">VARIANCE {{ rec.variance|kes }}</span>{% endif %}</p>
<p>Paid but no ticket: <span class="tag {{ 'bad' if rec.no_ticket else 'ok' }}">{{ rec.no_ticket|length }}</span> &middot; Pending over 2 minutes (callback missing): <span class="tag {{ 'warn' if rec.pending else 'ok' }}">{{ rec.pending|length }}</span>
&middot; Need attention (late payment / amount mismatch): <span class="tag {{ 'warn' if rec.attention else 'ok' }}">{{ rec.attention|length }}</span></p></div>
<div class="card scroll"><table><tr><th>#</th><th>When</th><th>Route</th><th>Vehicle</th><th>Seat</th><th class="n">Amount</th><th>Phone</th><th>Method</th><th>Status</th><th>Receipt</th>{% if role=='admin' %}<th></th>{% endif %}</tr>
{% for p in rows %}<tr><td>{{ p.id }}</td><td>{{ p.created_at|eat('%d %b %H:%M') }}</td><td>{{ p.short_name }}</td><td>{{ p.plate }}</td><td>{{ p.seat_no }}</td><td class="n">{{ p.amount|kes }}</td><td>{{ p.masked }}</td><td>{{ p.method }}</td>
<td><span class="tag {{ 'ok' if p.status=='SUCCESS' else ('bad' if p.status in ('FAILED','REVIEW') else 'warn') }}">{{ p.status }}</span></td><td>{{ p.receipt or '' }}</td>
{% if role=='admin' %}<td>{% if p.status=='PENDING' %}<form method="post" action="/admin/payments/{{ p.id }}/query"><input type="hidden" name="_csrf" value="{{ csrf }}"><button class="btn sec sm">Query status</button></form>
{% elif p.status in ('SUCCESS','REFUND_DUE') %}<form method="post" action="/admin/payments/{{ p.id }}/refund" onsubmit="return confirm('Refund this payment?')"><input type="hidden" name="_csrf" value="{{ csrf }}"><button class="btn red sm">Refund</button></form>{% endif %}</td>{% endif %}</tr>{% endfor %}</table></div>{% endblock %}"""

TEMPLATES["settlement.html"] = """{% extends "base.html" %}{% block content %}<h1>Revenue settlement</h1>
<form method="get" class="card row"><div><label>Day (EAT)</label><input type="date" name="day" value="{{ day }}"></div><button class="btn sec">Show</button>
<span class="mut" style="flex:2">Split: owner {{ split.owner }}% &middot; SACCO {{ split.sacco }}% &middot; crew {{ split.crew }}%</span></form>
<div class="card scroll"><table><tr><th>Vehicle</th><th>Owner</th><th class="n">Collected</th><th class="n">Owner</th><th class="n">SACCO</th><th class="n">Crew</th><th>State</th></tr>
{% for r in rows %}<tr><td><b>{{ r.plate }}</b></td><td>{{ r.owner }}</td><td class="n">{{ r.collected|kes }}</td><td class="n">{{ r.owner_amt|kes }}</td><td class="n">{{ r.sacco_amt|kes }}</td><td class="n">{{ r.crew_amt|kes }}</td>
<td>{% if r.settled %}<span class="tag ok">settled</span>{% elif r.n %}<span class="tag warn">open</span>{% else %}<span class="mut">no sales</span>{% endif %}</td></tr>{% endfor %}
<tr><th>Total</th><th></th><th class="n">{{ totals.collected|kes }}</th><th class="n">{{ totals.owner_amt|kes }}</th><th class="n">{{ totals.sacco_amt|kes }}</th><th class="n">{{ totals.crew_amt|kes }}</th><th>{{ 'OK' if totals.owner_amt+totals.sacco_amt+totals.crew_amt==totals.collected else 'MISMATCH' }}</th></tr></table></div>
{% if role=='admin' %}<form method="post"><input type="hidden" name="_csrf" value="{{ csrf }}"><input type="hidden" name="day" value="{{ day }}"><button class="btn" onclick="return confirm('Settle this day? Settled days cannot be edited.')">Run settlement for {{ day }}</button></form>
<p class="mut">Settlements are permanent. Later refunds appear as reversing entries on the day they happen.</p>{% endif %}
{% if rec %}<p class="mut">Unexplained ledger variance: <b>{{ rec.variance|kes }}</b> (see Payments).</p>{% endif %}{% endblock %}"""

TEMPLATES["owner.html"] = """{% extends "base.html" %}{% block content %}<h1>My vehicles</h1>
<div class="card scroll"><table><tr><th>Plate</th><th>Route</th><th class="n">Collected today</th><th>Location</th></tr>
{% for v in vs %}<tr><td><b>{{ v.plate }}</b></td><td>{{ v.short_name }}</td><td class="n">{{ v.today|kes }}</td><td>{% set l=last.get(v.id) %}{% if l %}<span class="tag {{ 'ok' if l.live else 'warn' }}">{{ 'live' if l.live else 'last seen' }}</span> {{ l.seen }}{% endif %}</td></tr>{% endfor %}</table></div>
<h2>Settlement history</h2><div class="card scroll"><table><tr><th>Day</th><th>Vehicle</th><th class="n">Collected</th><th class="n">Your share</th></tr>
{% for h in hist %}<tr><td>{{ h.day }}</td><td>{{ h.plate }}</td><td class="n">{{ h.collected|kes }}</td><td class="n"><b>{{ h.owner_amt|kes }}</b></td></tr>{% else %}<tr><td colspan="4" class="mut">No settlements yet.</td></tr>{% endfor %}</table></div>
<p class="mut">Owners can view their own vehicles only. Fares are set by the SACCO.</p>{% endblock %}"""

TEMPLATES["audit.html"] = """{% extends "base.html" %}{% block content %}<h1>Audit log <span class="tag">append-only</span></h1>
<form class="card row" method="get"><div><label>Search</label><input name="q" value="{{ q }}" placeholder="action, user or detail"></div><button class="btn sec">Search</button></form>
<div class="card scroll"><table><tr><th>When (EAT)</th><th>Who</th><th>What</th><th>Detail</th><th>Device</th></tr>
{% for r in rows %}<tr><td>{{ r.at|eat('%d %b %H:%M:%S') }}</td><td>{{ r.username }}</td><td><b>{{ r.action }}</b></td><td>{{ r.detail }}</td><td class="mut" style="font-size:.75rem">{{ r.device }}</td></tr>{% endfor %}</table></div>
<h2>Latest ticket validations</h2><div class="card scroll"><table><tr><th>When</th><th>Ticket (payment #)</th><th>Result</th><th>Reason</th></tr>
{% for v in val %}<tr><td>{{ v.at|eat('%d %b %H:%M:%S') }}</td><td>{{ v.ticket_payment_id or '-' }}</td><td><span class="tag {{ 'ok' if v.result=='VALID' else 'bad' }}">{{ v.result }}</span></td><td>{{ v.reason }}</td></tr>{% endfor %}</table></div>{% endblock %}"""

TEMPLATES["tracking.html"] = """{% extends "base.html" %}{% block head %}<meta http-equiv="refresh" content="30">{% endblock %}{% block content %}
<h1>Fleet tracking</h1>
<div class="card"><svg viewBox="0 0 {{ m.W }} {{ m.H }}" style="width:100%;height:auto;background:#eef3ee;border-radius:10px">
{% for r in m.routes %}<polyline points="{{ r.pts|join(' ') }}" fill="none" stroke="{{ r.color }}" stroke-width="3" opacity=".55"/>
{% for s in r.stops %}<circle cx="{{ s.x }}" cy="{{ s.y }}" r="{{ 5 if s.end else 3 }}" fill="#fff" stroke="{{ r.color }}" stroke-width="2"/>{% if s.end %}<text x="{{ s.x+8 }}" y="{{ s.y-6 }}" font-size="12" fill="#333">{{ s.name }}</text>{% endif %}{% endfor %}{% endfor %}
{% for v in m.vehicles if v.x is not none %}<circle cx="{{ v.x }}" cy="{{ v.y }}" r="9" fill="{{ v.color if v.live else '#fff' }}" stroke="{{ v.color }}" stroke-width="3" opacity="{{ 1 if v.live else 0.8 }}"/>
<text x="{{ v.x+12 }}" y="{{ v.y+4 }}" font-size="11" font-weight="700" fill="#111">{{ v.plate }}{% if not v.live %} ({{ v.seen }}){% endif %}</text>{% endfor %}</svg>
<p class="mut">Schematic map (no internet needed). Filled dot = live; hollow dot = no signal, showing last seen time. Refreshes every 30 s.</p></div>
<div class="card scroll"><table><tr><th>Vehicle</th><th>Route</th><th>Location</th></tr>{% for v in m.vehicles %}<tr><td><b>{{ v.plate }}</b></td><td>{{ v.route }}</td><td><span class="tag {{ 'ok' if v.live else 'warn' }}">{{ 'live' if v.live else 'last seen' }}</span> {{ v.seen }}</td></tr>{% endfor %}</table></div>
{% if role=='admin' %}<form method="post" action="/tracking/simulate"><input type="hidden" name="_csrf" value="{{ csrf }}"><button class="btn alt">Simulate vehicle movement</button></form>{% endif %}{% endblock %}"""

app.jinja_env.loader = DictLoader(TEMPLATES)

if __name__ == "__main__":
    init_db()
    print("Matatu Ticketing UI -> http://127.0.0.1:5001   (demo password for all accounts: %s)" % DEMO_PASSWORD)
    app.run(host="127.0.0.1", port=5001, debug=False)