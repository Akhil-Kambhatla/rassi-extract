import re
from dataclasses import dataclass
from pathlib import Path

from rassi.filename import SourceFileInfo

PROJECT_ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = PROJECT_ROOT / ".env"
DATA_DIR = PROJECT_ROOT / "data"
INPUT_DIR = DATA_DIR / "in"
OUTPUT_DIR = DATA_DIR / "out"
CACHE_DIR = PROJECT_ROOT / "cache"

RAW_DIRECTORY_NAME = "raw"
RAW_PAGE_PATTERN = re.compile(r"^(?P<stem>.+)_page\d+\.json$")


@dataclass(frozen=True)
class DataLayout:
    input_root: Path = INPUT_DIR
    output_root: Path = OUTPUT_DIR
    cache_root: Path = CACHE_DIR

    def teacher_input_directory(self, teacher_code: str) -> Path:
        return self.input_root / teacher_code

    def teacher_output_directory(self, teacher_code: str) -> Path:
        return self.output_root / teacher_code


DEFAULT_LAYOUT = DataLayout()


def output_stem(source: SourceFileInfo) -> str:
    return Path(source.source_pdf).stem


@dataclass(frozen=True)
class OutputPaths:
    directory: Path
    stem: str
    wide_csv: Path
    long_csv: Path
    flags_csv: Path
    metrics_csv: Path
    manifest_json: Path
    raw_directory: Path

    def raw_page(self, page_number: int) -> Path:
        return self.raw_directory / f"{self.stem}_page{page_number:02d}.json"

    def fixed_files(self) -> list[Path]:
        return [self.wide_csv, self.long_csv, self.flags_csv, self.metrics_csv, self.manifest_json]

    def existing_raw_pages(self) -> list[Path]:
        if not self.raw_directory.is_dir():
            return []
        return sorted(
            path
            for path in self.raw_directory.iterdir()
            if path.is_file() and (match := RAW_PAGE_PATTERN.match(path.name)) and match.group("stem") == self.stem
        )

    def existing_files(self) -> list[Path]:
        return [path for path in self.fixed_files() if path.is_file()] + self.existing_raw_pages()


def output_paths(source: SourceFileInfo, layout: DataLayout = DEFAULT_LAYOUT) -> OutputPaths:
    directory = layout.teacher_output_directory(source.teacher_code)
    stem = output_stem(source)
    return OutputPaths(
        directory=directory,
        stem=stem,
        wide_csv=directory / f"{stem}_wide.csv",
        long_csv=directory / f"{stem}_long.csv",
        flags_csv=directory / f"{stem}_flags.csv",
        metrics_csv=directory / f"{stem}_metrics.csv",
        manifest_json=directory / f"{stem}_manifest.json",
        raw_directory=directory / RAW_DIRECTORY_NAME,
    )


def display_path(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(PROJECT_ROOT))
    except ValueError:
        return str(path)
