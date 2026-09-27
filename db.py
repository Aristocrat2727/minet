import os
import asyncpg

DATABASE_URL = os.environ["DATABASE_URL"]
pool = None


async def init_db():
    global pool
    pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=5)
    async with pool.acquire() as conn:
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
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS logs (
                id SERIAL PRIMARY KEY,
                account_id INTEGER REFERENCES accounts(id) ON DELETE CASCADE,
                level TEXT,
                message TEXT,
                created_at TIMESTAMP DEFAULT NOW()
            );
        """)


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
