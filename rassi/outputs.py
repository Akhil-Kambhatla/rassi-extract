import csv
import json
from pathlib import Path

from rassi.config import ALL_ITEM_NUMBERS, MISSING_VALUE_CODE, NON_NUMERIC_VALUES, REASON_LETTER_TO_NUMBER
from rassi.filename import SourceFileInfo
from rassi.metrics import PageMetric
from rassi.paths import OutputPaths
from rassi.schema import ItemExtraction
from rassi.structure import IdentityFlag, Student

DIMENSIONS = (("A", "agreement"), ("B", "reason"), ("C", "confidence"))
WIDE_ID_COLUMNS = ["sid", "teacher_code", "period", "form", "pre_post", "source_pdf", "page_start"]
LONG_COLUMNS = ["sid", "item", "dimension", "value", "value_num", "mark_notes", "page_number", "model"]
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


def numeric_value(dimension: str, value: str):
    if value in NON_NUMERIC_VALUES:
        return MISSING_VALUE_CODE
    if dimension == "B":
        return REASON_LETTER_TO_NUMBER.get(value, MISSING_VALUE_CODE)
    return int(value) if value.isdigit() else MISSING_VALUE_CODE


def wide_columns() -> list[str]:
    columns = list(WIDE_ID_COLUMNS)
    for item_number in ALL_ITEM_NUMBERS:
        columns += [f"Q{item_number}_A", f"Q{item_number}_B", f"Q{item_number}_B_num", f"Q{item_number}_C"]
    return columns


def student_items(student: Student) -> list[tuple[int, ItemExtraction]]:
    located = [(student.header_page_number, item) for item in student.header.items]
    located += [(student.continuation_page_number, item) for item in student.continuation.items]
    return sorted(located, key=lambda pair: pair[1].item_number)


def sid_text(student: Student) -> str:
    return student.sid if student.sid is not None else ""


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
        for _, item in student_items(student):
            prefix = f"Q{item.item_number}"
            row[f"{prefix}_A"] = item.agreement
            row[f"{prefix}_B"] = item.reason
            row[f"{prefix}_B_num"] = numeric_value("B", item.reason)
            row[f"{prefix}_C"] = item.confidence
        rows.append(row)
    return rows


def build_long_rows(students: list[Student], model: str) -> list[dict]:
    rows = []
    for student in students:
        for page_number, item in student_items(student):
            for dimension, field_name in DIMENSIONS:
                value = getattr(item, field_name)
                rows.append(
                    {
                        "sid": sid_text(student),
                        "item": item.item_number,
                        "dimension": dimension,
                        "value": value,
                        "value_num": numeric_value(dimension, value),
                        "mark_notes": getattr(item, f"{field_name}_notes"),
                        "page_number": page_number,
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
                if value in NON_NUMERIC_VALUES:
                    rows.append(
                        {
                            "sid": sid_text(student),
                            "page_number": page_number,
                            "item": item.item_number,
                            "dimension": dimension,
                            "flag_type": f"value_{value}",
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
    return removed
