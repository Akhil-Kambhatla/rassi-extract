import re
from dataclasses import dataclass
from pathlib import Path

FILENAME_PATTERN = re.compile(r"^(?P<teacher>[A-Za-z0-9]+)_PD(?P<period>\d+)_(?P<form>RASSI)\.pdf$")
EXPECTED_FORMAT = "<teacher_code>_PD<period>_RASSI.pdf, for example 020421_PD1_RASSI.pdf"


class FilenameError(ValueError):
    pass


@dataclass(frozen=True)
class SourceFileInfo:
    teacher_code: str
    period: str
    form: str
    source_pdf: str


def parse_source_filename(path: str | Path) -> SourceFileInfo:
    name = Path(path).name
    match = FILENAME_PATTERN.match(name)
    if match is None:
        raise FilenameError(f"Filename '{name}' does not match the expected format {EXPECTED_FORMAT}")
    return SourceFileInfo(
        teacher_code=match.group("teacher"),
        period=match.group("period"),
        form=match.group("form"),
        source_pdf=name,
    )


def normalize_period(period: str | None) -> str | None:
    if period is None:
        return None
    stripped = period.strip().upper().removeprefix("PD").strip()
    if stripped.isdigit():
        return str(int(stripped))
    return stripped
