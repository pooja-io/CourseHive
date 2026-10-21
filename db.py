import mysql.connector
from dotenv import load_dotenv
import os

load_dotenv()


def get_db_connection():
    connection = mysql.connector.connect(
        host=os.getenv("DB_HOST", "localhost"),
        port=int(os.getenv("DB_PORT", "3306")),
        user=os.getenv("DB_USER", "root"),
        password=os.getenv("DB_PASSWORD", ""),
        database=os.getenv("DB_NAME", "coursehive_db"),
        ssl_disabled=False
    )

    return connection