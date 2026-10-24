import aiosqlite

DB = "villas.db"


async def init_db():
    async with aiosqlite.connect(DB) as db:
        await db.executescript("""
        CREATE TABLE IF NOT EXISTS villas(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER, url TEXT, title TEXT, added_by INTEGER,
            UNIQUE(chat_id, url));
        CREATE TABLE IF NOT EXISTS votes(
            chat_id INTEGER, user_id INTEGER, villa_id INTEGER, name TEXT,
            PRIMARY KEY(chat_id, user_id));
        CREATE TABLE IF NOT EXISTS chats(chat_id INTEGER PRIMARY KEY, closed INTEGER DEFAULT 0);
        """)
        await db.commit()
