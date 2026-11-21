import sqlite3
from dataclasses import dataclass

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
