from flask import (
    Flask, render_template, request, redirect,
    url_for, session, flash, abort
)
from werkzeug.security import generate_password_hash, check_password_hash
from functools import wraps
from db import get_db_connection
import os
import secrets
import io
import base64
import matplotlib

matplotlib.use("Agg")

import matplotlib.pyplot as plt
from urllib.parse import urlparse, parse_qs
import re
import pandas as pd
import numpy as np

def get_youtube_embed_url(youtube_url):
    parsed_url = urlparse(youtube_url)
    hostname = parsed_url.hostname or ""

    video_id = None

    if hostname in ("www.youtube.com", "youtube.com", "m.youtube.com"):
        video_id = parse_qs(parsed_url.query).get("v", [None])[0]

        if not video_id and parsed_url.path.startswith("/embed/"):
            video_id = parsed_url.path.split("/")[2]

    elif hostname == "youtu.be":
        video_id = parsed_url.path.strip("/").split("/")[0]

    if video_id and re.fullmatch(r"[A-Za-z0-9_-]{11}", video_id):
        return f"https://www.youtube-nocookie.com/embed/{video_id}"

    return None

app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET_KEY") or secrets.token_hex(32)


# --------------------------------------------------
# DATABASE HELPER
# --------------------------------------------------

def fetch_one(query, params=()):
    connection = get_db_connection()
    cursor = connection.cursor(dictionary=True)

    try:
        cursor.execute(query, params)
        return cursor.fetchone()
    finally:
        cursor.close()
        connection.close()


def fetch_all(query, params=()):
    connection = get_db_connection()
    cursor = connection.cursor(dictionary=True)

    try:
        cursor.execute(query, params)
        return cursor.fetchall()
    finally:
        cursor.close()
        connection.close()


def execute_query(query, params=()):
    connection = get_db_connection()
    cursor = connection.cursor()

    try:
        cursor.execute(query, params)
        connection.commit()
        return cursor.lastrowid
    except Exception:
        connection.rollback()
        raise
    finally:
        cursor.close()
        connection.close()

# --------------------------------------------------
# WEIGHTED COURSE PROGRESS CALCULATION
# --------------------------------------------------

def calculate_course_progress(student_id, course_id):

    # Count total lessons in the course
    total_lessons = fetch_one("""
        SELECT COUNT(*) AS total
        FROM lessons
        WHERE course_id = %s
    """, (course_id,))["total"]

    # Count completed lessons for this student
    completed_lessons = fetch_one("""
        SELECT COUNT(*) AS total
        FROM lesson_progress lp
        JOIN lessons l ON lp.lesson_id = l.lesson_id
        WHERE lp.student_id = %s
        AND l.course_id = %s
        AND lp.completed = TRUE
    """, (student_id, course_id))["total"]

    # Count total assignments in the course
    total_assignments = fetch_one("""
        SELECT COUNT(*) AS total
        FROM assignments
        WHERE course_id = %s
    """, (course_id,))["total"]

    # Count assignments submitted by this student
    submitted_assignments = fetch_one("""
        SELECT COUNT(*) AS total
        FROM assignment_submissions sub
        JOIN assignments a
            ON sub.assignment_id = a.assignment_id
        WHERE sub.student_id = %s
        AND a.course_id = %s
    """, (student_id, course_id))["total"]

    # Count total quizzes in the course
    total_quizzes = fetch_one("""
        SELECT COUNT(*) AS total
        FROM quizzes
        WHERE course_id = %s
    """, (course_id,))["total"]

    # Count quizzes with at least one submitted attempt
    completed_quizzes = fetch_one("""
        SELECT COUNT(DISTINCT qa.quiz_id) AS total
        FROM quiz_attempts qa
        JOIN quizzes q ON qa.quiz_id = q.quiz_id
        WHERE qa.student_id = %s
        AND q.course_id = %s
        AND qa.submitted_at IS NOT NULL
    """, (student_id, course_id))["total"]

    # Define the weights
    weights = {
        "lessons": 50,
        "assignments": 25,
        "quizzes": 25
    }

    progress = 0
    available_weight = 0

    # Lesson contribution
    if total_lessons > 0:
        progress += (
            completed_lessons / total_lessons
        ) * weights["lessons"]

        available_weight += weights["lessons"]

    # Assignment contribution
    if total_assignments > 0:
        progress += (
            submitted_assignments / total_assignments
        ) * weights["assignments"]

        available_weight += weights["assignments"]

    # Quiz contribution
    if total_quizzes > 0:
        progress += (
            completed_quizzes / total_quizzes
        ) * weights["quizzes"]

        available_weight += weights["quizzes"]

    # Normalize the weights if an activity category is unavailable
    if available_weight > 0:
        progress = round(progress * 100 / available_weight)
    else:
        progress = 0

    # Keep progress within 0–100
    progress = max(0, min(100, progress))

    # Determine course status
    if progress == 100:
        status = "Completed"
    elif progress > 0:
        status = "In Progress"
    else:
        status = "Enrolled"

    # Save the updated progress
    execute_query("""
        UPDATE enrollments
        SET progress = %s, status = %s
        WHERE student_id = %s
        AND course_id = %s
    """, (
        progress,
        status,
        student_id,
        course_id
    ))

    return progress


# --------------------------------------------------
# LOGIN PROTECTION
# --------------------------------------------------

def login_required(role):
    def decorator(view_function):
        @wraps(view_function)
        def wrapper(*args, **kwargs):
            if session.get("user_role") != role:
                flash("Please log in with the correct account.", "error")
                return redirect(url_for(f"{role}_login"))

            return view_function(*args, **kwargs)

        return wrapper
    return decorator



# --------------------------------------------------
# HOME PAGE
# --------------------------------------------------

@app.route("/")
def home():
    try:
        courses = fetch_all("""
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
        """)

        # Get the courses enrolled by the logged-in student
        enrolled_course_ids = []

        if session.get("user_role") == "student":
            student_id = session.get("user_id")

            enrolled_courses = fetch_all("""
                SELECT course_id
                FROM enrollments
                WHERE student_id = %s
            """, (student_id,))

            enrolled_course_ids = [
                enrollment["course_id"]
                for enrollment in enrolled_courses
            ]

    except Exception as error:
        print("Database error:", error)
        courses = []
        enrolled_course_ids = []

    return render_template(
        "index.html",
        courses=courses,
        enrolled_course_ids=enrolled_course_ids
    )
# --------------------------------------------------
# COURSE DETAILS AND LESSON PROGRESS
# --------------------------------------------------

@app.route("/course/<int:course_id>")
def course_detail(course_id):

    # Get the selected course
    course = fetch_one("""
        SELECT *
        FROM courses
        WHERE course_id = %s
    """, (course_id,))

    if not course:
        abort(404)

    # Get lessons belonging to this course
    lessons = fetch_all("""
        SELECT *
        FROM lessons
        WHERE course_id = %s
        ORDER BY lesson_order
    """, (course_id,))

    # Prepare lesson information
    for lesson in lessons:
        lesson["embed_url"] = get_youtube_embed_url(
            lesson["youtube_url"]
        )
        lesson["completed"] = False

    total_lessons = len(lessons)
    completed_count = 0
    course_progress = 0

    # Get progress for the logged-in student
    if session.get("user_role") == "student":

        student_id = session["user_id"]

        # Check whether the student is enrolled
        enrollment = fetch_one("""
            SELECT enrollment_id
            FROM enrollments
            WHERE student_id = %s
            AND course_id = %s
        """, (student_id, course_id))

        if not enrollment:
    # Allow students to preview lessons without enrolling
            return render_template(
                "course_detail.html",
                course=course,
                lessons=lessons,
                total_lessons=total_lessons,
                completed_count=0,
                course_progress=0,
                is_enrolled=False
            )

        # Get completed lessons
        completed_lessons = fetch_all("""
            SELECT lp.lesson_id
            FROM lesson_progress lp
            JOIN lessons l
                ON l.lesson_id = lp.lesson_id
            WHERE lp.student_id = %s
            AND l.course_id = %s
            AND lp.completed = TRUE
        """, (student_id, course_id))

        completed_ids = {
            item["lesson_id"]
            for item in completed_lessons
        }

        # Update lesson completion status
        for lesson in lessons:
            if lesson["lesson_id"] in completed_ids:
                lesson["completed"] = True

        completed_count = len(completed_ids)

        # Calculate progress percentage
        if total_lessons > 0:
            course_progress = round(
                completed_count * 100 / total_lessons
            )

    return render_template(
        "course_detail.html",
        course=course,
        lessons=lessons,
        total_lessons=total_lessons,
        completed_count=completed_count,
        course_progress=course_progress
    )

@app.route("/health")
def health():
    return {"status": "CourseHive application is running"}


# --------------------------------------------------
# ADD LESSON
# --------------------------------------------------

@app.route("/add-lesson", methods=["GET", "POST"])
def add_lesson():

    # Allow only admins and mentors
    role = session.get("user_role")

    if role not in ("admin", "mentor"):
        flash("Please log in as an admin or mentor.", "error")
        return redirect(url_for("home"))

    # Get courses that the logged-in user is allowed to manage
    if role == "admin":
        courses = fetch_all("""
            SELECT course_id, course_name
            FROM courses
            ORDER BY course_name
        """)
    else:
        mentor_id = session.get("user_id")

        courses = fetch_all("""
            SELECT course_id, course_name
            FROM courses
            WHERE instructor_id = %s
            ORDER BY course_name
        """, (mentor_id,))

    if request.method == "POST":

        course_id = request.form.get("course_id", type=int)
        title = request.form.get("title", "").strip()
        description = request.form.get("description", "").strip()
        youtube_url = request.form.get("youtube_url", "").strip()
        lesson_order = request.form.get("lesson_order", type=int)

        if (
            not course_id
            or not title
            or not description
            or not youtube_url
            or not lesson_order
        ):
            flash("Please fill in all fields.", "error")
            return render_template("add_lesson.html", courses=courses)

        if lesson_order < 1:
            flash("Lesson number must be at least 1.", "error")
            return render_template("add_lesson.html", courses=courses)

        # Verify that the course exists
        course = fetch_one("""
            SELECT course_id
            FROM courses
            WHERE course_id = %s
        """, (course_id,))

        if not course:
            flash("The selected course does not exist.", "error")
            return render_template("add_lesson.html", courses=courses)

        # Mentors can manage only their own courses
        if role == "mentor":
            mentor_id = session.get("user_id")

            permitted_course = fetch_one("""
                SELECT course_id
                FROM courses
                WHERE course_id = %s
                AND instructor_id = %s
            """, (course_id, mentor_id))

            if not permitted_course:
                flash("You can add lessons only to your own courses.", "error")
                return render_template("add_lesson.html", courses=courses)

        # Validate supported YouTube URL formats
        allowed_prefixes = (
            "https://www.youtube.com/watch?v=",
            "https://youtube.com/watch?v=",
            "https://youtu.be/",
            "https://www.youtube.com/embed/"
        )

        if not youtube_url.startswith(allowed_prefixes):
            flash("Please enter a valid YouTube video URL.", "error")
            return render_template("add_lesson.html", courses=courses)

        try:
            execute_query("""
                INSERT INTO lessons
                    (course_id, title, description, youtube_url, lesson_order)
                VALUES (%s, %s, %s, %s, %s)
            """, (
                course_id,
                title,
                description,
                youtube_url,
                lesson_order
            ))

            flash("Lesson added successfully!", "success")

            return redirect(
                url_for("course_detail", course_id=course_id)
            )

        except Exception as error:
            print("Error adding lesson:", error)
            flash("Could not add the lesson. Please try again.", "error")

    return render_template("add_lesson.html", courses=courses)


# --------------------------------------------------
# STUDENT: MARK LESSON AS COMPLETED
# --------------------------------------------------

@app.route(
    "/student/lesson/<int:lesson_id>/complete",
    methods=["POST"]
)
@login_required("student")
def complete_lesson(lesson_id):

    student_id = session["user_id"]

    # Check that the student is enrolled in the lesson's course
    lesson = fetch_one("""
        SELECT l.lesson_id, l.course_id
        FROM lessons l
        JOIN enrollments e
            ON e.course_id = l.course_id
        WHERE l.lesson_id = %s
          AND e.student_id = %s
    """, (lesson_id, student_id))

    if not lesson:
        abort(404)

    # Save the lesson completion
    execute_query("""
        INSERT INTO lesson_progress
            (student_id, lesson_id, completed, completed_at)
        VALUES (%s, %s, TRUE, CURRENT_TIMESTAMP)
        ON DUPLICATE KEY UPDATE
            completed = TRUE,
            completed_at = CURRENT_TIMESTAMP
    """, (student_id, lesson_id))

    # Recalculate progress using lessons, assignments and quizzes
    calculate_course_progress(
        student_id,
        lesson["course_id"]
    )

    flash(
        "Lesson marked as completed! Your course progress has been updated.",
        "success"
    )

    return redirect(
        url_for(
            "course_detail",
            course_id=lesson["course_id"]
        )
    )


@app.route(
    "/student/study/start/<int:course_id>",
    methods=["POST"]
)
@login_required("student")
def start_study_session(course_id):
    student_id = session["user_id"]

    # Check whether the student is enrolled in this course
    enrollment = fetch_one("""
        SELECT course_id
        FROM enrollments
        WHERE student_id = %s AND course_id = %s
    """, (student_id, course_id))

    if not enrollment:
        flash("You must enroll in this course before tracking study time.", "error")
        return redirect(url_for("student_dashboard"))

    # Check whether the student already has an active session
    active_session = fetch_one("""
        SELECT session_id
        FROM study_sessions
        WHERE student_id = %s AND end_time IS NULL
        LIMIT 1
    """, (student_id,))

    if active_session:
        flash("You already have an active study session. Stop it before starting another.", "error")
        return redirect(url_for("course_detail", course_id=course_id))

    # Create a new study session
    execute_query("""
        INSERT INTO study_sessions (student_id, course_id)
        VALUES (%s, %s)
    """, (student_id, course_id))

    flash("Study session started! Remember to stop it when you finish.", "success")

    return redirect(url_for("course_detail", course_id=course_id))


@app.route(
    "/student/study/stop",
    methods=["POST"]
)
@login_required("student")
def stop_study_session():
    from datetime import datetime

    student_id = session["user_id"]

    # Find the student's active study session
    active_session = fetch_one("""
        SELECT session_id, course_id, start_time
        FROM study_sessions
        WHERE student_id = %s AND end_time IS NULL
        ORDER BY session_id DESC
        LIMIT 1
    """, (student_id,))

    if not active_session:
        flash("You don't have an active study session.", "error")
        return redirect(url_for("student_dashboard"))

    # Calculate the study duration
    start_time = active_session["start_time"]

    if isinstance(start_time, str):
        start_time = datetime.fromisoformat(start_time)

    end_time = datetime.now()
    duration_seconds = max(0, int((end_time - start_time).total_seconds()))
    duration_minutes = duration_seconds // 60

    # Save the end time and duration
    execute_query("""
        UPDATE study_sessions
        SET end_time = %s,
            duration_minutes = %s
        WHERE session_id = %s AND student_id = %s
    """, (
        end_time,
        duration_minutes,
        active_session["session_id"],
        student_id
    ))

    flash(
        f"Study session stopped! You studied for {duration_minutes} minutes.",
        "success"
    )

    return redirect(
        url_for("course_detail", course_id=active_session["course_id"])
    )


@app.route(
    "/student/study/auto/<int:course_id>",
    methods=["POST"]
)
@login_required("student")
def auto_save_study_time(course_id):
    from datetime import datetime, timedelta

    student_id = session["user_id"]

    # Check student enrollment
    enrollment = fetch_one("""
        SELECT course_id
        FROM enrollments
        WHERE student_id = %s AND course_id = %s
    """, (student_id, course_id))

    if not enrollment:
        return {"error": "You are not enrolled in this course."}, 403

    # Read duration sent by the browser
    data = request.get_json(silent=True) or {}
    duration_seconds = data.get("duration_seconds", 0)

    try:
        duration_seconds = int(duration_seconds)
    except (TypeError, ValueError):
        return {"error": "Invalid duration."}, 400

    # Allow only reasonable intervals
    if duration_seconds <= 0 or duration_seconds > 120:
        return {"error": "Invalid duration."}, 400

    end_time = datetime.now()
    start_time = end_time - timedelta(seconds=duration_seconds)
    duration_minutes = duration_seconds // 60

    # Save the exact interval and duration
    execute_query("""
        INSERT INTO study_sessions
            (student_id, course_id, start_time, end_time,
             duration_minutes, duration_seconds)
        VALUES (%s, %s, %s, %s, %s, %s)
    """, (
        student_id,
        course_id,
        start_time,
        end_time,
        duration_minutes,
        duration_seconds
    ))

    return {"success": True}, 200

# --------------------------------------------------
# ADMIN: WEEKLY LEARNING ACTIVITY REPORT
# Using Pandas and NumPy for activity summaries
# --------------------------------------------------

@app.route("/admin/analytics/weekly-activity")
@login_required("admin")
def admin_weekly_activity():

    # Fetch completed study sessions from the last 12 weeks
    study_sessions = fetch_all("""
        SELECT
            session_id,
            student_id,
            start_time,
            duration_seconds,
            duration_minutes
        FROM study_sessions
        WHERE start_time >= DATE_SUB(
            CURDATE(),
            INTERVAL 12 WEEK
        )
        AND end_time IS NOT NULL
        ORDER BY start_time ASC
    """)

    # Convert database results into a Pandas DataFrame
    df = pd.DataFrame(study_sessions)

    # Handle missing or invalid activity data
    if df.empty:
        weekly_activity = []

    else:
        # Convert start_time to datetime
        df["start_time"] = pd.to_datetime(
            df["start_time"],
            errors="coerce"
        )

        # Remove records without valid dates or identifiers
        df = df.dropna(
            subset=["start_time", "student_id", "session_id"]
        ).copy()

        if df.empty:
            weekly_activity = []

        else:
            # Convert duration values to numeric
            df["duration_seconds"] = pd.to_numeric(
                df["duration_seconds"],
                errors="coerce"
            )

            df["duration_minutes"] = pd.to_numeric(
                df["duration_minutes"],
                errors="coerce"
            )

            # Calculate study time in seconds.
            # Use minutes if seconds are missing or zero.
            df["study_seconds"] = np.where(
                df["duration_seconds"].fillna(0) > 0,
                df["duration_seconds"],
                df["duration_minutes"].fillna(0) * 60
            )

            # Replace negative or invalid durations with zero
            df["study_seconds"] = (
                pd.to_numeric(
                    df["study_seconds"],
                    errors="coerce"
                )
                .fillna(0)
                .clip(lower=0)
            )

            # Group sessions by week, starting Monday
            df["week_start"] = (
                df["start_time"]
                .dt.to_period("W-SUN")
                .dt.start_time
                .dt.date
            )

            # Summarize weekly learning activity
            summary = (
                df.groupby("week_start")
                .agg(
                    active_students=("student_id", "nunique"),
                    total_sessions=("session_id", "count"),
                    total_study_seconds=("study_seconds", "sum")
                )
                .reset_index()
            )

            # Convert seconds to study hours
            summary["total_study_hours"] = (
                summary["total_study_seconds"] / 3600
            ).round(2)

            # Sort newest week first
            summary = summary.sort_values(
                "week_start",
                ascending=False
            )

            # Prepare data for the HTML template
            weekly_activity = summary[
                [
                    "week_start",
                    "active_students",
                    "total_sessions",
                    "total_study_hours"
                ]
            ].to_dict(orient="records")

    return render_template(
        "admin_weekly_activity.html",
        weekly_activity=weekly_activity
    )

# --------------------------------------------------
# STUDENT REGISTRATION
# --------------------------------------------------

@app.route("/student/register", methods=["GET", "POST"])
def student_register():
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        confirm_password = request.form.get("confirm_password", "")

        if not name or not email or not password:
            flash("Please fill in all required fields.", "error")
            return render_template("student_register.html")

        if len(password) < 8:
            flash("Password must contain at least 8 characters.", "error")
            return render_template("student_register.html")

        if password != confirm_password:
            flash("Passwords do not match.", "error")
            return render_template("student_register.html")

        existing = fetch_one(
            "SELECT student_id FROM students WHERE email = %s",
            (email,)
        )

        if existing:
            flash("This email is already registered. Please log in.", "error")
            return redirect(url_for("student_login"))

        execute_query("""
            INSERT INTO students (name, email, password)
            VALUES (%s, %s, %s)
        """, (name, email, generate_password_hash(password)))

        flash("Registration successful! Please log in.", "success")
        return redirect(url_for("student_login"))

    return render_template("student_register.html")


# --------------------------------------------------
# STUDENT LOGIN
# --------------------------------------------------

@app.route("/student/login", methods=["GET", "POST"])
def student_login():
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")

        student = fetch_one(
            "SELECT * FROM students WHERE email = %s",
            (email,)
        )

        if not student or not student["password"]:
            flash("Invalid email or password. Please register first if needed.", "error")
            return render_template("student_login.html")

        stored_password = student["password"]

        # Only accept passwords stored as Werkzeug password hashes.
        if not stored_password.startswith(("scrypt:", "pbkdf2:")):
            flash("This account needs a new password. Please register with another email or contact the administrator.", "error")
            return render_template("student_login.html")

        if not check_password_hash(stored_password, password):
            flash("Invalid email or password.", "error")
            return render_template("student_login.html")

        session.clear()
        session["user_id"] = student["student_id"]
        session["user_role"] = "student"
        session["user_name"] = student["name"]

        return redirect(url_for("student_dashboard"))

    return render_template("student_login.html")


# --------------------------------------------------
# MENTOR REGISTRATION
# --------------------------------------------------

@app.route("/mentor/register", methods=["GET", "POST"])
def mentor_register():
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        email = request.form.get("email", "").strip().lower()
        expertise = request.form.get("expertise", "").strip()
        bio = request.form.get("bio", "").strip()
        password = request.form.get("password", "")
        confirm_password = request.form.get("confirm_password", "")

        if not name or not email or not expertise or not password:
            flash("Please fill in all required fields.", "error")
            return render_template("mentor_register.html")

        if len(password) < 8:
            flash("Password must contain at least 8 characters.", "error")
            return render_template("mentor_register.html")

        if password != confirm_password:
            flash("Passwords do not match.", "error")
            return render_template("mentor_register.html")

        existing = fetch_one(
            "SELECT instructor_id FROM instructors WHERE email = %s",
            (email,)
        )

        if existing:
            flash(
                "This email is already registered. Please log in or contact the administrator.",
                "error"
            )
            return redirect(url_for("mentor_login"))

        execute_query("""
            INSERT INTO instructors
                (name, email, expertise, bio, password, approval_status)
            VALUES (%s, %s, %s, %s, %s, 'Pending')
        """, (
            name,
            email,
            expertise,
            bio,
            generate_password_hash(password)
        ))

        flash(
            "Registration submitted successfully! Please wait for admin approval before logging in.",
            "success"
        )
        return redirect(url_for("mentor_login"))

    return render_template("mentor_register.html")
# --------------------------------------------------
# MENTOR LOGIN
# --------------------------------------------------

@app.route("/mentor/login", methods=["GET", "POST"])
def mentor_login():
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")

        mentor = fetch_one(
            "SELECT * FROM instructors WHERE email = %s",
            (email,)
        )

        # Check whether the mentor account exists
        if not mentor:
            flash("Invalid email or password.", "error")
            return render_template("mentor_login.html")

        # Check whether a password has been set
        if (
            not mentor.get("password")
            or not mentor["password"].startswith(("scrypt:", "pbkdf2:"))
            or not check_password_hash(mentor["password"], password)
        ):
            flash("Invalid email or password.", "error")
            return render_template("mentor_login.html")

        # Check admin approval status
        if mentor["approval_status"] == "Pending":
            flash(
                "Your registration is awaiting admin approval.",
                "error"
            )
            return render_template("mentor_login.html")

        if mentor["approval_status"] == "Rejected":
            flash(
                "Your mentor registration was rejected. Please contact the administrator.",
                "error"
            )
            return render_template("mentor_login.html")

        # Log in approved mentors
        session.clear()
        session["user_id"] = mentor["instructor_id"]
        session["user_role"] = "mentor"
        session["user_name"] = mentor["name"]

        return redirect(url_for("mentor_dashboard"))

    return render_template("mentor_login.html")


# --------------------------------------------------
# ADMIN LOGIN
# --------------------------------------------------

@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")

        admin = fetch_one(
            "SELECT * FROM admins WHERE email = %s",
            (email,)
        )

        if (
            not admin
            or not admin["password"]
            or not admin["password"].startswith(("scrypt:", "pbkdf2:"))
            or not check_password_hash(admin["password"], password)
        ):
            flash("Invalid admin email or password.", "error")
            return render_template("admin_login.html")

        session.clear()
        session["user_id"] = admin["admin_id"]
        session["user_role"] = "admin"
        session["user_name"] = admin["name"]

        return redirect(url_for("admin_dashboard"))

    return render_template("admin_login.html")


# --------------------------------------------------
# STUDENT DASHBOARD
# --------------------------------------------------

@app.route("/student/dashboard")
@login_required("student")
def student_dashboard():
    student_id = session["user_id"]

    enrollments = fetch_all("""
        SELECT
            c.course_id,
            c.course_name,
            c.description,
            c.category,
            e.progress,
            e.status,
            i.name AS instructor_name
        FROM enrollments e
        JOIN courses c ON e.course_id = c.course_id
        LEFT JOIN instructors i ON c.instructor_id = i.instructor_id
        WHERE e.student_id = %s
        ORDER BY e.enrolled_at DESC
    """, (student_id,))

    available_courses = fetch_all("""
        SELECT
            c.course_id,
            c.course_name,
            c.description,
            c.category,
            i.name AS instructor_name
        FROM courses c
        LEFT JOIN instructors i ON c.instructor_id = i.instructor_id
        WHERE c.course_id NOT IN (
            SELECT course_id FROM enrollments WHERE student_id = %s
        )
        ORDER BY c.course_name
    """, (student_id,))

    total_enrolled = len(enrollments)
    completed_courses = sum(
        1 for item in enrollments if item["status"] == "Completed"
    )
    average_progress = round(
        sum(item["progress"] for item in enrollments) / total_enrolled
    ) if total_enrolled else 0

    return render_template(
        "student_dashboard.html",
        student_name=session["user_name"],
        enrollments=enrollments,
        available_courses=available_courses,
        total_enrolled=total_enrolled,
        completed_courses=completed_courses,
        average_progress=average_progress
    )

# -------------------- STUDENT ASSIGNMENTS --------------------

@app.route("/student/assignments")
@login_required("student")
def student_assignments():
    student_id = session["user_id"]

    # Get assignments only for courses the student is enrolled in
    assignments = fetch_all("""
        SELECT
            a.assignment_id,
            a.title,
            a.description,
            a.due_date,
            a.max_marks,
            a.created_at,
            c.course_name,
            s.submission_id,
            s.answer_text,
            s.marks_obtained,
            s.feedback,
            s.submitted_at
        FROM assignments a
        JOIN courses c
            ON a.course_id = c.course_id
        JOIN enrollments e
            ON e.course_id = c.course_id
        LEFT JOIN assignment_submissions s
            ON s.assignment_id = a.assignment_id
            AND s.student_id = %s
        WHERE e.student_id = %s
        ORDER BY a.created_at DESC
    """, (student_id, student_id))

    return render_template(
        "student_assignments.html",
        assignments=assignments
    )

# -------------------- STUDENT QUIZ LIST --------------------

@app.route("/student/quizzes")
@login_required("student")
def student_quizzes():
    student_id = session["user_id"]

    quizzes = fetch_all("""
        SELECT
            q.quiz_id,
            q.title,
            q.description,
            q.time_limit_minutes,
            q.max_attempts,
            c.course_name,

            COUNT(DISTINCT qq.question_id) AS question_count,

            COUNT(DISTINCT qa.attempt_id) AS attempt_count,

            COALESCE(
                MAX(
                    CASE
                        WHEN qa.submitted_at IS NOT NULL
                        THEN qa.score
                    END
                ),
                0
            ) AS best_score

        FROM quizzes q

        JOIN courses c
            ON q.course_id = c.course_id

        JOIN enrollments e
            ON e.course_id = c.course_id
            AND e.student_id = %s

        LEFT JOIN quiz_questions qq
            ON qq.quiz_id = q.quiz_id

        LEFT JOIN quiz_attempts qa
            ON qa.quiz_id = q.quiz_id
            AND qa.student_id = %s

        GROUP BY
            q.quiz_id,
            q.title,
            q.description,
            q.time_limit_minutes,
            q.max_attempts,
            c.course_name

        ORDER BY q.created_at DESC
    """, (student_id, student_id))

    return render_template(
        "student_quizzes.html",
        quizzes=quizzes
    )


# -------------------- STUDENT QUIZ RESULTS --------------------

@app.route("/student/quiz-results")
@login_required("student")
def quiz_results():
    student_id = session["user_id"]

    results = fetch_all("""
        SELECT
            qa.attempt_id,
            qa.score,
            qa.started_at,
            qa.submitted_at,
            q.title AS quiz_title,
            c.course_name,

            (
                SELECT COUNT(*)
                FROM quiz_attempts previous
                WHERE previous.quiz_id = qa.quiz_id
                  AND previous.student_id = qa.student_id
                  AND (
                      previous.started_at < qa.started_at
                      OR (
                          previous.started_at = qa.started_at
                          AND previous.attempt_id <= qa.attempt_id
                      )
                  )
            ) AS attempt_number

        FROM quiz_attempts qa

        JOIN quizzes q
            ON qa.quiz_id = q.quiz_id

        JOIN courses c
            ON q.course_id = c.course_id

        WHERE qa.student_id = %s

        ORDER BY qa.started_at DESC, qa.attempt_id DESC
    """, (student_id,))

    return render_template(
        "quiz_results.html",
        results=results
    )

# --------------------------------------------------
# QUIZ LEADERBOARD - GLOBAL AND COURSE-WISE
# --------------------------------------------------

@app.route("/leaderboard")
def leaderboard():

    # TEMPORARY DEBUGGING
    print("LEADERBOARD ROLE:", session.get("user_role"))
    print("LEADERBOARD USER ID:", session.get("user_id"))

    # Allow logged-in students, mentors, and admins
    user_role = session.get("user_role")
    user_id = session.get("user_id")

    if not user_id or user_role not in ["student", "mentor", "admin"]:
        flash("Please log in to view the leaderboard.", "error")

        if user_role == "mentor":
            return redirect(url_for("mentor_login"))
        elif user_role == "admin":
            return redirect(url_for("admin_login"))
        else:
            return redirect(url_for("student_login"))

    # Get the selected course ID, if any
    course_id = request.args.get("course_id", type=int)

    # Get courses for the dropdown
    courses = fetch_all("""
        SELECT course_id, course_name
        FROM courses
        ORDER BY course_name
    """)

    # Get the best submitted score for each student in each quiz
    query = """
        SELECT
            s.student_id,
            s.name AS student_name,
            COUNT(*) AS quizzes_completed,
            ROUND(AVG(best_scores.best_score), 2) AS average_score,
            ROUND(SUM(best_scores.best_score), 2) AS total_score

        FROM (
            SELECT
                student_id,
                quiz_id,
                MAX(score) AS best_score
            FROM quiz_attempts
            WHERE submitted_at IS NOT NULL
            GROUP BY student_id, quiz_id
        ) AS best_scores

        JOIN students s
            ON s.student_id = best_scores.student_id

        JOIN quizzes q
            ON q.quiz_id = best_scores.quiz_id
    """

    params = ()

    # Filter results when a particular course is selected
    if course_id is not None:
        query += """
            WHERE q.course_id = %s
        """
        params = (course_id,)

    # Rank students by average score, then total score
    query += """
        GROUP BY s.student_id, s.name

        ORDER BY
            average_score DESC,
            total_score DESC,
            quizzes_completed DESC,
            student_name ASC
    """

    # Fetch leaderboard data
    leaderboard_data = fetch_all(query, params)

    # Display the leaderboard
    return render_template(
        "leaderboard.html",
        leaderboard_data=leaderboard_data,
        courses=courses,
        selected_course_id=course_id
    )

# -------------------- START QUIZ ATTEMPT --------------------

@app.route("/student/quizzes/<int:quiz_id>/attempt")
@login_required("student")
def attempt_quiz(quiz_id):
    student_id = session["user_id"]

    # Check that the student is enrolled in this course.
    quiz = fetch_one("""
        SELECT
            q.quiz_id,
            q.title,
            q.description,
            q.time_limit_minutes,
            q.max_attempts,
            c.course_name
        FROM quizzes q
        JOIN courses c
            ON q.course_id = c.course_id
        JOIN enrollments e
            ON e.course_id = c.course_id
        WHERE q.quiz_id = %s
        AND e.student_id = %s
    """, (quiz_id, student_id))

    if not quiz:
        flash("Quiz not found or you are not enrolled in its course.", "error")
        return redirect(url_for("student_quizzes"))

    # Count attempts already started by this student.
    attempt_data = fetch_one("""
        SELECT COUNT(*) AS attempt_count
        FROM quiz_attempts
        WHERE quiz_id = %s
        AND student_id = %s
    """, (quiz_id, student_id))

    if attempt_data["attempt_count"] >= quiz["max_attempts"]:
        flash("You have used all available attempts for this quiz.", "error")
        return redirect(url_for("student_quizzes"))

    # Check whether the quiz has questions.
    questions = fetch_all("""
        SELECT
            question_id,
            question_text,
            option_a,
            option_b,
            option_c,
            option_d,
            marks,
            question_order
        FROM quiz_questions
        WHERE quiz_id = %s
        ORDER BY question_order, question_id
    """, (quiz_id,))

    if not questions:
        flash("This quiz does not have any questions yet.", "error")
        return redirect(url_for("student_quizzes"))

    # Create an attempt and record its start time.
    attempt_id = execute_query("""
        INSERT INTO quiz_attempts (
            quiz_id,
            student_id,
            score,
            total_marks,
            started_at
        )
        VALUES (%s, %s, 0, 10, NOW())
    """, (quiz_id, student_id))

    # Pass the attempt ID to the quiz form.
    return render_template(
        "attempt_quiz.html",
        quiz=quiz,
        questions=questions,
        attempt_id=attempt_id
    )


# --------------------------------------------------
# STUDENT: SUBMIT QUIZ AND CALCULATE SCORE
# --------------------------------------------------

@app.route(
    "/student/quiz/<int:quiz_id>/submit",
    methods=["POST"]
)
@login_required("student")
def submit_quiz(quiz_id):
    from datetime import datetime, timedelta

    student_id = session["user_id"]

    # Get the quiz details
    quiz = fetch_one("""
        SELECT q.quiz_id, q.course_id, q.time_limit_minutes
        FROM quizzes q
        JOIN enrollments e
            ON e.course_id = q.course_id
        WHERE q.quiz_id = %s
          AND e.student_id = %s
    """, (quiz_id, student_id))

    if not quiz:
        flash("Quiz not found or you are not enrolled in its course.", "error")
        return redirect(url_for("student_quizzes"))

    # Get the student's latest attempt
    attempt = fetch_one("""
        SELECT attempt_id, started_at, submitted_at
        FROM quiz_attempts
        WHERE quiz_id = %s
          AND student_id = %s
        ORDER BY attempt_id DESC
        LIMIT 1
    """, (quiz_id, student_id))

    if not attempt:
        flash("Please start the quiz before submitting.", "error")
        return redirect(url_for("student_quizzes"))

    # Prevent submitting an already submitted attempt
    if attempt["submitted_at"] is not None:
        flash("This quiz attempt has already been submitted.", "error")
        return redirect(url_for("student_quizzes"))

    # Check the quiz time limit
    started_at = attempt["started_at"]

    if isinstance(started_at, str):
        started_at = datetime.fromisoformat(started_at)

    deadline = started_at + timedelta(
        minutes=quiz["time_limit_minutes"]
    )

    if datetime.now() > deadline:
        execute_query("""
            UPDATE quiz_attempts
            SET score = 0,
                submitted_at = CURRENT_TIMESTAMP
            WHERE attempt_id = %s
              AND student_id = %s
        """, (attempt["attempt_id"], student_id))

        # Recalculate course progress after the quiz is submitted
        calculate_course_progress(
            student_id,
            quiz["course_id"]
        )

        flash("Time is up! Your quiz was submitted with a score of 0.", "error")
        return redirect(url_for("student_quizzes"))

    # Get the correct answers and marks
    questions = fetch_all("""
        SELECT question_id, correct_option, marks
        FROM quiz_questions
        WHERE quiz_id = %s
    """, (quiz_id,))

    if not questions:
        flash("This quiz has no questions yet.", "error")
        return redirect(url_for("student_quizzes"))

    earned_marks = 0
    total_marks = 0

    # Check each student's answer
    for question in questions:
        question_id = question["question_id"]
        correct_option = question["correct_option"]
        marks = question["marks"]

        total_marks += marks

        student_answer = request.form.get(
            f"question_{question_id}", ""
        ).strip().upper()

        if student_answer == correct_option:
            earned_marks += marks

    # Convert the score to a scale of 10
    if total_marks > 0:
        score_out_of_10 = round(
            (earned_marks / total_marks) * 10, 2
        )
    else:
        score_out_of_10 = 0

    # Save the quiz score
    execute_query("""
        UPDATE quiz_attempts
        SET score = %s,
            total_marks = 10,
            submitted_at = CURRENT_TIMESTAMP
        WHERE attempt_id = %s
          AND student_id = %s
    """, (
        score_out_of_10,
        attempt["attempt_id"],
        student_id
    ))

    # Update overall course progress
    calculate_course_progress(
        student_id,
        quiz["course_id"]
    )

    flash(
        f"Quiz submitted successfully! Your score is "
        f"{score_out_of_10}/10.",
        "success"
    )

    return redirect(url_for("student_quizzes"))

#  -------- Student Assignment Submission -----------
@app.route(
    "/student/assignments/<int:assignment_id>/submit",
    methods=["POST"]
)
@login_required("student")
def submit_assignment(assignment_id):
    student_id = session["user_id"]
    answer_text = request.form.get("answer_text", "").strip()

    if not answer_text:
        flash("Please enter your answer before submitting.", "error")
        return redirect(url_for("student_assignments"))

    # Confirm that the student is enrolled in the assignment's course
    assignment = fetch_one("""
        SELECT a.assignment_id, a.course_id, a.due_date
        FROM assignments a
        JOIN enrollments e
            ON e.course_id = a.course_id
        WHERE a.assignment_id = %s
          AND e.student_id = %s
    """, (assignment_id, student_id))

    if not assignment:
        flash(
            "Assignment not found or you are not enrolled in its course.",
            "error"
        )
        return redirect(url_for("student_assignments"))

    # Check whether the deadline has passed
    if assignment["due_date"] is not None:
        from datetime import datetime

        due_date = assignment["due_date"]

        if isinstance(due_date, str):
            due_date = datetime.fromisoformat(due_date)

        if datetime.now() > due_date:
            flash("The deadline for this assignment has passed.", "error")
            return redirect(url_for("student_assignments"))

    # Prevent duplicate submissions
    existing_submission = fetch_one("""
        SELECT submission_id
        FROM assignment_submissions
        WHERE assignment_id = %s
          AND student_id = %s
    """, (assignment_id, student_id))

    if existing_submission:
        flash("You have already submitted this assignment.", "error")
        return redirect(url_for("student_assignments"))

    # Save the student's answer
    execute_query("""
        INSERT INTO assignment_submissions
            (assignment_id, student_id, answer_text)
        VALUES (%s, %s, %s)
    """, (assignment_id, student_id, answer_text))

    # Recalculate course progress after assignment submission
    calculate_course_progress(
        student_id,
        assignment["course_id"]
    )

    flash(
        "Assignment submitted successfully! Your course progress has been updated.",
        "success"
    )

    return redirect(url_for("student_assignments"))

# --------------------------------------------------
# COURSE ENROLLMENT
# --------------------------------------------------

@app.route("/student/enroll/<int:course_id>", methods=["POST"])
@login_required("student")
def enroll_course(course_id):
    course = fetch_one(
        "SELECT course_id FROM courses WHERE course_id = %s",
        (course_id,)
    )

    if not course:
        abort(404)

    existing = fetch_one("""
        SELECT enrollment_id
        FROM enrollments
        WHERE student_id = %s AND course_id = %s
    """, (session["user_id"], course_id))

    if existing:
        flash("You are already enrolled in this course.", "error")
    else:
        execute_query("""
            INSERT INTO enrollments
                (student_id, course_id, progress, status)
            VALUES (%s, %s, 0, 'Enrolled')
        """, (session["user_id"], course_id))
        flash("Successfully enrolled in the course!", "success")

    return redirect(url_for("student_dashboard"))


# --------------------------------------------------
# MENTOR DASHBOARD
# --------------------------------------------------

@app.route("/mentor/dashboard")
@login_required("mentor")
def mentor_dashboard():
    mentor_id = session["user_id"]

    courses = fetch_all("""
        SELECT
            c.course_id,
            c.course_name,
            c.description,
            c.category,
            c.difficulty,
            c.duration_hours,
            COUNT(DISTINCT e.enrollment_id) AS student_count,
            COALESCE(ROUND(AVG(e.progress)), 0) AS average_progress
        FROM course_instructors ci
        JOIN courses c
            ON ci.course_id = c.course_id
        LEFT JOIN enrollments e
            ON c.course_id = e.course_id
        WHERE ci.instructor_id = %s
        GROUP BY
            c.course_id,
            c.course_name,
            c.description,
            c.category,
            c.difficulty,
            c.duration_hours
        ORDER BY c.course_name
    """, (mentor_id,))

    total_courses = len(courses)

    total_students = sum(
        course["student_count"] for course in courses
    )

    all_progress = fetch_one("""
        SELECT
            COALESCE(ROUND(AVG(e.progress)), 0) AS average_progress
        FROM course_instructors ci
        JOIN enrollments e
            ON ci.course_id = e.course_id
        WHERE ci.instructor_id = %s
    """, (mentor_id,))

    expertise_data = fetch_one("""
        SELECT expertise
        FROM instructors
        WHERE instructor_id = %s
    """, (mentor_id,))

    return render_template(
        "mentor_dashboard.html",
        mentor_name=session["user_name"],
        expertise=expertise_data["expertise"],
        courses=courses,
        total_courses=total_courses,
        total_students=total_students,
        average_progress=all_progress["average_progress"]
    )
# -------------------- MENTOR ASSIGNMENTS --------------------

@app.route("/mentor/assignments", methods=["GET", "POST"])
@login_required("mentor")
def mentor_assignments():
    mentor_id = session["user_id"]

    # Get all courses assigned to this mentor
    courses = fetch_all("""
        SELECT c.course_id, c.course_name
        FROM courses c
        JOIN course_instructors ci
            ON c.course_id = ci.course_id
        WHERE ci.instructor_id = %s
        ORDER BY c.course_name
    """, (mentor_id,))

    # Handle assignment creation
    if request.method == "POST":
        course_id = request.form.get("course_id", "").strip()
        title = request.form.get("title", "").strip()
        description = request.form.get("description", "").strip()
        due_date = request.form.get("due_date", "").strip()
        max_marks = request.form.get("max_marks", "").strip()

        # Validate submitted details
        if not course_id or not title or not description or not max_marks:
            flash("Please fill in all required fields.", "error")
            return redirect(url_for("mentor_assignments"))

        try:
            course_id = int(course_id)
            max_marks = int(max_marks)

            if max_marks < 1 or max_marks > 100000:
                raise ValueError

        except ValueError:
            flash(
                "Please select a valid course and enter valid maximum marks.",
                "error"
            )
            return redirect(url_for("mentor_assignments"))

        # Verify that the selected course is assigned to this mentor
        assigned_course = fetch_one("""
            SELECT c.course_id
            FROM courses c
            JOIN course_instructors ci
                ON c.course_id = ci.course_id
            WHERE c.course_id = %s
              AND ci.instructor_id = %s
        """, (course_id, mentor_id))

        if not assigned_course:
            flash(
                "You can create assignments only for your assigned courses.",
                "error"
            )
            return redirect(url_for("mentor_assignments"))

        # Convert deadline to MySQL datetime format
        if due_date:
            due_date = due_date.replace("T", " ")
            if len(due_date) == 16:
                due_date += ":00"
        else:
            due_date = None

        # Save the assignment
        execute_query("""
            INSERT INTO assignments
                (course_id, title, description, due_date, max_marks)
            VALUES (%s, %s, %s, %s, %s)
        """, (
            course_id,
            title,
            description,
            due_date,
            max_marks
        ))

        flash("Assignment published successfully!", "success")
        return redirect(url_for("mentor_assignments"))

    # Display assignments belonging to this mentor's assigned courses
    assignments = fetch_all("""
        SELECT
            a.assignment_id,
            a.title,
            a.description,
            a.due_date,
            a.max_marks,
            a.created_at,
            c.course_name
        FROM assignments a
        JOIN courses c
            ON a.course_id = c.course_id
        JOIN course_instructors ci
            ON c.course_id = ci.course_id
        WHERE ci.instructor_id = %s
        ORDER BY a.created_at DESC
    """, (mentor_id,))

    return render_template(
        "mentor_assignments.html",
        courses=courses,
        assignments=assignments
    )
# -------------------- MENTOR QUIZZES --------------------

@app.route("/mentor/quizzes", methods=["GET", "POST"])
@login_required("mentor")
def mentor_quizzes():
    mentor_id = session["user_id"]

    # Get courses assigned to this mentor
    courses = fetch_all("""
        SELECT c.course_id, c.course_name
        FROM courses c
        JOIN course_instructors ci
            ON c.course_id = ci.course_id
        WHERE ci.instructor_id = %s
        ORDER BY c.course_name
    """, (mentor_id,))

    # Create a new quiz
    if request.method == "POST":
        course_id = request.form.get("course_id", "").strip()
        title = request.form.get("title", "").strip()
        description = request.form.get("description", "").strip()
        time_limit = request.form.get("time_limit_minutes", "15").strip()
        max_attempts = request.form.get("max_attempts", "1").strip()

        if not course_id or not title:
            flash("Please select a course and enter a quiz title.", "error")
            return redirect(url_for("mentor_quizzes"))

        try:
            course_id = int(course_id)
            time_limit = int(time_limit)
            max_attempts = int(max_attempts)

            if not 1 <= time_limit <= 180:
                raise ValueError

            if not 1 <= max_attempts <= 10:
                raise ValueError

        except ValueError:
            flash("Please enter valid quiz settings.", "error")
            return redirect(url_for("mentor_quizzes"))

        # Verify that the course is assigned to this mentor
        assigned_course = fetch_one("""
            SELECT c.course_id
            FROM courses c
            JOIN course_instructors ci
                ON c.course_id = ci.course_id
            WHERE c.course_id = %s
              AND ci.instructor_id = %s
        """, (course_id, mentor_id))

        if not assigned_course:
            flash(
                "You can create quizzes only for your assigned courses.",
                "error"
            )
            return redirect(url_for("mentor_quizzes"))

        # Insert the new quiz
        execute_query("""
            INSERT INTO quizzes (
                course_id,
                title,
                description,
                time_limit_minutes,
                max_attempts
            )
            VALUES (%s, %s, %s, %s, %s)
        """, (
            course_id,
            title,
            description or None,
            time_limit,
            max_attempts
        ))

        flash("Quiz created successfully! Now add questions.", "success")
        return redirect(url_for("mentor_quizzes"))

    # Retrieve quizzes belonging to this mentor's assigned courses
    quizzes = fetch_all("""
        SELECT
            q.quiz_id,
            q.title,
            q.description,
            q.time_limit_minutes,
            q.max_attempts,
            c.course_name,
            COUNT(qq.question_id) AS question_count,
            COALESCE(SUM(qq.marks), 0) AS total_marks
        FROM quizzes q
        JOIN courses c
            ON q.course_id = c.course_id
        JOIN course_instructors ci
            ON c.course_id = ci.course_id
        LEFT JOIN quiz_questions qq
            ON q.quiz_id = qq.quiz_id
        WHERE ci.instructor_id = %s
        GROUP BY
            q.quiz_id,
            q.title,
            q.description,
            q.time_limit_minutes,
            q.max_attempts,
            c.course_name,
            q.created_at
        ORDER BY q.created_at DESC
    """, (mentor_id,))

    return render_template(
        "mentor_quizzes.html",
        courses=courses,
        quizzes=quizzes
    )

# -------------------- MENTOR QUIZ QUESTIONS --------------------

@app.route(
    "/mentor/quizzes/<int:quiz_id>/questions",
    methods=["GET", "POST"]
)
@login_required("mentor")
def manage_quiz_questions(quiz_id):
    mentor_id = session["user_id"]

    # Verify that this quiz belongs to one of the mentor's assigned courses.
    quiz = fetch_one("""
        SELECT
            q.quiz_id,
            q.title,
            c.course_name
        FROM quizzes q
        JOIN courses c
            ON q.course_id = c.course_id
        JOIN course_instructors ci
            ON c.course_id = ci.course_id
        WHERE q.quiz_id = %s
          AND ci.instructor_id = %s
    """, (quiz_id, mentor_id))

    if not quiz:
        flash("Quiz not found or access denied.", "error")
        return redirect(url_for("mentor_quizzes"))

    # Save a new question.
    if request.method == "POST":
        question_text = request.form.get("question_text", "").strip()
        option_a = request.form.get("option_a", "").strip()
        option_b = request.form.get("option_b", "").strip()
        option_c = request.form.get("option_c", "").strip()
        option_d = request.form.get("option_d", "").strip()
        correct_option = request.form.get("correct_option", "").strip()
        marks_input = request.form.get("marks", "1").strip()

        if not all([
            question_text,
            option_a,
            option_b,
            option_c,
            option_d
        ]):
            flash("Please fill in all question fields.", "error")
            return redirect(
                url_for("manage_quiz_questions", quiz_id=quiz_id)
            )

        if correct_option not in ("A", "B", "C", "D"):
            flash("Please select a valid correct answer.", "error")
            return redirect(
                url_for("manage_quiz_questions", quiz_id=quiz_id)
            )

        try:
            marks = int(marks_input)

            if marks < 1 or marks > 10:
                raise ValueError

        except ValueError:
            flash("Question marks must be between 1 and 10.", "error")
            return redirect(
                url_for("manage_quiz_questions", quiz_id=quiz_id)
            )

        # Assign the next question order.
        result = fetch_one("""
            SELECT COALESCE(MAX(question_order), 0) + 1 AS next_order
            FROM quiz_questions
            WHERE quiz_id = %s
        """, (quiz_id,))

        next_order = result["next_order"]

        # Insert the new question.
        execute_query("""
            INSERT INTO quiz_questions (
                quiz_id,
                question_text,
                option_a,
                option_b,
                option_c,
                option_d,
                correct_option,
                marks,
                question_order
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
        """, (
            quiz_id,
            question_text,
            option_a,
            option_b,
            option_c,
            option_d,
            correct_option,
            marks,
            next_order
        ))

        flash("Question added successfully!", "success")

        return redirect(
            url_for("manage_quiz_questions", quiz_id=quiz_id)
        )

    # Retrieve the quiz's questions.
    questions = fetch_all("""
        SELECT
            question_id,
            question_text,
            option_a,
            option_b,
            option_c,
            option_d,
            correct_option,
            marks,
            question_order
        FROM quiz_questions
        WHERE quiz_id = %s
        ORDER BY question_order, question_id
    """, (quiz_id,))

    return render_template(
        "mentor_quiz_questions.html",
        quiz=quiz,
        questions=questions
    )
# -------------------- MENTOR ASSIGNMENT EVALUATION --------------------

@app.route("/mentor/submissions")
@login_required("mentor")
def mentor_submissions():
    mentor_id = session["user_id"]

    submissions = fetch_all("""
        SELECT
            s.submission_id,
            s.answer_text,
            s.marks_obtained,
            s.feedback,
            s.submitted_at,
            a.assignment_id,
            a.title AS assignment_title,
            a.max_marks,
            c.course_name,
            st.student_id,
            st.name AS student_name
        FROM assignment_submissions s
        JOIN assignments a
            ON s.assignment_id = a.assignment_id
        JOIN courses c
            ON a.course_id = c.course_id
        JOIN students st
            ON s.student_id = st.student_id
        JOIN course_instructors ci
            ON c.course_id = ci.course_id
        WHERE ci.instructor_id = %s
        ORDER BY s.submitted_at DESC
    """, (mentor_id,))

    return render_template(
        "mentor_submissions.html",
        submissions=submissions
    )
@app.route(
    "/mentor/submissions/<int:submission_id>/grade",
    methods=["POST"]
)
@login_required("mentor")
def grade_assignment(submission_id):
    mentor_id = session["user_id"]

    marks_input = request.form.get("marks_obtained", "").strip()
    feedback = request.form.get("feedback", "").strip()

    # Validate the marks entered by the mentor.
    try:
        marks = float(marks_input)

        if marks < 0:
            raise ValueError

    except ValueError:
        flash("Please enter valid marks.", "error")
        return redirect(url_for("mentor_submissions"))

    # Verify that the submission belongs to a course
    # assigned to the currently logged-in mentor.
    submission = fetch_one("""
        SELECT
            s.submission_id,
            a.max_marks
        FROM assignment_submissions s
        JOIN assignments a
            ON s.assignment_id = a.assignment_id
        JOIN courses c
            ON a.course_id = c.course_id
        JOIN course_instructors ci
            ON c.course_id = ci.course_id
        WHERE s.submission_id = %s
          AND ci.instructor_id = %s
    """, (submission_id, mentor_id))

    if not submission:
        flash("Submission not found or access denied.", "error")
        return redirect(url_for("mentor_submissions"))

    # Ensure marks do not exceed the assignment's maximum marks.
    if marks > float(submission["max_marks"]):
        flash("Marks cannot exceed the maximum marks.", "error")
        return redirect(url_for("mentor_submissions"))

    # Save the marks and feedback.
    execute_query("""
        UPDATE assignment_submissions
        SET marks_obtained = %s,
            feedback = %s,
            graded_at = NOW()
        WHERE submission_id = %s
    """, (marks, feedback, submission_id))

    flash("Assignment evaluated successfully!", "success")
    return redirect(url_for("mentor_submissions"))

# --------------------------------------------------
# MENTOR: BROWSE COURSES
# --------------------------------------------------

@app.route("/mentor/courses")
@login_required("mentor")
def mentor_browse_courses():
    mentor_id = session["user_id"]

    mentor = fetch_one("""
        SELECT approval_status
        FROM instructors
        WHERE instructor_id = %s
    """, (mentor_id,))

    if not mentor or mentor["approval_status"] != "Approved":
        flash("Your mentor account must be approved first.", "error")
        return redirect(url_for("mentor_dashboard"))

    courses = fetch_all("""
        SELECT
            c.course_id,
            c.course_name,
            c.description,
            c.category,
            c.difficulty,
            cr.request_status
        FROM courses c
        LEFT JOIN course_requests cr
            ON c.course_id = cr.course_id
            AND cr.instructor_id = %s
        ORDER BY c.course_name
    """, (mentor_id,))

    return render_template(
        "mentor_browse_courses.html",
        courses=courses
    )


# --------------------------------------------------
# MENTOR: REQUEST A COURSE
# --------------------------------------------------

@app.route("/mentor/course/<int:course_id>/request", methods=["POST"])
@login_required("mentor")
def mentor_request_course(course_id):
    mentor_id = session["user_id"]

    # Check whether the mentor is approved
    mentor = fetch_one("""
        SELECT approval_status
        FROM instructors
        WHERE instructor_id = %s
    """, (mentor_id,))

    if not mentor or mentor["approval_status"] != "Approved":
        flash("Your mentor account must be approved first.", "error")
        return redirect(url_for("mentor_dashboard"))

    # Check whether the course exists
    course = fetch_one("""
        SELECT course_id, instructor_id
        FROM courses
        WHERE course_id = %s
    """, (course_id,))

    if not course:
        abort(404)

    # Prevent requesting a course already assigned to this mentor
    if course["instructor_id"] == mentor_id:
        flash("This course is already assigned to you.", "info")
        return redirect(url_for("mentor_browse_courses"))

    # Check whether this mentor has already requested the course
    existing_request = fetch_one("""
        SELECT request_id, request_status
        FROM course_requests
        WHERE instructor_id = %s AND course_id = %s
    """, (mentor_id, course_id))

    if existing_request:
        if existing_request["request_status"] == "Rejected":
            execute_query("""
                UPDATE course_requests
                SET request_status = 'Pending',
                    requested_at = CURRENT_TIMESTAMP
                WHERE request_id = %s
            """, (existing_request["request_id"],))

            flash("Your course request has been submitted again.", "success")
        else:
            flash("You have already requested this course.", "info")
    else:
        execute_query("""
            INSERT INTO course_requests
                (instructor_id, course_id, request_status)
            VALUES (%s, %s, 'Pending')
        """, (mentor_id, course_id))

        flash("Your request to teach this course has been submitted.", "success")

    return redirect(url_for("mentor_browse_courses"))


# --------------------------------------------------
# MENTOR: VIEW STUDENTS IN AN ASSIGNED COURSE
# --------------------------------------------------

@app.route("/mentor/course/<int:course_id>/students")
@login_required("mentor")
def mentor_course_students(course_id):
    mentor_id = session["user_id"]

    # Check whether this course is assigned to the logged-in mentor
    course = fetch_one("""
        SELECT c.course_id, c.course_name
        FROM courses c
        JOIN course_instructors ci
            ON c.course_id = ci.course_id
        WHERE c.course_id = %s
          AND ci.instructor_id = %s
    """, (course_id, mentor_id))

    # Reject access if the mentor is not assigned to this course
    if not course:
        abort(403)

    # Retrieve students enrolled in this course
    students = fetch_all("""
        SELECT
            s.student_id,
            s.name,
            s.email,
            e.progress,
            e.status,
            e.enrolled_at
        FROM enrollments e
        JOIN students s
            ON e.student_id = s.student_id
        WHERE e.course_id = %s
        ORDER BY s.name ASC
    """, (course_id,))

    return render_template(
        "mentor_students.html",
        course=course,
        students=students
    )
# --------------------------------------------------
# ADMIN DASHBOARD
# --------------------------------------------------

@app.route("/admin/dashboard")
@login_required("admin")
def admin_dashboard():
    stats = fetch_one("""
        SELECT
            (SELECT COUNT(*) FROM students) AS total_students,
            (SELECT COUNT(*) FROM instructors) AS total_mentors,
            (SELECT COUNT(*) FROM courses) AS total_courses,
            (SELECT COUNT(*) FROM enrollments) AS total_enrollments,
            (SELECT COALESCE(ROUND(AVG(progress)), 0)
            FROM enrollments) AS average_progress
    """)

    return render_template(
        "admin_dashboard.html",
        admin_name=session["user_name"],
        **stats
    )


# --------------------------------------------------
# ADMIN: COURSE ANALYTICS
# --------------------------------------------------

@app.route("/admin/analytics/courses")
@login_required("admin")
def admin_course_analytics():

    # Overall course statistics
    stats = fetch_one("""
        SELECT
            (SELECT COUNT(*)
             FROM courses) AS total_courses,

            (SELECT COUNT(*)
             FROM enrollments) AS total_enrollments,

            (SELECT COALESCE(ROUND(AVG(rating), 1), 0)
             FROM reviews) AS average_rating,

            (SELECT COUNT(*)
             FROM reviews) AS total_reviews
    """)

    # Course-wise enrollment and rating statistics
    course_analytics = fetch_all("""
        SELECT
            c.course_id,
            c.course_name,

            COUNT(DISTINCT e.student_id)
                AS total_enrollments,

            ROUND(AVG(r.rating), 1)
                AS average_rating,

            COUNT(DISTINCT r.review_id)
                AS total_reviews

        FROM courses c

        LEFT JOIN enrollments e
            ON c.course_id = e.course_id

        LEFT JOIN reviews r
            ON c.course_id = r.course_id

        GROUP BY
            c.course_id,
            c.course_name

        ORDER BY
            total_enrollments DESC,
            c.course_name ASC
    """)

    return render_template(
        "admin_course_analytics.html",
        stats=stats,
        course_analytics=course_analytics
    )


    # --------------------------------------------------
# ADMIN: STUDENT PERFORMANCE ANALYTICS
# --------------------------------------------------

@app.route("/admin/analytics/students")
@login_required("admin")
def admin_student_analytics():

    student_performance = fetch_all("""
        SELECT
            s.student_id,
            s.name,

            COALESCE(qa.quiz_attempts, 0)
                AS quiz_attempts,

            COALESCE(qa.average_quiz_percentage, 0)
                AS average_quiz_percentage,

            COALESCE(ass.total_submissions, 0)
                AS total_submissions,

            COALESCE(ass.graded_submissions, 0)
                AS graded_submissions,

            ass.average_assignment_marks

        FROM students s

        LEFT JOIN (
            SELECT
                student_id,
                COUNT(*) AS quiz_attempts,

                ROUND(
                    AVG(
                        CASE
                            WHEN total_marks > 0
                            THEN (score / total_marks) * 100
                            ELSE NULL
                        END
                    ), 2
                ) AS average_quiz_percentage

            FROM quiz_attempts
            GROUP BY student_id
        ) qa ON s.student_id = qa.student_id

        LEFT JOIN (
            SELECT
                student_id,
                COUNT(*) AS total_submissions,

                SUM(
                    CASE
                        WHEN marks_obtained IS NOT NULL
                        THEN 1 ELSE 0
                    END
                ) AS graded_submissions,

                ROUND(AVG(marks_obtained), 2)
                    AS average_assignment_marks

            FROM assignment_submissions
            GROUP BY student_id
        ) ass ON s.student_id = ass.student_id

        ORDER BY s.name ASC
    """)

    return render_template(
        "admin_student_analytics.html",
        student_performance=student_performance
    )

# --------------------------------------------------
# ADMIN: COURSE COMPLETION ANALYTICS
# --------------------------------------------------

@app.route("/admin/analytics/completion")
@login_required("admin")
def admin_completion_analytics():

    completion_analytics = fetch_all("""
        SELECT
            c.course_id,
            c.course_name,

            COUNT(e.student_id) AS total_students,

            SUM(
                CASE
    WHEN e.status = 'Completed' THEN 1
    ELSE 0
END
            ) AS completed_students,

            SUM(
                CASE
                    WHEN e.student_id IS NOT NULL
                         AND e.progress < 100
                    THEN 1
                    ELSE 0
                END
            ) AS students_in_progress,

            COALESCE(
                ROUND(AVG(e.progress), 2),
                0
            ) AS average_progress,

            COALESCE(
                ROUND(
                    100.0 * SUM(
                        CASE
                            WHEN e.progress >= 100 THEN 1
                            ELSE 0
                        END
                    ) / NULLIF(COUNT(e.student_id), 0),
                    2
                ),
                0
            ) AS completion_rate

        FROM courses c

        LEFT JOIN enrollments e
            ON c.course_id = e.course_id

        GROUP BY
            c.course_id,
            c.course_name

        ORDER BY
            completion_rate DESC,
            c.course_name ASC
    """)

    return render_template(
        "admin_completion_analytics.html",
        completion_analytics=completion_analytics
    )


# --------------------------------------------------
# ADMIN: STUDY-TIME ANALYTICS
# --------------------------------------------------

@app.route("/admin/analytics/study-time")
@login_required("admin")
def admin_study_time_analytics():

    study_time_analytics = fetch_all("""
        SELECT
            c.course_id,
            c.course_name,

            COALESCE(
                SUM(ss.duration_seconds),
                0
            ) AS total_study_seconds,

            COUNT(DISTINCT ss.student_id)
                AS students_studied,

            COALESCE(
                ROUND(
                    SUM(ss.duration_seconds) /
                    NULLIF(COUNT(DISTINCT ss.student_id), 0),
                    2
                ),
                0
            ) AS average_seconds_per_student

        FROM courses c

        LEFT JOIN study_sessions ss
            ON c.course_id = ss.course_id

        GROUP BY
            c.course_id,
            c.course_name

        ORDER BY
            total_study_seconds DESC,
            c.course_name ASC
    """)

    total_study_time = fetch_one("""
        SELECT
            COALESCE(SUM(duration_seconds), 0)
                AS total_study_seconds,

            COUNT(DISTINCT student_id)
                AS total_students_studied

        FROM study_sessions
    """)

    return render_template(
        "admin_study_time_analytics.html",
        study_time_analytics=study_time_analytics,
        total_study_time=total_study_time
    )

# --------------------------------------------------
# ADMIN: VISUAL ANALYTICS REPORTS
# Using Pandas, NumPy, and Matplotlib
# --------------------------------------------------

@app.route("/admin/analytics/visual-reports")
@login_required("admin")
def admin_visual_reports():

    # --------------------------------------------------
    # 1. FETCH COURSE ENROLLMENT STATISTICS
    # --------------------------------------------------

    enrollment_data = fetch_all("""
        SELECT
            c.course_name,
            COUNT(e.enrollment_id) AS total_enrollments
        FROM courses c
        LEFT JOIN enrollments e
            ON c.course_id = e.course_id
        GROUP BY c.course_id, c.course_name
        ORDER BY total_enrollments DESC, c.course_name
    """)

    # --------------------------------------------------
    # 2. FETCH AVERAGE COURSE RATINGS
    # --------------------------------------------------

    rating_data = fetch_all("""
        SELECT
            c.course_name,
            ROUND(AVG(r.rating), 2) AS average_rating
        FROM courses c
        LEFT JOIN reviews r
            ON c.course_id = r.course_id
        GROUP BY c.course_id, c.course_name
        ORDER BY c.course_name
    """)

    # --------------------------------------------------
    # 3. FETCH COURSE COMPLETION STATISTICS
    # --------------------------------------------------

    completion_data = fetch_all("""
        SELECT
            c.course_name,
            COALESCE(
                ROUND(
                    100.0 * SUM(
                        CASE
                            WHEN e.progress >= 100 THEN 1
                            ELSE 0
                        END
                    ) / NULLIF(COUNT(e.enrollment_id), 0),
                    2
                ),
                0
            ) AS completion_rate
        FROM courses c
        LEFT JOIN enrollments e
            ON c.course_id = e.course_id
        GROUP BY c.course_id, c.course_name
        ORDER BY c.course_name
    """)

    # --------------------------------------------------
    # 4. FETCH RECORDED STUDY TIME BY COURSE
    # --------------------------------------------------

    study_data = fetch_all("""
        SELECT
            c.course_name,
          COALESCE(
    SUM(
        CASE
            WHEN ss.duration_seconds > 0
                THEN ss.duration_seconds
            ELSE COALESCE(ss.duration_minutes, 0) * 60
        END
    ),
    0
) AS total_study_seconds
        FROM courses c
        LEFT JOIN study_sessions ss
            ON c.course_id = ss.course_id
        GROUP BY c.course_id, c.course_name
        ORDER BY total_study_seconds DESC, c.course_name
    """)

    # --------------------------------------------------
    # HELPER: CREATE MATPLOTLIB CHART
    # --------------------------------------------------

    def create_chart(labels, values, title, y_label):

        figure, axis = plt.subplots(figsize=(9, 4.5))

        if labels:

            axis.bar(labels, values)

            axis.set_title(title)
            axis.set_xlabel("Course")
            axis.set_ylabel(y_label)

            axis.tick_params(
                axis="x",
                labelrotation=35
            )

            for label in axis.get_xticklabels():
                label.set_horizontalalignment("right")

            figure.tight_layout()

        else:

            axis.text(
                0.5,
                0.5,
                "No data available",
                horizontalalignment="center",
                verticalalignment="center",
                transform=axis.transAxes
            )

            axis.set_title(title)
            axis.set_axis_off()

        # Save the chart in memory
        image_buffer = io.BytesIO()

        figure.savefig(
            image_buffer,
            format="png",
            dpi=100,
            bbox_inches="tight"
        )

        plt.close(figure)

        image_buffer.seek(0)

        # Convert the chart to Base64
        image_data = base64.b64encode(
            image_buffer.getvalue()
        ).decode("utf-8")

        image_buffer.close()

        return image_data

    # --------------------------------------------------
    # 5. PREPARE ENROLLMENT DATA USING PANDAS
    # --------------------------------------------------

    enrollment_df = pd.DataFrame(enrollment_data)

    if not enrollment_df.empty:

        enrollment_df["total_enrollments"] = (
            pd.to_numeric(
                enrollment_df["total_enrollments"],
                errors="coerce"
            )
            .fillna(0)
        )

        enrollment_labels = (
            enrollment_df["course_name"].astype(str).tolist()
        )

        enrollment_values = (
            enrollment_df["total_enrollments"]
            .to_numpy(dtype=float)
        )

    else:

        enrollment_labels = []
        enrollment_values = np.array([], dtype=float)

    # --------------------------------------------------
    # 6. PREPARE RATING DATA USING PANDAS
    # --------------------------------------------------

    rating_df = pd.DataFrame(rating_data)

    if not rating_df.empty:

        rating_df["average_rating"] = (
            pd.to_numeric(
                rating_df["average_rating"],
                errors="coerce"
            )
        )

        rating_df["average_rating"] = (
            rating_df["average_rating"].fillna(0)
        )

        rating_labels = (
            rating_df["course_name"].astype(str).tolist()
        )

        rating_values = (
            rating_df["average_rating"]
            .to_numpy(dtype=float)
        )

    else:

        rating_labels = []
        rating_values = np.array([], dtype=float)

    # --------------------------------------------------
    # 7. PREPARE COMPLETION DATA USING PANDAS AND NUMPY
    # --------------------------------------------------

    completion_df = pd.DataFrame(completion_data)

    if not completion_df.empty:

        completion_df["completion_rate"] = (
            pd.to_numeric(
                completion_df["completion_rate"],
                errors="coerce"
            )
            .fillna(0)
        )

        completion_labels = (
            completion_df["course_name"].astype(str).tolist()
        )

        completion_values = np.round(
            np.clip(
                completion_df["completion_rate"].to_numpy(dtype=float),
                0,
                100
            ),
            2
        )

    else:

        completion_labels = []
        completion_values = np.array([], dtype=float)

    # --------------------------------------------------
    # 8. PREPARE STUDY TIME USING PANDAS AND NUMPY
    # --------------------------------------------------

    study_df = pd.DataFrame(study_data)

    if not study_df.empty:

        study_df["total_study_seconds"] = (
            pd.to_numeric(
                study_df["total_study_seconds"],
                errors="coerce"
            )
            .fillna(0)
        )

        study_df["study_hours"] = np.round(
            study_df["total_study_seconds"].to_numpy(dtype=float)
            / 3600,
            2
        )

        study_labels = (
            study_df["course_name"].astype(str).tolist()
        )

        study_values = (
            study_df["study_hours"].to_numpy(dtype=float)
        )

    else:

        study_labels = []
        study_values = np.array([], dtype=float)

    # --------------------------------------------------
    # 9. CREATE THE FOUR MATPLOTLIB CHARTS
    # --------------------------------------------------

    enrollment_chart = create_chart(
        enrollment_labels,
        enrollment_values,
        "Student Enrollments by Course",
        "Number of Enrollments"
    )

    rating_chart = create_chart(
        rating_labels,
        rating_values,
        "Average Course Ratings",
        "Average Rating (out of 5)"
    )

    completion_chart = create_chart(
        completion_labels,
        completion_values,
        "Course Completion Rates",
        "Completion Rate (%)"
    )

    study_chart = create_chart(
        study_labels,
        study_values,
        "Recorded Study Time by Course",
        "Study Time (Hours)"
    )

    # --------------------------------------------------
    # 10. DISPLAY THE VISUAL REPORTS
    # --------------------------------------------------

    return render_template(
        "admin_visual_reports.html",
        enrollment_chart=enrollment_chart,
        rating_chart=rating_chart,
        completion_chart=completion_chart,
        study_chart=study_chart
    )


# --------------------------------------------------
# ADMIN: ALL ANALYTICS
# --------------------------------------------------

@app.route("/admin/analytics")
@login_required("admin")
def admin_all_analytics():

    return render_template("admin_all_analytics.html")



# --------------------------------------------------
# ADMIN: MENTOR APPROVAL REQUESTS
# --------------------------------------------------

@app.route("/admin/mentor-requests")
@login_required("admin")
def admin_mentor_requests():
    pending_mentors = fetch_all("""
        SELECT
            instructor_id,
            name,
            email,
            expertise,
            bio
        FROM instructors
        WHERE approval_status = 'Pending'
        ORDER BY instructor_id DESC
    """)

    return render_template(
        "admin_mentor_requests.html",
        pending_mentors=pending_mentors
    )


# --------------------------------------------------
# ADMIN: APPROVE OR REJECT MENTOR
# --------------------------------------------------

@app.route(
    "/admin/mentor-requests/<int:mentor_id>/<action>",
    methods=["POST"]
)
@login_required("admin")
def update_mentor_approval(mentor_id, action):
    if action not in ("approve", "reject"):
        abort(400)

    mentor = fetch_one(
        "SELECT instructor_id FROM instructors WHERE instructor_id = %s",
        (mentor_id,)
    )

    if not mentor:
        abort(404)

    new_status = "Approved" if action == "approve" else "Rejected"

    execute_query("""
        UPDATE instructors
        SET approval_status = %s
        WHERE instructor_id = %s
    """, (new_status, mentor_id))

    if action == "approve":
        flash("Mentor approved successfully!", "success")
    else:
        flash("Mentor request rejected.", "success")

    return redirect(url_for("admin_mentor_requests"))

# --------------------------------------------------
# ADMIN: VIEW STUDENTS
# --------------------------------------------------

@app.route("/admin/students")
@login_required("admin")
def admin_students():
    students = fetch_all("""
        SELECT student_id, name, email, created_at
        FROM students
        ORDER BY student_id ASC
    """)

    return render_template("admin_students.html", students=students)

# --------------------------------------------------
# ADMIN: VIEW MENTORS
# --------------------------------------------------
@app.route("/admin/mentors")
@login_required("admin")
def admin_mentors():
    mentors = fetch_all("""
        SELECT instructor_id, name, email, expertise, bio
        FROM instructors
        ORDER BY instructor_id ASC
    """)

    return render_template("admin_mentors.html", mentors=mentors)
# --------------------------------------------------
# ADMIN: VIEW COURSES
# --------------------------------------------------

@app.route("/admin/courses")
@login_required("admin")
def admin_courses():
    courses = fetch_all("""
        SELECT
            c.course_id,
            c.course_name,
            c.description,
            c.category,
            c.difficulty,
            c.duration_hours,
            i.name AS instructor_name
        FROM courses c
        LEFT JOIN instructors i ON c.instructor_id = i.instructor_id
        ORDER BY c.course_id ASC
    """)

    return render_template("admin_courses.html", courses=courses)
# --------------------------------------------------
# ADMIN: ADD COURSE
# --------------------------------------------------

@app.route("/admin/courses/add", methods=["GET", "POST"])
@login_required("admin")
def admin_add_course():
    mentors = fetch_all("""
        SELECT instructor_id, name
        FROM instructors
        ORDER BY name
    """)

    if request.method == "POST":
        course_name = request.form.get("course_name", "").strip()
        description = request.form.get("description", "").strip()
        category = request.form.get("category", "").strip()
        difficulty = request.form.get("difficulty", "").strip()
        duration_hours = request.form.get("duration_hours", "").strip()
        instructor_id = request.form.get("instructor_id", "").strip()

        if not course_name:
            flash("Course name is required.", "error")
            return render_template("admin_add_course.html", mentors=mentors)

        try:
            duration = float(duration_hours)
            if duration < 0:
                raise ValueError
        except ValueError:
            flash("Enter a valid non-negative course duration.", "error")
            return render_template("admin_add_course.html", mentors=mentors)

        selected_mentor = None
        if instructor_id:
            selected_mentor = fetch_one(
                "SELECT instructor_id FROM instructors WHERE instructor_id = %s",
                (instructor_id,)
            )
            if not selected_mentor:
                flash("Please select a valid mentor.", "error")
                return render_template("admin_add_course.html", mentors=mentors)

        execute_query("""
            INSERT INTO courses
                (course_name, description, category, difficulty,
                duration_hours, instructor_id)
            VALUES (%s, %s, %s, %s, %s, %s)
        """, (
            course_name, description, category, difficulty,
            duration, instructor_id if instructor_id else None
        ))

        flash("Course added successfully.", "success")
        return redirect(url_for("admin_courses"))

    return render_template("admin_add_course.html", mentors=mentors)



# =========================
# ADMIN COURSE TEACHING REQUESTS
# =========================

@app.route("/admin/course-requests")
@login_required("admin")
def admin_course_requests():

    requests = fetch_all("""
        SELECT
            cr.request_id,
            cr.request_status,
            cr.requested_at,
            i.instructor_id,
            i.name AS mentor_name,
            i.email AS mentor_email,
            c.course_id,
            c.course_name,
            c.instructor_id AS current_instructor_id
        FROM course_requests cr
        JOIN instructors i
            ON cr.instructor_id = i.instructor_id
        JOIN courses c
            ON cr.course_id = c.course_id
        ORDER BY
            CASE
                WHEN cr.request_status = 'Pending' THEN 0
                ELSE 1
            END,
            cr.requested_at DESC
    """)

    return render_template(
        "admin_course_requests.html",
        requests=requests
    )

# =========================
# ADMIN HANDLE COURSE REQUESTS
# =========================

@app.route(
    "/admin/course-requests/<int:request_id>/<action>",
    methods=["POST"]
)
@login_required("admin")
def admin_handle_course_request(request_id, action):

    # Validate the requested action
    if action not in ["approve", "reject"]:
        abort(400)

    # Get the selected course request
    course_request = fetch_one("""
        SELECT
            cr.request_id,
            cr.instructor_id,
            cr.course_id,
            cr.request_status,
            i.name AS mentor_name,
            i.approval_status,
            c.course_name
        FROM course_requests cr
        JOIN instructors i
            ON cr.instructor_id = i.instructor_id
        JOIN courses c
            ON cr.course_id = c.course_id
        WHERE cr.request_id = %s
    """, (request_id,))

    # Check whether the request exists
    if not course_request:
        flash("Course request not found.", "error")
        return redirect(url_for("admin_course_requests"))

    # Process only pending requests
    if course_request["request_status"] != "Pending":
        flash("This request has already been processed.", "info")
        return redirect(url_for("admin_course_requests"))

    # Ensure the mentor account is approved
    if course_request["approval_status"] != "Approved":
        flash(
            "Approve the mentor account before approving a course request.",
            "error"
        )
        return redirect(url_for("admin_course_requests"))

    # Approve the course request
    if action == "approve":

        course_id = course_request["course_id"]
        instructor_id = course_request["instructor_id"]

        # Assign the course to the mentor without removing other mentors
        execute_query("""
            INSERT IGNORE INTO course_instructors
                (course_id, instructor_id)
            VALUES (%s, %s)
        """, (course_id, instructor_id))

        # Update the request status
        execute_query("""
            UPDATE course_requests
            SET request_status = 'Approved'
            WHERE request_id = %s
        """, (request_id,))

        flash(
            f"Course '{course_request['course_name']}' "
            f"has been assigned to {course_request['mentor_name']}.",
            "success"
        )

    # Reject the course request
    else:

        execute_query("""
            UPDATE course_requests
            SET request_status = 'Rejected'
            WHERE request_id = %s
        """, (request_id,))

        flash(
            f"The request for '{course_request['course_name']}' "
            f"has been rejected.",
            "success"
        )

    return redirect(url_for("admin_course_requests"))
@app.route("/student/study-time")
@login_required("student")
def student_study_time():
    student_id = session["user_id"]

    study_summary = fetch_all("""
        SELECT
            c.course_id,
            c.course_name,
            COALESCE(SUM(ss.duration_seconds), 0) AS total_seconds
        FROM enrollments e
        JOIN courses c
            ON e.course_id = c.course_id
        LEFT JOIN study_sessions ss
            ON ss.course_id = c.course_id
            AND ss.student_id = e.student_id
        WHERE e.student_id = %s
        GROUP BY c.course_id, c.course_name
        ORDER BY total_seconds DESC
    """, (student_id,))

    total_seconds = sum(
        int(course["total_seconds"] or 0)
        for course in study_summary
    )

    return render_template(
        "study_time.html",
        study_summary=study_summary,
        total_seconds=total_seconds
    )


@app.route("/student/course/<int:course_id>/reviews", methods=["GET", "POST"])
@login_required("student")
def course_reviews(course_id):
    student_id = session["user_id"]

    course = fetch_one("""
        SELECT course_id, course_name
        FROM courses
        WHERE course_id = %s
    """, (course_id,))

    if not course:
        abort(404)

    enrollment = fetch_one("""
        SELECT course_id
        FROM enrollments
        WHERE student_id = %s AND course_id = %s
    """, (student_id, course_id))

    if not enrollment:
        flash("Enroll in this course before submitting a review.", "error")
        return redirect(url_for("course_detail", course_id=course_id))

    if request.method == "POST":
        rating = request.form.get("rating", "").strip()
        comment = request.form.get("comment", "").strip()

        try:
            rating = int(rating)
        except (TypeError, ValueError):
            flash("Please select a valid rating.", "error")
            return redirect(url_for("course_reviews", course_id=course_id))

        if rating < 1 or rating > 5:
            flash("Rating must be between 1 and 5 stars.", "error")
            return redirect(url_for("course_reviews", course_id=course_id))

        existing_review = fetch_one("""
            SELECT review_id
            FROM reviews
            WHERE student_id = %s AND course_id = %s
        """, (student_id, course_id))

        if existing_review:
            execute_query("""
                UPDATE reviews
                SET rating = %s, comment = %s
                WHERE review_id = %s AND student_id = %s
            """, (rating, comment, existing_review["review_id"], student_id))

            flash("Your review has been updated!", "success")
        else:
            execute_query("""
                INSERT INTO reviews (student_id, course_id, rating, comment)
                VALUES (%s, %s, %s, %s)
            """, (student_id, course_id, rating, comment))

            flash("Your review has been submitted!", "success")

        return redirect(url_for("course_reviews", course_id=course_id))

    my_review = fetch_one("""
        SELECT review_id, rating, comment
        FROM reviews
        WHERE student_id = %s AND course_id = %s
    """, (student_id, course_id))

    reviews = fetch_all("""
        SELECT
            r.rating,
            r.comment,
            r.created_at,
            s.name AS student_name
        FROM reviews r
        JOIN students s ON r.student_id = s.student_id
        WHERE r.course_id = %s
        ORDER BY r.created_at DESC
    """, (course_id,))

    rating_summary = fetch_one("""
        SELECT
            ROUND(AVG(rating), 1) AS average_rating,
            COUNT(*) AS total_reviews
        FROM reviews
        WHERE course_id = %s
    """, (course_id,))

    return render_template(
        "course_reviews.html",
        course=course,
        my_review=my_review,
        reviews=reviews,
        rating_summary=rating_summary
    )
# --------------------------------------------------
# ADMIN: COURSE-WISE ENROLLMENT AND COMPLETION REPORT
# Using Pandas and NumPy
# --------------------------------------------------

@app.route("/admin/analytics/course-enrollments")
@login_required("admin")
def admin_course_enrollments():

    # Fetch course enrollment data from MySQL
    course_enrollments = fetch_all("""
        SELECT
            c.course_id,
            c.course_name,
            i.name AS instructor_name,
            COUNT(e.student_id) AS total_students,
            COALESCE(
                SUM(
                    CASE
                        WHEN e.status = 'Completed'
                        THEN 1
                        ELSE 0
                    END
                ), 0
            ) AS completed_students
        FROM courses c
        LEFT JOIN instructors i
            ON c.instructor_id = i.instructor_id
        LEFT JOIN enrollments e
            ON c.course_id = e.course_id
        GROUP BY
            c.course_id,
            c.course_name,
            i.name
        ORDER BY c.course_id ASC
    """)

    # Convert database results into a Pandas DataFrame
    df = pd.DataFrame(course_enrollments)

    if not df.empty:

        # Calculate completion rates using NumPy
        total_students = df["total_students"].to_numpy(dtype=float)
        completed_students = df["completed_students"].to_numpy(dtype=float)

        df["completion_rate"] = np.round(
            np.divide(
                completed_students * 100,
                total_students,
                out=np.zeros(len(df), dtype=float),
                where=total_students != 0
            ),
            2
        )

        # Convert DataFrame into records for the HTML table
        course_enrollments = df.to_dict(orient="records")

    return render_template(
        "admin_course_enrollments.html",
        course_enrollments=course_enrollments
    )
# --------------------------------------------------
# ADMIN: INACTIVE ENROLLMENT REPORT
# --------------------------------------------------
# INACTIVITY RULE:
# An enrollment is flagged for review if no completed study session
# has been recorded in the previous 14 days.
# This includes enrollments with no recorded completed study session.
# Missing activity does not prove that a student has stopped learning.
@app.route("/admin/analytics/inactive-enrollments")
@login_required("admin")
def admin_inactive_enrollments():

    inactive_enrollments = fetch_all("""
        SELECT
            s.student_id,
            s.name AS student_name,
            c.course_id,
            c.course_name,
            MAX(ss.start_time) AS last_study_date

        FROM enrollments e

        JOIN students s
            ON e.student_id = s.student_id

        JOIN courses c
            ON e.course_id = c.course_id

        LEFT JOIN study_sessions ss
            ON e.student_id = ss.student_id
            AND e.course_id = ss.course_id
            AND ss.end_time IS NOT NULL

        GROUP BY
            s.student_id,
            s.name,
            c.course_id,
            c.course_name

        HAVING
            MAX(ss.start_time) IS NULL
            OR MAX(ss.start_time) < DATE_SUB(
                NOW(),
                INTERVAL 14 DAY
            )

        ORDER BY
    s.student_id ASC,
    c.course_id ASC
    """)

    return render_template(
        "admin_inactive_enrollments.html",
        inactive_enrollments=inactive_enrollments
    )


# --------------------------------------------------
# STUDENT: COURSE COMPLETION CERTIFICATE
# --------------------------------------------------

@app.route("/student/certificate/<int:course_id>")
@login_required("student")
def student_certificate(course_id):

    student_id = session["user_id"]

    certificate = fetch_one("""
        SELECT
            s.name AS student_name,
            c.course_name,
            e.progress,
            e.status
        FROM enrollments e
        JOIN students s
            ON e.student_id = s.student_id
        JOIN courses c
            ON e.course_id = c.course_id
        WHERE e.student_id = %s
        AND e.course_id = %s
    """, (student_id, course_id))

    if not certificate:
        return "You are not enrolled in this course.", 404

    if certificate["status"] != "Completed" or certificate["progress"] < 100:
        return "You must complete this course to receive a certificate.", 403

    return render_template(
        "student_certificate.html",
        certificate=certificate
    )



# --------------------------------------------------
# LOGOUT
# --------------------------------------------------

@app.route("/logout")
def logout():
    session.clear()
    flash("You have been logged out.", "success")
    return redirect(url_for("home"))

# --------------------------------------------------
# STUDENT: VIEW NOTIFICATIONS
# --------------------------------------------------

@app.route("/student/notifications")
@login_required("student")
def student_notifications():
    student_id = session["user_id"]

    notifications = fetch_all("""
        SELECT
            notification_id,
            title,
            message,
            is_read,
            created_at
        FROM notifications
        WHERE student_id = %s
        ORDER BY created_at DESC
    """, (student_id,))

    return render_template(
        "student_notifications.html",
        notifications=notifications
    )


if __name__ == "__main__":
    app.run(debug=False)