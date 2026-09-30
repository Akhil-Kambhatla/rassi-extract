from dataclasses import dataclass
from pathlib import Path

import pymupdf

from rassi.config import RENDER_DPI


@dataclass(frozen=True)
class RenderedPage:
    page_number: int
    png_bytes: bytes
    width_px: int
    height_px: int


def count_pages(pdf_path: Path) -> int:
    with pymupdf.open(pdf_path) as document:
        return document.page_count


def render_pages(pdf_path: Path, page_numbers: list[int], dpi: int = RENDER_DPI) -> list[RenderedPage]:
    rendered = []
    with pymupdf.open(pdf_path) as document:
        for page_number in page_numbers:
            pixmap = document[page_number - 1].get_pixmap(dpi=dpi, alpha=False)
            rendered.append(
                RenderedPage(
                    page_number=page_number,
                    png_bytes=pixmap.tobytes("png"),
                    width_px=pixmap.width,
                    height_px=pixmap.height,
                )
            )
    return rendered


def parse_page_selection(selection: str | None, total_pages: int) -> list[int]:
    if selection is None:
        return list(range(1, total_pages + 1))
    selected = set()
    for part in selection.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            start_text, end_text = part.split("-", 1)
            start, end = int(start_text), int(end_text)
            if start > end:
                raise ValueError(f"Invalid page range '{part}': start is after end")
            selected.update(range(start, end + 1))
        else:
            selected.add(int(part))
    if not selected:
        raise ValueError(f"No pages selected by '{selection}'")
    out_of_range = sorted(page for page in selected if page < 1 or page > total_pages)
    if out_of_range:
        raise ValueError(f"Pages {out_of_range} are outside the document, which has {total_pages} pages")
    return sorted(selected)
