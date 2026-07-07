import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest
import aiosqlite
from main import prune_old_checkpoints

@pytest.mark.asyncio
async def test_prune_old_checkpoints():
    # Use an in-memory SQLite database
    async with aiosqlite.connect(":memory:") as conn:
        # Create checkpoints and writes tables matching LangGraph saver schema
        await conn.execute("""
            CREATE TABLE checkpoints (
                thread_id TEXT NOT NULL,
                checkpoint_id TEXT NOT NULL,
                parent_id TEXT,
                checkpoint BLOB,
                metadata BLOB,
                PRIMARY KEY (thread_id, checkpoint_id)
            )
        """)
        await conn.execute("""
            CREATE TABLE writes (
                thread_id TEXT NOT NULL,
                checkpoint_id TEXT NOT NULL,
                task_id TEXT NOT NULL,
                idx INTEGER NOT NULL,
                channel TEXT NOT NULL,
                value BLOB,
                PRIMARY KEY (thread_id, checkpoint_id, task_id, idx)
            )
        """)
        await conn.commit()

        # Insert 7 threads (thread-1 to thread-7)
        # Thread-1 is inserted first (lowest rowid), thread-7 is inserted last (highest rowid)
        for i in range(1, 8):
            thread_id = f"thread-{i}"
            # Insert to checkpoints
            await conn.execute(
                "INSERT INTO checkpoints (thread_id, checkpoint_id, parent_id) VALUES (?, ?, ?)",
                (thread_id, f"chk-{i}", None)
            )
            # Insert matching writes
            await conn.execute(
                "INSERT INTO writes (thread_id, checkpoint_id, task_id, idx, channel) VALUES (?, ?, ?, ?, ?)",
                (thread_id, f"chk-{i}", f"task-{i}", 0, "output")
            )
        await conn.commit()

        # Verify 7 threads exist before pruning
        async with conn.execute("SELECT DISTINCT thread_id FROM checkpoints") as cursor:
            threads_before = [row[0] for row in await cursor.fetchall()]
        assert len(threads_before) == 7

        # Prune keeping last 5
        await prune_old_checkpoints(conn, keep_last_n=5)

        # Check remaining threads in checkpoints
        async with conn.execute("SELECT DISTINCT thread_id FROM checkpoints") as cursor:
            threads_after_chk = [row[0] for row in await cursor.fetchall()]
        
        # Check remaining threads in writes
        async with conn.execute("SELECT DISTINCT thread_id FROM writes") as cursor:
            threads_after_w = [row[0] for row in await cursor.fetchall()]

        # The 5 most recent threads (thread-3, thread-4, thread-5, thread-6, thread-7) should remain
        expected_threads = [f"thread-{i}" for i in range(3, 8)]
        
        assert sorted(threads_after_chk) == sorted(expected_threads)
        assert sorted(threads_after_w) == sorted(expected_threads)
