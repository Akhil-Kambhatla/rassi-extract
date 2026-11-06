import re
from dataclasses import dataclass
from pathlib import Path

from rassi.config import COMBINED_DIRECTORY_NAME
from rassi.filename import FilenameError, SourceFileInfo, parse_source_filename

PROJECT_ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = PROJECT_ROOT / ".env"
DATA_DIR = PROJECT_ROOT / "data"
INPUT_DIR = DATA_DIR / "in"
OUTPUT_DIR = DATA_DIR / "out"
DETAILS_DIRECTORY_NAME = "details"
CACHE_DIR = PROJECT_ROOT / "cache"
EVAL_DIR = PROJECT_ROOT / "eval"

RAW_DIRECTORY_NAME = "raw"
PERIOD_DIRECTORY_PREFIX = "PD"
RAW_PAGE_PATTERN = re.compile(r"^(?P<stem>.+)_page\d+\.json$")


@dataclass(frozen=True)
class DataLayout:
    input_root: Path = INPUT_DIR
    output_root: Path = OUTPUT_DIR
    cache_root: Path = CACHE_DIR
    eval_root: Path = EVAL_DIR
    details_root: Path | None = None

    def __post_init__(self):
        if self.details_root is None:
            object.__setattr__(self, "details_root", self.output_root.parent / DETAILS_DIRECTORY_NAME)

    @property
    def eval_cases_directory(self) -> Path:
        return self.eval_root / "cases"

    @property
    def eval_results_directory(self) -> Path:
        return self.eval_root / "results"

    @property
    def eval_cache_directory(self) -> Path:
        return self.cache_root / "eval"

    @property
    def combined_output_directory(self) -> Path:
        return self.output_root / COMBINED_DIRECTORY_NAME

    @property
    def combined_students_csv(self) -> Path:
        return self.combined_output_directory / "all_students.csv"

    @property
    def combined_review_txt(self) -> Path:
        return self.combined_output_directory / "all_review.txt"

    def teacher_input_directory(self, teacher_code: str) -> Path:
        return self.input_root / teacher_code

    def teacher_output_directory(self, teacher_code: str) -> Path:
        return self.output_root / teacher_code

    def period_output_directory(self, teacher_code: str, period: str) -> Path:
        return self.teacher_output_directory(teacher_code) / f"{PERIOD_DIRECTORY_PREFIX}{period}"

    def period_details_directory(self, teacher_code: str, period: str) -> Path:
        return self.details_root / teacher_code / f"{PERIOD_DIRECTORY_PREFIX}{period}"


DEFAULT_LAYOUT = DataLayout()


def output_stem(source: SourceFileInfo) -> str:
    return Path(source.source_pdf).stem


def raw_pages_in(directory: Path, stem: str) -> list[Path]:
    if not directory.is_dir():
        return []
    return sorted(
        path
        for path in directory.iterdir()
        if path.is_file() and (match := RAW_PAGE_PATTERN.match(path.name)) and match.group("stem") == stem
    )


@dataclass(frozen=True)
class OutputPaths:
    directory: Path
    details_directory: Path
    stem: str
    wide_csv: Path
    long_csv: Path
    flags_csv: Path
    metrics_csv: Path
    manifest_json: Path
    review_txt: Path
    raw_directory: Path
    legacy_files: tuple[Path, ...]
    legacy_raw_directory: Path

    def raw_page(self, page_number: int) -> Path:
        return self.raw_directory / f"{self.stem}_page{page_number:02d}.json"

    def visible_files(self) -> list[Path]:
        return [self.wide_csv, self.flags_csv, self.review_txt]

    def detail_files(self) -> list[Path]:
        return [self.long_csv, self.metrics_csv, self.manifest_json]

    def fixed_files(self) -> list[Path]:
        return self.visible_files() + self.detail_files()

    def existing_raw_pages(self) -> list[Path]:
        return raw_pages_in(self.raw_directory, self.stem)

    def existing_legacy_files(self) -> list[Path]:
        return [path for path in self.legacy_files if path.is_file()] + raw_pages_in(self.legacy_raw_directory, self.stem)

    def existing_files(self) -> list[Path]:
        return [path for path in self.fixed_files() if path.is_file()] + self.existing_raw_pages() + self.existing_legacy_files()

    def manifest_candidates(self) -> list[Path]:
        return [self.manifest_json, self.directory / f"{self.stem}_manifest.json"]


def output_paths(source: SourceFileInfo, layout: DataLayout = DEFAULT_LAYOUT) -> OutputPaths:
    directory = layout.period_output_directory(source.teacher_code, source.period)
    details_directory = layout.period_details_directory(source.teacher_code, source.period)
    stem = output_stem(source)
    return OutputPaths(
        directory=directory,
        details_directory=details_directory,
        stem=stem,
        wide_csv=directory / f"{stem}.csv",
        flags_csv=directory / f"{stem}_flags.csv",
        review_txt=directory / f"{stem}_review.txt",
        long_csv=details_directory / f"{stem}_long.csv",
        metrics_csv=details_directory / f"{stem}_metrics.csv",
        manifest_json=details_directory / f"{stem}_manifest.json",
        raw_directory=details_directory / RAW_DIRECTORY_NAME,
        legacy_files=tuple(
            directory / f"{stem}{suffix}" for suffix in ("_wide.csv", "_long.csv", "_metrics.csv", "_manifest.json")
        ),
        legacy_raw_directory=directory / RAW_DIRECTORY_NAME,
    )


def per_file_csv_source(path: Path, layout: DataLayout = DEFAULT_LAYOUT) -> SourceFileInfo | None:
    if path.suffix != ".csv":
        return None
    try:
        source = parse_source_filename(f"{path.stem}.pdf")
    except FilenameError:
        return None
    if output_paths(source, layout).wide_csv != path:
        return None
    return source


def display_path(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(PROJECT_ROOT))
    except ValueError:
        return str(path)
