import sqlite3
import threading
import time
from datetime import datetime


DATABASE = "Q5/code/bookings.db"


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
            status TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
    """)

    connection.commit()
    connection.close()


def book_seat(trip_id, seat_no, passenger_id):
    """Naive booking function that contains a race condition."""
    connection = sqlite3.connect(DATABASE)

    cursor = connection.execute(
        """
        SELECT COUNT(*) FROM bookings
        WHERE trip_id = ? AND seat_no = ?
        """,
        (trip_id, seat_no)
    )

    seat_count = cursor.fetchone()[0]

    if seat_count > 0:
        print(f"Passenger {passenger_id}: Seat already taken.")
        connection.close()
        return

    # Simulate a delay between checking and inserting.
    time.sleep(0.2)

    connection.execute(
        """
        INSERT INTO bookings
        (trip_id, seat_no, passenger_id, status, created_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        (
            trip_id,
            seat_no,
            passenger_id,
            "CONFIRMED",
            datetime.now().isoformat()
        )
    )

    connection.commit()
    connection.close()

    print(f"Passenger {passenger_id}: Seat {seat_no} booked successfully!")


if __name__ == "__main__":
    setup_database()

    thread1 = threading.Thread(
        target=book_seat,
        args=(1, 14, 101)
    )

    thread2 = threading.Thread(
        target=book_seat,
        args=(1, 14, 102)
    )

    print("Two passengers are attempting to book seat 14...\n")

    thread1.start()
    thread2.start()

    thread1.join()
    thread2.join()

    connection = sqlite3.connect(DATABASE)

    bookings = connection.execute(
        "SELECT * FROM bookings"
    ).fetchall()

    print("\nFinal bookings in the database:")

    for booking in bookings:
        print(booking)

    print(f"\nTotal bookings: {len(bookings)}")

    connection.close()