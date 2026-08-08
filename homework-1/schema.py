"""
Lakebase schema + sample data.

`init_db()` runs at app startup: it creates the tables if they are missing and
seeds sample rows only when the tickets table is empty, so restarting the app
never duplicates or overwrites real data.
"""

import logging

import lakebase

logger = logging.getLogger(__name__)

STATUSES = ["open", "in_progress", "resolved", "closed"]
PRIORITIES = ["low", "medium", "high", "urgent"]
CATEGORIES = ["general", "access", "billing", "bug", "hardware"]

DDL = [
    """
    CREATE TABLE IF NOT EXISTS users (
        user_id      SERIAL PRIMARY KEY,
        username     TEXT NOT NULL UNIQUE,
        email        TEXT NOT NULL UNIQUE,
        created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS tickets (
        ticket_id    SERIAL PRIMARY KEY,
        title        TEXT NOT NULL,
        status       TEXT NOT NULL DEFAULT 'open'
                     CHECK (status IN ('open', 'in_progress', 'resolved', 'closed')),
        priority     TEXT NOT NULL DEFAULT 'medium'
                     CHECK (priority IN ('low', 'medium', 'high', 'urgent')),
        category     TEXT NOT NULL DEFAULT 'general'
                     CHECK (category IN ('general', 'access', 'billing', 'bug', 'hardware')),
        created_by   INTEGER NOT NULL REFERENCES users(user_id),
        created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
        updated_at   TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS ticket_messages (
        message_id   SERIAL PRIMARY KEY,
        ticket_id    INTEGER NOT NULL REFERENCES tickets(ticket_id) ON DELETE CASCADE,
        message_text TEXT NOT NULL,
        author       INTEGER NOT NULL REFERENCES users(user_id),
        created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    "CREATE INDEX IF NOT EXISTS ix_ticket_messages_ticket_id ON ticket_messages (ticket_id)",
    "CREATE INDEX IF NOT EXISTS ix_tickets_status ON tickets (status)",
]

SAMPLE_USERS = [
    ("alice.nguyen", "alice.nguyen@example.com"),
    ("brian.ortiz", "brian.ortiz@example.com"),
    ("chen.wu", "chen.wu@example.com"),
    ("dana.silva", "dana.silva@example.com"),
]

# (title, status, priority, category, creator_username, [(author_username, message_text), ...])
SAMPLE_TICKETS = [
    (
        "VPN disconnects every few minutes",
        "open",
        "high",
        "access",
        "alice.nguyen",
        [
            ("alice.nguyen", "The VPN client drops about every 5 minutes since Monday."),
            ("brian.ortiz", "Thanks for reporting. Which VPN client version are you on?"),
            ("alice.nguyen", "Version 4.10.2 on macOS."),
        ],
    ),
    (
        "Cannot access the finance dashboard",
        "in_progress",
        "medium",
        "access",
        "chen.wu",
        [
            ("chen.wu", "I get a 403 when opening the finance dashboard."),
            ("dana.silva", "You are missing the finance_readers group. Requesting it now."),
        ],
    ),
    (
        "Duplicate charge on the September invoice",
        "resolved",
        "urgent",
        "billing",
        "dana.silva",
        [
            ("dana.silva", "September invoice shows the platform fee twice."),
            ("brian.ortiz", "Confirmed a duplicate line item, refund issued today."),
            ("dana.silva", "Refund received, thanks. Closing this out."),
        ],
    ),
    (
        "Laptop fan runs constantly after the last update",
        "open",
        "low",
        "hardware",
        "brian.ortiz",
        [
            ("brian.ortiz", "Fan is at full speed even when the machine is idle."),
            ("chen.wu", "Can you send the output of the diagnostics tool?"),
        ],
    ),
]


def init_db() -> None:
    """Create the schema if needed, then seed sample data if the app is empty."""
    lakebase.execute_script([(sql, None) for sql in DDL])
    logger.info("Lakebase schema ready")

    existing = lakebase.query_one("SELECT COUNT(*) AS n FROM tickets")
    if existing and existing["n"] > 0:
        logger.info("Tickets already present (%s rows), skipping seed", existing["n"])
        return

    _seed()
    logger.info("Seeded sample users, tickets and messages")


def _seed() -> None:
    """Insert the sample users, tickets and messages in one transaction."""
    with lakebase.get_connection() as conn:
        with conn.cursor() as cur:
            user_ids = {}
            for username, email in SAMPLE_USERS:
                cur.execute(
                    """
                    INSERT INTO users (username, email)
                    VALUES (%s, %s)
                    ON CONFLICT (username) DO UPDATE SET email = EXCLUDED.email
                    RETURNING user_id
                    """,
                    (username, email),
                )
                user_ids[username] = cur.fetchone()["user_id"]

            for title, status, priority, category, creator, messages in SAMPLE_TICKETS:
                cur.execute(
                    """
                    INSERT INTO tickets (title, status, priority, category, created_by)
                    VALUES (%s, %s, %s, %s, %s)
                    RETURNING ticket_id
                    """,
                    (title, status, priority, category, user_ids[creator]),
                )
                ticket_id = cur.fetchone()["ticket_id"]

                for author, text in messages:
                    cur.execute(
                        """
                        INSERT INTO ticket_messages (ticket_id, message_text, author)
                        VALUES (%s, %s, %s)
                        """,
                        (ticket_id, text, user_ids[author]),
                    )
        conn.commit()
