import sqlite3
import threading
from datetime import date, datetime, timedelta


DATABASE = "Q5/code/bookings.db"
VEHICLE_CAPACITY = 14


def setup_database():
    """Create a fresh bookings table for the demonstration."""
    connection = sqlite3.connect(DATABASE)

    connection.execute("DROP TABLE IF EXISTS bookings")

    connection.execute("""
        CREATE TABLE bookings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            trip_id INTEGER NOT NULL,
            seat_no INTEGER NOT NULL,
            passenger_id INTEGER NOT NULL,
            trip_date TEXT NOT NULL,
            status TEXT NOT NULL,
            created_at TEXT NOT NULL,
            expires_at TEXT,
            UNIQUE(trip_id, seat_no)
        )
    """)

    connection.commit()
    connection.close()


def validate_booking(seat_no, passenger_id, trip_date):
    """Validate booking details before inserting a booking."""

    if not isinstance(seat_no, int) or isinstance(seat_no, bool):
        return "Invalid seat number."

    if seat_no < 1 or seat_no > VEHICLE_CAPACITY:
        return f"Invalid seat number. Choose between 1 and {VEHICLE_CAPACITY}."

    if passenger_id is None:
        return "Passenger ID cannot be empty."

    if isinstance(passenger_id, str) and not passenger_id.strip():
        return "Passenger ID cannot be empty."

    try:
        departure_date = date.fromisoformat(trip_date)
    except (ValueError, TypeError):
        return "Invalid trip date. Use YYYY-MM-DD."

    if departure_date < date.today():
        return "Trip date cannot be in the past."

    return None


def release_expired_holds():
    """Release pending bookings whose five-minute hold has expired."""

    connection = sqlite3.connect(DATABASE)

    try:
        current_time = datetime.now().isoformat()

        cursor = connection.execute(
            """
            DELETE FROM bookings
            WHERE status = 'PENDING'
            AND expires_at <= ?
            """,
            (current_time,)
        )

        released = cursor.rowcount
        connection.commit()

        print(f"Expired seat holds released: {released}")

        return released

    finally:
        connection.close()


def book_seat(trip_id, seat_no, passenger_id, trip_date=None):
    """Book a seat and handle invalid or duplicate booking attempts."""

    if trip_date is None:
        trip_date = date.today().isoformat()

    error = validate_booking(seat_no, passenger_id, trip_date)

    if error:
        print(f"Passenger {passenger_id}: {error}")
        return

    release_expired_holds()

    connection = sqlite3.connect(DATABASE)

    try:
        connection.execute(
            """
            INSERT INTO bookings
            (trip_id, seat_no, passenger_id, trip_date,
             status, created_at, expires_at)
            VALUES (?, ?, ?, ?, ?, ?, NULL)
            """,
            (
                trip_id,
                seat_no,
                passenger_id,
                trip_date,
                "CONFIRMED",
                datetime.now().isoformat()
            )
        )

        connection.commit()

        print(
            f"Passenger {passenger_id}: "
            f"Seat {seat_no} booked successfully!"
        )

    except sqlite3.IntegrityError:
        connection.rollback()
        print(f"Passenger {passenger_id}: Seat already taken.")

    finally:
        connection.close()


def hold_seat(trip_id, seat_no, passenger_id, trip_date=None):
    """Temporarily hold a seat for five minutes."""

    if trip_date is None:
        trip_date = date.today().isoformat()

    error = validate_booking(seat_no, passenger_id, trip_date)

    if error:
        print(f"Passenger {passenger_id}: {error}")
        return

    release_expired_holds()

    connection = sqlite3.connect(DATABASE)

    try:
        current_time = datetime.now()
        expiry_time = current_time + timedelta(minutes=5)

        connection.execute(
            """
            INSERT INTO bookings
            (trip_id, seat_no, passenger_id, trip_date,
             status, created_at, expires_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                trip_id,
                seat_no,
                passenger_id,
                trip_date,
                "PENDING",
                current_time.isoformat(),
                expiry_time.isoformat()
            )
        )

        connection.commit()

        print(
            f"Passenger {passenger_id}: "
            f"Seat {seat_no} held for five minutes."
        )

        print(f"Hold expires at: {expiry_time}")

    except sqlite3.IntegrityError:
        connection.rollback()
        print(f"Passenger {passenger_id}: Seat already taken.")

    finally:
        connection.close()


def confirm_payment(trip_id, seat_no, passenger_id):
    """Confirm a pending booking if its hold has not expired."""

    connection = sqlite3.connect(DATABASE)

    try:
        current_time = datetime.now().isoformat()

        cursor = connection.execute(
            """
            UPDATE bookings
            SET status = 'CONFIRMED', expires_at = NULL
            WHERE trip_id = ?
            AND seat_no = ?
            AND passenger_id = ?
            AND status = 'PENDING'
            AND expires_at > ?
            """,
            (trip_id, seat_no, passenger_id, current_time)
        )

        connection.commit()

        if cursor.rowcount == 1:
            print(f"Passenger {passenger_id}: Payment confirmed!")
            return True

        print("Payment cannot be confirmed. Hold expired or booking not found.")
        return False

    finally:
        connection.close()


if __name__ == "__main__":
    setup_database()

    print("TWO PASSENGERS BOOKING THE SAME SEAT\n")

    thread1 = threading.Thread(
        target=book_seat,
        args=(1, 14, 101)
    )

    thread2 = threading.Thread(
        target=book_seat,
        args=(1, 14, 102)
    )

    thread1.start()
    thread2.start()

    thread1.join()
    thread2.join()

    print("\nTESTING INVALID BOOKINGS\n")

    book_seat(1, 15, 103)
    book_seat(1, 5, 104, "2020-01-01")
    book_seat(1, 6, "")

    print("\nTESTING FIVE-MINUTE SEAT HOLD\n")

    hold_seat(1, 10, 105)

    print("\nSIMULATING AN EXPIRED HOLD\n")

    connection = sqlite3.connect(DATABASE)

    expired_time = (
        datetime.now() - timedelta(seconds=1)
    ).isoformat()

    connection.execute(
        """
        UPDATE bookings
        SET expires_at = ?
        WHERE trip_id = 1
        AND seat_no = 10
        AND status = 'PENDING'
        """,
        (expired_time,)
    )

    connection.commit()
    connection.close()

    release_expired_holds()

    print("\nTESTING WHETHER THE RELEASED SEAT IS AVAILABLE\n")

    book_seat(1, 10, 106)

    connection = sqlite3.connect(DATABASE)

    bookings = connection.execute(
        "SELECT * FROM bookings"
    ).fetchall()

    print("\nFINAL BOOKINGS IN THE DATABASE:")

    for booking in bookings:
        print(booking)

    print(f"\nTotal bookings: {len(bookings)}")

    connection.close()