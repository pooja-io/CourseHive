from getpass import getpass
from werkzeug.security import generate_password_hash
from db import get_db_connection


def create_admin():
    name = input("Enter admin name: ").strip()
    email = input("Enter admin email: ").strip().lower()

    if not name or not email:
        print("Name and email cannot be empty.")
        return

    password = getpass("Create admin password: ")
    confirm_password = getpass("Confirm admin password: ")

    if len(password) < 8:
        print("Password must contain at least 8 characters.")
        return

    if password != confirm_password:
        print("Passwords do not match.")
        return

    connection = None
    cursor = None

    try:
        connection = get_db_connection()
        cursor = connection.cursor()

        cursor.execute(
            "SELECT admin_id FROM admins WHERE email = %s",
            (email,)
        )

        if cursor.fetchone():
            print("An admin account with this email already exists.")
            return

        password_hash = generate_password_hash(password)

        cursor.execute(
            """
            INSERT INTO admins (name, email, password)
            VALUES (%s, %s, %s)
            """,
            (name, email, password_hash)
        )

        connection.commit()

        print("Admin account created successfully!")
        print("You can now log in through the CourseHive admin login page.")

    except Exception as error:
        if connection:
            connection.rollback()

        print("Could not create the admin account.")
        print("Error:", error)

    finally:
        if cursor:
            cursor.close()

        if connection and connection.is_connected():
            connection.close()


if __name__ == "__main__":
    create_admin()