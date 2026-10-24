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
            PRIMARY KEY(chat_id, user_id, villa_id));
        CREATE TABLE IF NOT EXISTS chats(chat_id INTEGER PRIMARY KEY, closed INTEGER DEFAULT 0);
        """)
        await migrate_votes(db)  # upgrade an old one-vote-per-person table
        await add_column(db, "villas", "price", "TEXT")
        await drop_column(db, "villas", "capacity")
        await add_column(db, "villas", "rating", "TEXT")
        await add_column(db, "villas", "ratingCount", "TEXT")
        await add_column(db, "villas", "location", "TEXT")
        await db.commit()


async def add_column(db, table, col, decl):
    cols = [
        r[1] for r in await (await db.execute(f"PRAGMA table_info({table})")).fetchall()
    ]
    if col not in cols:
        await db.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")


async def drop_column(db, table, col):
    cols = [
        r[1] for r in await (await db.execute(f"PRAGMA table_info({table})")).fetchall()
    ]
    if col in cols:
        await db.execute(f"ALTER TABLE {table} DROP COLUMN {col}")


async def migrate_votes(db):
    info = await (await db.execute("PRAGMA table_info(votes)")).fetchall()
    if sum(1 for r in info if r[5]) == 3:  # already (chat_id, user_id, villa_id)
        return
    await db.executescript("""
    CREATE TABLE votes_new(
        chat_id INTEGER, user_id INTEGER, villa_id INTEGER, name TEXT,
        PRIMARY KEY(chat_id, user_id, villa_id));
    INSERT OR IGNORE INTO votes_new SELECT chat_id,user_id,villa_id,name FROM votes;
    DROP TABLE votes;
    ALTER TABLE votes_new RENAME TO votes;
    """)
