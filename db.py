import os
import asyncpg

DATABASE_URL = os.environ["DATABASE_URL"]
pool = None


# =========================================================
#                    ИНИЦИАЛИЗАЦИЯ
# =========================================================
async def init_db():
    global pool
    pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=5)
    async with pool.acquire() as conn:
        # ===== АККАУНТЫ =====
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS accounts (
                id SERIAL PRIMARY KEY,
                phone TEXT,
                session_str TEXT UNIQUE NOT NULL,
                username TEXT,
                status TEXT DEFAULT 'active',
                bonuses INTEGER DEFAULT 0,
                owner_id BIGINT,
                created_at TIMESTAMP DEFAULT NOW(),
                last_run_at TIMESTAMP,
                last_error TEXT
            );
        """)
        await conn.execute("ALTER TABLE accounts ADD COLUMN IF NOT EXISTS owner_id BIGINT;")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_accounts_status ON accounts(status);")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_accounts_owner_id ON accounts(owner_id);")

        # ===== ЛОГИ =====
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS logs (
                id SERIAL PRIMARY KEY,
                account_id INTEGER REFERENCES accounts(id) ON DELETE CASCADE,
                level TEXT,
                message TEXT,
                created_at TIMESTAMP DEFAULT NOW()
            );
        """)
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_logs_account ON logs(account_id);")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_logs_created ON logs(created_at DESC);")

        # ===== SETTINGS =====
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT,
                updated_at TIMESTAMP DEFAULT NOW()
            );
        """)
        await conn.execute("""
            INSERT INTO settings (key, value) VALUES ('fake_accounts_offset', '0')
            ON CONFLICT (key) DO NOTHING;
        """)

        # ===== ПОДПИСЧИКИ =====
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS subscribers (
                user_id BIGINT PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                subscribed BOOLEAN DEFAULT FALSE,
                joined_at TIMESTAMP DEFAULT NOW(),
                last_check TIMESTAMP
            );
        """)
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_subscribers_sub ON subscribers(subscribed);")


# =========================================================
#                      АККАУНТЫ
# =========================================================
async def add_account(phone: str, session_str: str, username: str, owner_id: int | None = None) -> int:
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """INSERT INTO accounts (phone, session_str, username, status, owner_id)
               VALUES ($1, $2, $3, 'active', $4)
               ON CONFLICT (session_str) DO UPDATE
               SET phone=$1, username=$3, status='active', last_error=NULL, owner_id=$4
               RETURNING id""",
            phone, session_str, username, owner_id
        )
        return row["id"]


async def get_accounts(status: str | None = None) -> list:
    async with pool.acquire() as conn:
        if status:
            rows = await conn.fetch("SELECT * FROM accounts WHERE status=$1 ORDER BY id", status)
        else:
            rows = await conn.fetch("SELECT * FROM accounts ORDER BY id")
        return [dict(r) for r in rows]


async def get_account(acc_id: int):
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM accounts WHERE id=$1", acc_id)
        return dict(row) if row else None


async def delete_account(acc_id: int) -> bool:
    async with pool.acquire() as conn:
        result = await conn.execute("DELETE FROM accounts WHERE id=$1", acc_id)
        return result == "DELETE 1"


async def update_status(acc_id: int, status: str, error: str | None = None):
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE accounts SET status=$1, last_error=$2 WHERE id=$3",
            status, error, acc_id
        )


async def increment_bonus(acc_id: int):
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE accounts SET bonuses = bonuses + 1, last_run_at = NOW() WHERE id=$1",
            acc_id
        )


async def count_accounts(status: str | None = None) -> int:
    async with pool.acquire() as conn:
        if status:
            row = await conn.fetchrow("SELECT COUNT(*) AS cnt FROM accounts WHERE status=$1", status)
        else:
            row = await conn.fetchrow("SELECT COUNT(*) AS cnt FROM accounts")
        return int(row["cnt"]) if row else 0


async def count_user_accounts(user_id: int) -> int:
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT COUNT(*) AS cnt FROM accounts WHERE owner_id=$1", user_id
        )
        return int(row["cnt"]) if row else 0


async def count_total_bonuses() -> int:
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT COALESCE(SUM(bonuses), 0) AS total FROM accounts")
        return int(row["total"]) if row else 0


# =========================================================
#                        ЛОГИ
# =========================================================
async def add_log(account_id: int, level: str, message: str):
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO logs (account_id, level, message) VALUES ($1, $2, $3)",
            account_id, level, message[:500]
        )


async def get_recent_logs(limit: int = 30) -> list:
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT * FROM logs ORDER BY id DESC LIMIT $1", limit)
        return [dict(r) for r in rows]


async def clear_logs():
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM logs")


# =========================================================
#                      SETTINGS
# =========================================================
async def get_setting(key: str, default: str = "") -> str:
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT value FROM settings WHERE key=$1", key)
        return row["value"] if row else default


async def set_setting(key: str, value: str):
    async with pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO settings (key, value, updated_at) VALUES ($1, $2, NOW())
            ON CONFLICT (key) DO UPDATE SET value=$2, updated_at=NOW()
        """, key, value)


async def get_fake_accounts_offset() -> int:
    try:
        val = await get_setting("fake_accounts_offset", "0")
        return int(val)
    except Exception:
        return 0


async def set_fake_accounts_offset(value: int):
    await set_setting("fake_accounts_offset", str(value))


# =========================================================
#                      ПОДПИСЧИКИ
# =========================================================
async def add_subscriber(user_id: int, username: str = "", first_name: str = ""):
    async with pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO subscribers (user_id, username, first_name)
            VALUES ($1, $2, $3)
            ON CONFLICT (user_id) DO UPDATE
            SET username=$2, first_name=$3, last_check=NOW()
        """, user_id, username, first_name)


async def mark_subscribed(user_id: int, subscribed: bool):
    async with pool.acquire() as conn:
        await conn.execute("""
            UPDATE subscribers SET subscribed=$1, last_check=NOW() WHERE user_id=$2
        """, subscribed, user_id)


async def is_subscriber(user_id: int) -> bool:
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT subscribed FROM subscribers WHERE user_id=$1", user_id
        )
        return bool(row and row["subscribed"])


async def count_subscribers() -> int:
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT COUNT(*) AS cnt FROM subscribers")
        return int(row["cnt"]) if row else 0


async def get_subscriber(user_id: int):
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM subscribers WHERE user_id=$1", user_id)
        return dict(row) if row else None
