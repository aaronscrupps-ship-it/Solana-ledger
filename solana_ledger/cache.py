import json
import sqlite3
import time
from pathlib import Path
from typing import Dict, List, Optional, Set


class Cache:
    def __init__(self, db_path: str):
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self._init_schema()

    def _init_schema(self):
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS signatures (
                address   TEXT NOT NULL,
                signature TEXT NOT NULL,
                slot      INTEGER,
                block_time INTEGER,
                err       INTEGER DEFAULT 0,
                PRIMARY KEY (address, signature)
            );

            CREATE TABLE IF NOT EXISTS transactions (
                signature  TEXT PRIMARY KEY,
                block_time INTEGER,
                slot       INTEGER,
                data       TEXT NOT NULL,
                fetched_at INTEGER NOT NULL
            );

            -- Tracks whether a full history fetch has completed for each address.
            -- Until this flag is set, incremental early-exit is disabled so a
            -- partial cache from a broken first run doesn't fool the fetcher.
            CREATE TABLE IF NOT EXISTS address_meta (
                address        TEXT PRIMARY KEY,
                fetch_complete INTEGER DEFAULT 0
            );

            CREATE INDEX IF NOT EXISTS idx_sigs_address
                ON signatures (address);
            CREATE INDEX IF NOT EXISTS idx_sigs_time
                ON signatures (address, block_time DESC);
            CREATE INDEX IF NOT EXISTS idx_txn_time
                ON transactions (block_time);
        """)
        self.conn.commit()

    # ── Signature helpers ──────────────────────────────────────────────────

    def get_known_signatures(self, address: str) -> Set[str]:
        cur = self.conn.execute(
            "SELECT signature FROM signatures WHERE address = ?", (address,)
        )
        return {row[0] for row in cur}

    def get_newest_signature(self, address: str) -> Optional[str]:
        """Returns the signature with the highest block_time for this address."""
        cur = self.conn.execute(
            """SELECT signature FROM signatures
               WHERE address = ?
               ORDER BY block_time DESC LIMIT 1""",
            (address,),
        )
        row = cur.fetchone()
        return row[0] if row else None

    def save_signatures(self, address: str, sigs: List[Dict]):
        self.conn.executemany(
            """INSERT OR IGNORE INTO signatures
                   (address, signature, slot, block_time, err)
               VALUES (?, ?, ?, ?, ?)""",
            [
                (
                    address,
                    s["signature"],
                    s.get("slot"),
                    s.get("blockTime"),
                    1 if s.get("err") else 0,
                )
                for s in sigs
            ],
        )
        self.conn.commit()

    def count_signatures(self, address: str) -> int:
        cur = self.conn.execute(
            "SELECT COUNT(*) FROM signatures WHERE address = ?", (address,)
        )
        return cur.fetchone()[0]

    # ── Transaction helpers ────────────────────────────────────────────────

    def get_uncached_signatures(self, address: str) -> List[str]:
        """Return signatures we have in the sig table but no full tx data for."""
        cur = self.conn.execute(
            """SELECT s.signature
               FROM signatures s
               LEFT JOIN transactions t ON s.signature = t.signature
               WHERE s.address = ?
                 AND s.err = 0
                 AND t.signature IS NULL
               ORDER BY s.block_time DESC""",
            (address,),
        )
        return [row[0] for row in cur]

    def save_transactions(self, txns: List[Dict]):
        now = int(time.time())
        rows = []
        for tx in txns:
            if tx is None:
                continue
            sig = tx.get("signature")
            if not sig:
                continue
            rows.append((
                sig,
                tx.get("timestamp") or tx.get("blockTime"),
                tx.get("slot"),
                json.dumps(tx),
                now,
            ))
        if rows:
            self.conn.executemany(
                """INSERT OR REPLACE INTO transactions
                       (signature, block_time, slot, data, fetched_at)
                   VALUES (?, ?, ?, ?, ?)""",
                rows,
            )
            self.conn.commit()

    def get_transactions(self, address: str) -> List[Dict]:
        """Return all fully cached transactions for an address, oldest first."""
        cur = self.conn.execute(
            """SELECT t.data
               FROM transactions t
               JOIN signatures s ON t.signature = s.signature
               WHERE s.address = ?
                 AND s.err = 0
               ORDER BY t.block_time ASC""",
            (address,),
        )
        return [json.loads(row[0]) for row in cur]

    # ── Stats ──────────────────────────────────────────────────────────────

    def get_stats(self, address: str) -> Dict:
        cur = self.conn.execute(
            """SELECT
                   COUNT(s.signature)   AS total_sigs,
                   COUNT(t.signature)   AS cached_txns,
                   MIN(s.block_time)    AS oldest,
                   MAX(s.block_time)    AS newest
               FROM signatures s
               LEFT JOIN transactions t ON s.signature = t.signature
               WHERE s.address = ? AND s.err = 0""",
            (address,),
        )
        row = dict(cur.fetchone())
        row["pending"] = row["total_sigs"] - row["cached_txns"]
        return row

    # ── fetch_complete flag ────────────────────────────────────────────────

    def is_fetch_complete(self, address: str) -> bool:
        """True if a full history fetch has previously completed for this address."""
        cur = self.conn.execute(
            "SELECT fetch_complete FROM address_meta WHERE address = ?", (address,)
        )
        row = cur.fetchone()
        return bool(row and row[0])

    def mark_fetch_complete(self, address: str):
        self.conn.execute(
            """INSERT INTO address_meta (address, fetch_complete) VALUES (?, 1)
               ON CONFLICT(address) DO UPDATE SET fetch_complete = 1""",
            (address,),
        )
        self.conn.commit()

    def clear_wallet(self, address: str):
        """Delete all cached signatures and meta for an address so it refetches from scratch."""
        self.conn.execute("DELETE FROM signatures WHERE address = ?", (address,))
        self.conn.execute("DELETE FROM address_meta WHERE address = ?", (address,))
        self.conn.commit()

    def close(self):
        self.conn.close()
