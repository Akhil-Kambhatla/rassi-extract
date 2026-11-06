import csv
import json
from dataclasses import dataclass
from pathlib import Path

from rassi.config import (
    ALL_ITEM_NUMBERS,
    FLAG_VALUES,
    MARK_CONFIDENCE_CLEAR,
    MARK_CONFIDENCE_UNSURE,
    MISSING_VALUE_CODE,
    MULTIPLE_MARK_FLAG,
    MULTIPLE_MARK_SEPARATOR,
    REDACTED_SID,
    REVIEW_CHECK_WORD,
    UNSURE_VALUE_MARKER,
)
from rassi.filename import SourceFileInfo
from rassi.metrics import PageMetric
from rassi.paths import OutputPaths
from rassi.schema import ItemExtraction
from rassi.structure import IdentityFlag, Student

DIMENSIONS = (("A", "agreement"), ("B", "reason"), ("C", "confidence"))
DIMENSION_DISPLAY_NAMES = {"A": "Agreement", "B": "Reason", "C": "Confidence"}
BLANK_VALUE = "blank"
AMBIGUOUS_VALUE = "ambiguous"
WIDE_ID_COLUMNS = ["sid", "teacher_code", "period", "form", "pre_post", "source_pdf", "page_start"]
WIDE_REVIEW_COLUMNS = ["n_multiple", "n_uncertain", "n_blank", "check"]
LONG_COLUMNS = ["sid", "item", "dimension", "value", "value_marked", "mark_confidence", "value_num", "mark_notes", "page_number", "model"]
FLAG_COLUMNS = ["sid", "page_number", "item", "dimension", "flag_type", "value", "reason"]
METRICS_COLUMNS = [
    "page_number",
    "status",
    "sid",
    "page_type",
    "latency_seconds",
    "input_tokens",
    "cached_input_tokens",
    "output_tokens",
    "reasoning_tokens",
    "cost_usd",
    "cache_hit",
    "retry_count",
    "model",
    "error",
]


REASON_DIMENSION = "B"


def is_multiple_mark(value: str) -> bool:
    return MULTIPLE_MARK_SEPARATOR in value


def flag_kind(value: str) -> str | None:
    if value in FLAG_VALUES:
        return value
    if is_multiple_mark(value):
        return MULTIPLE_MARK_FLAG
    return None


def numeric_value(dimension: str, value: str):
    if dimension == REASON_DIMENSION:
        return ""
    return int(value) if value.isdigit() else MISSING_VALUE_CODE


def wide_columns() -> list[str]:
    columns = list(WIDE_ID_COLUMNS)
    for item_number in ALL_ITEM_NUMBERS:
        columns += [f"Q{item_number}_A", f"Q{item_number}_B", f"Q{item_number}_C"]
    return columns + WIDE_REVIEW_COLUMNS


def effective_mark_confidence(value: str, reported: str) -> str:
    if value == BLANK_VALUE:
        return MARK_CONFIDENCE_CLEAR
    if value == AMBIGUOUS_VALUE:
        return MARK_CONFIDENCE_UNSURE
    return reported


def marked_value(value: str, mark_confidence: str) -> str:
    if mark_confidence == MARK_CONFIDENCE_UNSURE and value != BLANK_VALUE:
        return value + UNSURE_VALUE_MARKER
    return value


@dataclass(frozen=True)
class ValueReading:
    page_number: int
    item_number: int
    dimension: str
    value: str
    mark_confidence: str
    notes: str

    @property
    def marked(self) -> str:
        return marked_value(self.value, self.mark_confidence)

    @property
    def is_unsure(self) -> bool:
        return self.mark_confidence == MARK_CONFIDENCE_UNSURE

    @property
    def is_blank(self) -> bool:
        return self.value == BLANK_VALUE

    @property
    def is_multiple(self) -> bool:
        return is_multiple_mark(self.value)

    @property
    def row_name(self) -> str:
        return DIMENSION_DISPLAY_NAMES[self.dimension]


def student_items(student: Student) -> list[tuple[int, ItemExtraction]]:
    located = [(student.header_page_number, item) for item in student.header.items]
    located += [(student.continuation_page_number, item) for item in student.continuation.items]
    return sorted(located, key=lambda pair: pair[1].item_number)


def sid_text(student: Student) -> str:
    return student.sid if student.sid is not None else ""


def student_readings(student: Student) -> list[ValueReading]:
    readings = []
    for page_number, item in student_items(student):
        for dimension, field_name in DIMENSIONS:
            value = getattr(item, field_name)
            readings.append(
                ValueReading(
                    page_number=page_number,
                    item_number=item.item_number,
                    dimension=dimension,
                    value=value,
                    mark_confidence=effective_mark_confidence(value, getattr(item, f"{field_name}_mark_confidence")),
                    notes=getattr(item, f"{field_name}_notes"),
                )
            )
    return readings


def build_wide_rows(students: list[Student], source: SourceFileInfo) -> list[dict]:
    rows = []
    for student in students:
        row = {
            "sid": sid_text(student),
            "teacher_code": source.teacher_code,
            "period": source.period,
            "form": source.form,
            "pre_post": student.pre_post,
            "source_pdf": source.source_pdf,
            "page_start": student.header_page_number,
        }
        readings = student_readings(student)
        for reading in readings:
            row[f"Q{reading.item_number}_{reading.dimension}"] = reading.marked
        multiple_count = sum(reading.is_multiple for reading in readings)
        uncertain_count = sum(reading.is_unsure for reading in readings)
        row["n_multiple"] = multiple_count
        row["n_uncertain"] = uncertain_count
        row["n_blank"] = sum(reading.is_blank for reading in readings)
        row["check"] = REVIEW_CHECK_WORD if multiple_count >= 1 or uncertain_count >= 1 else ""
        rows.append(row)
    return rows


def build_long_rows(students: list[Student], model: str) -> list[dict]:
    rows = []
    for student in students:
        for reading in student_readings(student):
            rows.append(
                {
                    "sid": sid_text(student),
                    "item": reading.item_number,
                    "dimension": reading.dimension,
                    "value": reading.value,
                    "value_marked": reading.marked,
                    "mark_confidence": reading.mark_confidence,
                    "value_num": numeric_value(reading.dimension, reading.value),
                    "mark_notes": reading.notes,
                    "page_number": reading.page_number,
                    "model": model,
                }
            )
    return rows


def build_flag_rows(students: list[Student], identity_flags: list[IdentityFlag]) -> list[dict]:
    rows = [
        {
            "sid": flag.sid or "",
            "page_number": flag.page_number if flag.page_number is not None else "",
            "item": "",
            "dimension": "",
            "flag_type": flag.flag_type,
            "value": "",
            "reason": flag.reason,
        }
        for flag in identity_flags
    ]
    for student in students:
        for page_number, item in student_items(student):
            for dimension, field_name in DIMENSIONS:
                value = getattr(item, field_name)
                kind = flag_kind(value)
                if kind is not None:
                    rows.append(
                        {
                            "sid": sid_text(student),
                            "page_number": page_number,
                            "item": item.item_number,
                            "dimension": dimension,
                            "flag_type": f"value_{kind}",
                            "value": value,
                            "reason": getattr(item, f"{field_name}_notes"),
                        }
                    )
    return rows


def flag_counts(flag_rows: list[dict]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in flag_rows:
        counts[row["flag_type"]] = counts.get(row["flag_type"], 0) + 1
    return dict(sorted(counts.items()))


def write_csv(path: Path, columns: list[str], rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns, extrasaction="raise")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        json.dump(data, handle, indent=2)


def write_raw_page(paths: OutputPaths, page_number: int, payload: dict) -> Path:
    path = paths.raw_page(page_number)
    write_json(path, payload)
    return path


def build_metrics_rows(metrics: list[PageMetric]) -> list[dict]:
    return [
        {
            "page_number": metric.page_number,
            "status": metric.status,
            "sid": metric.sid or "",
            "page_type": metric.page_type or "",
            "latency_seconds": "" if metric.latency_seconds is None else round(metric.latency_seconds, 3),
            "input_tokens": metric.input_tokens,
            "cached_input_tokens": metric.cached_input_tokens,
            "output_tokens": metric.output_tokens,
            "reasoning_tokens": metric.reasoning_tokens,
            "cost_usd": round(metric.cost_usd, 6),
            "cache_hit": metric.cache_hit,
            "retry_count": metric.retry_count,
            "model": metric.model,
            "error": metric.error,
        }
        for metric in metrics
    ]


def build_page_failure_flag_rows(failed_pages: dict[int, str]) -> list[dict]:
    return [
        {
            "sid": "",
            "page_number": page_number,
            "item": "",
            "dimension": "",
            "flag_type": "page_failed",
            "value": "",
            "reason": reason,
        }
        for page_number, reason in sorted(failed_pages.items())
    ]


def remove_outputs(paths: OutputPaths) -> list[Path]:
    removed = paths.existing_files()
    for path in removed:
        path.unlink()
    for directory in (paths.legacy_raw_directory, paths.raw_directory):
        if directory.is_dir() and not any(directory.iterdir()):
            directory.rmdir()
    return removed


def plural(count: int, singular: str, plural_form: str) -> str:
    return f"{count} {singular if count == 1 else plural_form}"


def review_student_label(student: Student, sid_counts: dict[str, int]) -> str:
    sid = sid_text(student).strip()
    if not sid:
        return f"Student with no SID, starting page {student.header_page_number}"
    if sid.upper() == REDACTED_SID or sid_counts.get(sid, 0) > 1:
        return f"Student {sid}, starting page {student.header_page_number}"
    return f"Student {sid}"


DOUBLE_ENTRIES_HEADING = "Double entries"
DOUBLE_ENTRIES_EXPLANATION = "The student marked more than one option in the same row. Open the scan and decide which answer to keep."
UNCERTAIN_HEADING = "Uncertain values"
UNCERTAIN_EXPLANATION = (
    "The reader was not sure how the row was marked, for example a faint mark or one circle touching two options. "
    "Open the scan and confirm the value. A double entry the reader was unsure of is listed here and above."
)
BLANKS_HEADING = "Blanks"
BLANKS_EXPLANATION = "Rows with no mark. These are usually questions the student skipped and need no action."
EMPTY_SECTION_TEXT = "None"


def readings_by_student(labelled: list[tuple[str, list[ValueReading]]], keep) -> list[tuple[str, list[ValueReading]]]:
    grouped = [(label, [reading for reading in readings if keep(reading)]) for label, readings in labelled]
    return [(label, readings) for label, readings in grouped if readings]


def section_heading(title: str, count: int) -> str:
    return f"{title} ({count})"


def grouped_section_lines(title: str, explanation: str, grouped: list[tuple[str, list[ValueReading]]]) -> list[str]:
    count = sum(len(readings) for _, readings in grouped)
    lines = ["", section_heading(title, count), explanation]
    if not grouped:
        return lines + ["", EMPTY_SECTION_TEXT]
    for label, readings in grouped:
        lines += ["", label]
        lines += [
            f"Item {reading.item_number}, {reading.row_name}: {reading.value} page {reading.page_number} {reading.notes}".rstrip()
            for reading in readings
        ]
    return lines


def blank_section_lines(blanks: list[tuple[str, ValueReading]]) -> list[str]:
    lines = ["", section_heading(BLANKS_HEADING, len(blanks)), BLANKS_EXPLANATION, ""]
    if not blanks:
        return lines + [EMPTY_SECTION_TEXT]
    return lines + [f"{label} Item {reading.item_number} {reading.row_name}, page {reading.page_number}" for label, reading in blanks]


def review_summary_line(multiple_count: int, uncertain_count: int, blank_count: int, students_to_check: int) -> str:
    counts = ", ".join(
        [
            plural(multiple_count, "double entry", "double entries"),
            plural(uncertain_count, "uncertain value", "uncertain values"),
            plural(blank_count, "blank", "blanks"),
        ]
    )
    return f"Summary: {counts}. {plural(students_to_check, 'student needs', 'students need')} checking."


def build_review_text(students: list[Student], source_pdf: str, teacher_code: str, period: str, pre_post: str | None) -> str:
    sid_counts: dict[str, int] = {}
    for student in students:
        sid = sid_text(student).strip()
        sid_counts[sid] = sid_counts.get(sid, 0) + 1
    labelled = [(review_student_label(student, sid_counts), student_readings(student)) for student in students]
    double_entries = readings_by_student(labelled, lambda reading: reading.is_multiple)
    uncertain = readings_by_student(labelled, lambda reading: reading.is_unsure)
    blanks = [(label, reading) for label, readings in labelled for reading in readings if reading.is_blank]
    students_to_check = {label for label, _ in double_entries} | {label for label, _ in uncertain}

    lines = [
        f"Review list for {source_pdf}",
        f"Teacher {teacher_code}, period {period}, {pre_post if pre_post else 'PRE/POST undetermined'}",
        "",
        review_summary_line(
            sum(len(readings) for _, readings in double_entries),
            sum(len(readings) for _, readings in uncertain),
            len(blanks),
            len(students_to_check),
        ),
    ]
    lines += grouped_section_lines(DOUBLE_ENTRIES_HEADING, DOUBLE_ENTRIES_EXPLANATION, double_entries)
    lines += grouped_section_lines(UNCERTAIN_HEADING, UNCERTAIN_EXPLANATION, uncertain)
    lines += blank_section_lines(blanks)
    return "\n".join(lines) + "\n"


def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
