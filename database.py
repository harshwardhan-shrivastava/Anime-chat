import sqlite3

DATABASE = "animechat.db"


def get_connection():

    conn = sqlite3.connect(DATABASE)
    conn.row_factory = sqlite3.Row

    return conn


def create_tables():

    conn = get_connection()

    cursor = conn.cursor()

    cursor.execute("""

    CREATE TABLE IF NOT EXISTS anime_ratings(

        anime_slug TEXT PRIMARY KEY,

        total_rating INTEGER DEFAULT 0,

        total_votes INTEGER DEFAULT 0

    )

    """)

    cursor.execute("""

    CREATE TABLE IF NOT EXISTS reviews(

        id INTEGER PRIMARY KEY AUTOINCREMENT,

        anime_slug TEXT,

        username TEXT,

        rating INTEGER,

        comment TEXT

    )

    """)

    cursor.execute("""

    CREATE TABLE IF NOT EXISTS episode_ratings(

        id INTEGER PRIMARY KEY AUTOINCREMENT,

        anime_slug TEXT,

        season_name TEXT,

        episode_number INTEGER,

        total_rating INTEGER DEFAULT 0,

        total_votes INTEGER DEFAULT 0

    )

    """)

    conn.commit()

    conn.close()