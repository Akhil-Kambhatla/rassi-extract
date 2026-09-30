import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

from rassi.cache import ResponseCache
from rassi.config import DEFAULT_MODEL, DEFAULT_WORKERS
from rassi.crop import TableDetectionError, crop_item
from rassi.evaluation import (
    CROP_FILE,
    DIMENSION_FIELDS,
    LABEL_FILE,
    STATUS_NOT_RUN,
    CaseError,
    EvalReport,
    empty_label,
    eval_cache_key,
    load_library,
    previous_results_path,
    previous_statuses,
    results_timestamp,
    run_cases,
    status_changes,
    summarize,
    validate_case_id,
    write_report,
)
from rassi.extract import PageExtractor, make_client
from rassi.paths import ENV_FILE, DataLayout, display_path
from rassi.prompt import EVAL_USER_INSTRUCTION, PROMPT_VERSION
from rassi.spend import SpendTracker, UnknownModelPriceError, prices_for

EXIT_OK = 0
EXIT_USAGE_ERROR = 2
EXIT_CASES_FAILED = 1
DEFAULT_EVAL_MAX_SPEND_USD = 0.50
DIMENSION_SHORT_NAMES = {"agreement": "A", "reason": "B", "confidence": "C"}


def add_eval_parser(subcommands) -> None:
    evaluation = subcommands.add_parser("eval", help="Measure prompt accuracy against hand labeled item crops in eval/cases.")
    actions = evaluation.add_subparsers(dest="eval_command", required=True)
    add = actions.add_parser("add", help="Crop one item table from a page of a PDF in data/in and start a new case.")
    add.add_argument("--file", type=Path, required=True, help="A PDF inside data/in")
    add.add_argument("--page", type=int, required=True, help="Page number in the PDF, starting at 1")
    add.add_argument("--item", type=int, required=True, help="Item number, 1 to 12")
    add.add_argument("--case-id", required=True, help="Folder name for the case, for example span-into-neighbour")
    run = actions.add_parser("run", help="Send every labeled case to the model with the current prompt and score it.")
    run.add_argument("--model", default=DEFAULT_MODEL)
    run.add_argument("--max-spend", type=float, default=DEFAULT_EVAL_MAX_SPEND_USD, help="USD cap for this evaluation run")
    run.add_argument("--workers", type=int, default=DEFAULT_WORKERS)


def run_eval(arguments: argparse.Namespace, layout: DataLayout) -> int:
    if arguments.eval_command == "add":
        return run_eval_add(arguments, layout)
    return run_eval_run(arguments, layout)


def error(message: str) -> int:
    print(f"ERROR: {message}", file=sys.stderr)
    return EXIT_USAGE_ERROR


def run_eval_add(arguments: argparse.Namespace, layout: DataLayout) -> int:
    try:
        case_id = validate_case_id(arguments.case_id)
    except CaseError as problem:
        return error(str(problem))
    pdf_path: Path = arguments.file
    if not pdf_path.is_file():
        return error(f"{display_path(pdf_path)} does not exist")
    if not pdf_path.resolve().is_relative_to(layout.input_root.resolve()):
        return error(f"{display_path(pdf_path)} is not inside {display_path(layout.input_root)}. Cases may only be cut from approved PDFs in data/in")
    case_directory = layout.eval_cases_directory / case_id
    if case_directory.exists():
        return error(f"{display_path(case_directory)} already exists. Choose another --case-id or remove that folder by hand")
    try:
        crop = crop_item(pdf_path, arguments.page, arguments.item)
    except TableDetectionError as problem:
        return error(f"could not crop item {arguments.item} from page {arguments.page} of {display_path(pdf_path)}: {problem}")
    case_directory.mkdir(parents=True)
    (case_directory / CROP_FILE).write_bytes(crop.png_bytes)
    (case_directory / LABEL_FILE).write_text(json.dumps(empty_label(arguments.item), indent=2) + "\n", encoding="utf-8")
    box = crop.box
    print(f"Wrote {display_path(case_directory / CROP_FILE)} ({box.right - box.left}x{box.bottom - box.top} px)")
    for note in crop.notes:
        print(f"Note: {note}")
    print(f"Wrote {display_path(case_directory / LABEL_FILE)}")
    print(f"Next: open the crop, fill in agreement, reason and confidence in {LABEL_FILE}, and write notes.md explaining why the case is hard.")
    return EXIT_OK


def percent(fraction: float | None) -> str:
    return "n/a" if fraction is None else f"{fraction * 100:.1f}%"


def values_text(values: dict | None, matches: dict[str, bool] | None = None) -> str:
    if values is None:
        return "-"
    parts = [f"Q{values['item_number']}"]
    for name in DIMENSION_FIELDS:
        marker = "*" if matches is not None and not matches[name] else ""
        parts.append(f"{DIMENSION_SHORT_NAMES[name]}={values[name]}{marker}")
    return " ".join(parts)


def print_report(report: EvalReport) -> None:
    case_width = max([len("case")] + [len(run.case.case_id) for run in report.runs])
    rows = [("case", "expected", "actual", "result", "source")]
    for run in report.runs:
        score = run.score
        source = "cache" if run.from_cache else ("api" if run.response_id else "-")
        rows.append((run.case.case_id, values_text(score.expected), values_text(score.actual, score.matches), score.status.upper(), source))
    widths = [max(len(row[column]) for row in rows) for column in range(len(rows[0]))]
    widths[0] = case_width
    print()
    for row in rows:
        print("  ".join(cell.ljust(width) for cell, width in zip(row, widths)).rstrip())
    for run in report.runs:
        if run.score.error:
            print(f"  {run.case.case_id}: {run.score.error}")
    print()
    summary = report.summary
    print(f"Cases passed:     {summary.cases_passed.correct}/{summary.cases_passed.total} ({percent(summary.cases_passed.fraction)})")
    print(f"Overall accuracy: {summary.overall.correct}/{summary.overall.total} values ({percent(summary.overall.fraction)})")
    for name, accuracy in summary.per_dimension.items():
        print(f"  {DIMENSION_SHORT_NAMES[name]} {name:<11} {accuracy.correct}/{accuracy.total} ({percent(accuracy.fraction)})")
    if summary.not_run:
        print(f"Not run:          {summary.not_run} (--max-spend reached)")
    print(f"Total cost:       ${report.total_cost_usd:.4f}")
    live, recorded = report.live_latencies, report.all_latencies
    if live:
        print(f"Mean latency:     {sum(live) / len(live):.1f}s over {len(live)} API calls this run")
    elif recorded:
        print(f"Mean latency:     no API calls this run; {sum(recorded) / len(recorded):.1f}s recorded when the cached answers were made")
    else:
        print("Mean latency:     n/a")
    if report.previous_results is None:
        print("Changes:          no previous run to compare against")
    elif not report.changes:
        print(f"Changes:          none since {report.previous_results}")
    else:
        print(f"Changes since {report.previous_results}:")
        for change in report.changes:
            print(f"  {change.case_id}: {change.before} -> {change.after}")


def run_eval_run(arguments: argparse.Namespace, layout: DataLayout) -> int:
    try:
        prices_for(arguments.model)
    except UnknownModelPriceError as problem:
        return error(str(problem))
    try:
        library = load_library(layout.eval_cases_directory)
    except CaseError as problem:
        return error(str(problem))
    for case_id in library.unlabeled:
        print(f"SKIPPED {case_id}: {LABEL_FILE} has not been filled in")
    if not library.cases:
        return error(f"no labeled cases in {display_path(layout.eval_cases_directory)}")

    cache = ResponseCache(layout.eval_cache_directory)
    spend = SpendTracker(arguments.model, arguments.max_spend)
    uncached = [case for case in library.cases if not cache.contains(eval_cache_key(case.crop_png, arguments.model))]
    extractor = None
    if uncached:
        load_dotenv(ENV_FILE)
        if not os.environ.get("OPENAI_API_KEY"):
            return error("OPENAI_API_KEY is not set in .env or the environment")
        extractor = PageExtractor(make_client(), arguments.model, cache, spend, user_instruction=EVAL_USER_INSTRUCTION)
    print(
        f"Evaluating {len(library.cases)} cases with {arguments.model}, prompt {PROMPT_VERSION}: "
        f"{len(library.cases) - len(uncached)} cached, {len(uncached)} to send, max spend ${arguments.max_spend:.2f}"
    )

    previous_path = previous_results_path(layout.eval_results_directory)
    runs = run_cases(library.cases, extractor, cache, spend, arguments.model, arguments.workers)
    scores = [run.score for run in runs]
    report = EvalReport(
        timestamp=results_timestamp(datetime.now(timezone.utc)),
        model=arguments.model,
        runs=runs,
        summary=summarize(scores),
        changes=status_changes(previous_statuses(previous_path), scores),
        previous_results=previous_path.name if previous_path else None,
        unlabeled=library.unlabeled,
    )
    print_report(report)
    results_path = write_report(report, layout.eval_results_directory)
    print(f"Results written to {display_path(results_path)}")
    if any(score.status == STATUS_NOT_RUN for score in scores):
        return EXIT_CASES_FAILED
    return EXIT_OK if report.summary.cases_passed.correct == report.summary.cases_passed.total else EXIT_CASES_FAILED
