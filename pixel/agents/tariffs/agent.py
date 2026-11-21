from pathlib import Path

from pixel.contracts import LaneTariff, Message
from pixel.core.context import Context


def parse(filename: str, content: bytes) -> list[LaneTariff]:
    """Code parsers per file type (xlsx, docx, txt, text pdf). Raises ValueError when the layout is unknown."""
    raise NotImplementedError


async def read_with_model(ctx: Context, filename: str, pages: tuple[bytes, ...]) -> list[LaneTariff]:
    """Images, scanned PDFs and layouts `parse` refuses: one `llm.ask_json` call per document."""
    raise NotImplementedError


def check(tariffs: list[LaneTariff]) -> list[str]:
    """Code checks on every parse: rates fall as weight rises, minimum > 0, lane codes known, dates sane.
    A sheet with findings is stored as failed, not used for pricing."""
    raise NotImplementedError


async def handle(ctx: Context, message: Message) -> None:
    """A tariff mail: download attachments, ingest each, emit `tariff.ingested` or `tariff.failed`."""
    raise NotImplementedError


async def ingest_folder(ctx: Context, folder: Path) -> None:
    """The start kit: every tariff file, plus the carrier list and pickup zones, into `pricing/store.py`."""
    raise NotImplementedError
