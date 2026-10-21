from flask import Flask, render_template
from db import get_db_connection

app = Flask(__name__)


@app.route("/")
def home():
    courses = []
    connection = None
    cursor = None

    try:
        connection = get_db_connection()
        cursor = connection.cursor(dictionary=True)

        query = """
            SELECT
                courses.course_id,
                courses.course_name,
                courses.description,
                courses.category,
                courses.difficulty,
                courses.duration_hours,
                instructors.name AS instructor_name
            FROM courses
            LEFT JOIN instructors
                ON courses.instructor_id = instructors.instructor_id
            ORDER BY courses.course_id DESC
        """

        cursor.execute(query)
        courses = cursor.fetchall()

    except Exception as error:
        print("Database error:", error)

    finally:
        if cursor is not None:
            cursor.close()

        if connection is not None and connection.is_connected():
            connection.close()

    return render_template("index.html", courses=courses)


@app.route("/health")
def health():
    return {"status": "CourseHive application is running"}


if __name__ == "__main__":
    app.run(debug=True)