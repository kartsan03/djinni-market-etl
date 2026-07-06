import os
import sqlite3

from dotenv import load_dotenv


def connect():
    load_dotenv()
    path = os.getenv("DB_PATH", "djinni.db")
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn
