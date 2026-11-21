import asyncio
import sqlite3
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime

from pixel.api.board import Board
from pixel.api.gateway import Gateway
from pixel.api.mailbox import Mailbox
from pixel.api.portal import Portal
from pixel.config import Settings


@dataclass
class Context:
    """Everything a handler needs. Built once in `pixel/app.py`, passed down, never imported as a global."""

    settings: Settings
    db: sqlite3.Connection
    mailbox: Mailbox
    portal: Portal
    gateway: Gateway
    board: Board
    tasks: set[asyncio.Task] = field(default_factory=set)  # running handlers, kept so they are not collected
    mail_times: deque[datetime] = field(default_factory=deque)  # our sends in the last minute (R7)
