"""
Conversation Database

Persistent storage for conversation history using SQLite.
"""

import sqlite3
import json
from pathlib import Path
from typing import Optional
from datetime import datetime


class ConversationDB:
    """SQLite-based persistent conversation storage."""

    def __init__(self, db_path: str = "conversations.db"):
        """
        Initialize the conversation database.

        Args:
            db_path: Path to the SQLite database file
        """
        self.db_path = Path(db_path)
        self._init_db()

    def _init_db(self) -> None:
        """Create the database tables if they don't exist."""
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS conversations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    conversation_key TEXT NOT NULL,
                    role TEXT NOT NULL,
                    content TEXT NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_conversation_key
                ON conversations(conversation_key)
            """)
            # Store session IDs for resuming conversations
            conn.execute("""
                CREATE TABLE IF NOT EXISTS sessions (
                    conversation_key TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            conn.commit()

    def add_message(self, conversation_key: str, role: str, content: str) -> None:
        """
        Add a message to the conversation history.

        Args:
            conversation_key: Unique identifier for the conversation
            role: Either 'user' or 'assistant'
            content: The message content
        """
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "INSERT INTO conversations (conversation_key, role, content) VALUES (?, ?, ?)",
                (conversation_key, role, content)
            )
            conn.commit()

    def get_history(
        self,
        conversation_key: str,
        limit: int = 10
    ) -> list[dict[str, str]]:
        """
        Get the conversation history for a given key.

        Args:
            conversation_key: Unique identifier for the conversation
            limit: Maximum number of messages to return

        Returns:
            List of message dicts with 'role' and 'content' keys
        """
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute(
                """
                SELECT role, content FROM conversations
                WHERE conversation_key = ?
                ORDER BY id DESC
                LIMIT ?
                """,
                (conversation_key, limit)
            )
            rows = cursor.fetchall()

        # Reverse to get chronological order
        return [{"role": row[0], "content": row[1]} for row in reversed(rows)]

    def get_message_count(self, conversation_key: str, role: Optional[str] = None) -> int:
        """
        Get the number of messages in a conversation.

        Args:
            conversation_key: Unique identifier for the conversation
            role: Optional filter by role ('user' or 'assistant')

        Returns:
            Number of messages
        """
        with sqlite3.connect(self.db_path) as conn:
            if role:
                cursor = conn.execute(
                    "SELECT COUNT(*) FROM conversations WHERE conversation_key = ? AND role = ?",
                    (conversation_key, role)
                )
            else:
                cursor = conn.execute(
                    "SELECT COUNT(*) FROM conversations WHERE conversation_key = ?",
                    (conversation_key,)
                )
            return cursor.fetchone()[0]

    def clear_conversation(self, conversation_key: str) -> None:
        """
        Clear all messages for a conversation (keeps session for resumption).

        Args:
            conversation_key: Unique identifier for the conversation
        """
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "DELETE FROM conversations WHERE conversation_key = ?",
                (conversation_key,)
            )
            conn.commit()

    def clear_old_conversations(self, days: int = 30) -> int:
        """
        Clear conversations older than the specified number of days.

        Args:
            days: Number of days to keep

        Returns:
            Number of messages deleted
        """
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute(
                """
                DELETE FROM conversations
                WHERE created_at < datetime('now', ?)
                """,
                (f'-{days} days',)
            )
            # Also clean up old sessions
            conn.execute(
                """
                DELETE FROM sessions
                WHERE updated_at < datetime('now', ?)
                """,
                (f'-{days} days',)
            )
            conn.commit()
            return cursor.rowcount

    def get_session_id(self, conversation_key: str) -> Optional[str]:
        """
        Get the Claude session ID for a conversation.

        Args:
            conversation_key: Unique identifier for the conversation

        Returns:
            Session ID if exists, None otherwise
        """
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute(
                "SELECT session_id FROM sessions WHERE conversation_key = ?",
                (conversation_key,)
            )
            row = cursor.fetchone()
            return row[0] if row else None

    def set_session_id(self, conversation_key: str, session_id: str) -> None:
        """
        Store or update the Claude session ID for a conversation.

        Args:
            conversation_key: Unique identifier for the conversation
            session_id: Claude session ID to store
        """
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                INSERT INTO sessions (conversation_key, session_id, updated_at)
                VALUES (?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(conversation_key) DO UPDATE SET
                    session_id = excluded.session_id,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (conversation_key, session_id)
            )
            conn.commit()

    def clear_session(self, conversation_key: str) -> None:
        """
        Clear the session ID for a conversation.

        Args:
            conversation_key: Unique identifier for the conversation
        """
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "DELETE FROM sessions WHERE conversation_key = ?",
                (conversation_key,)
            )
            conn.commit()
