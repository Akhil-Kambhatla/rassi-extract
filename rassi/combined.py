import csv
from dataclasses import dataclass, field
from pathlib import Path

from rassi.config import COMBINED_DIRECTORY_NAME
from rassi.filename import SourceFileInfo
from rassi.outputs import wide_columns, write_csv, write_text
from rassi.paths import PERIOD_DIRECTORY_PREFIX, DataLayout, output_paths, per_file_csv_source


@dataclass
class CombinedResult:
    included: list[Path] = field(default_factory=list)
    skipped_old_format: list[Path] = field(default_factory=list)
    student_count: int = 0


def period_sort_key(directory: Path) -> tuple:
    period = directory.name[len(PERIOD_DIRECTORY_PREFIX):]
    return (int(period) if period.isdigit() else float("inf"), directory.name)


def per_file_student_csvs(layout: DataLayout) -> list[tuple[Path, SourceFileInfo]]:
    output_root = layout.output_root
    if not output_root.is_dir():
        return []
    found = []
    teacher_directories = sorted(path for path in output_root.iterdir() if path.is_dir() and path.name != COMBINED_DIRECTORY_NAME)
    for teacher_directory in teacher_directories:
        period_directories = [
            path for path in teacher_directory.iterdir() if path.is_dir() and path.name.startswith(PERIOD_DIRECTORY_PREFIX)
        ]
        for period_directory in sorted(period_directories, key=period_sort_key):
            for path in sorted(period_directory.glob("*.csv")):
                source = per_file_csv_source(path, layout)
                if source is not None:
                    found.append((path, source))
    return found


def read_wide_csv(path: Path) -> tuple[list[str], list[dict]]:
    with path.open(encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        rows = list(reader)
        return list(reader.fieldnames or []), rows


def file_heading(source_pdf: str) -> str:
    return f"===== {source_pdf} ====="


def build_combined_outputs(layout: DataLayout) -> CombinedResult:
    result = CombinedResult()
    columns = wide_columns()
    combined_rows: list[dict] = []
    review_blocks: list[str] = []
    for student_csv, source in per_file_student_csvs(layout):
        header, rows = read_wide_csv(student_csv)
        if header != columns:
            result.skipped_old_format.append(student_csv)
            continue
        result.included.append(student_csv)
        combined_rows += rows
        review_path = output_paths(source, layout).review_txt
        review_text = review_path.read_text(encoding="utf-8").rstrip("\n") if review_path.is_file() else "No review log found for this file."
        review_blocks.append(f"{file_heading(source.source_pdf)}\n\n{review_text}\n")
    result.student_count = len(combined_rows)
    write_csv(layout.combined_students_csv, columns, combined_rows)
    write_text(layout.combined_review_txt, "\n".join(review_blocks) if review_blocks else "No processed files yet.\n")
    return result
