import argparse
import json
import os
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

from rassi.cache import ResponseCache
from rassi.config import DEFAULT_MODEL, DEFAULT_WORKERS
from rassi.crop import TableDetectionError, crop_item
from rassi.evaluation import (
    CASE_FILE,
    CASE_TYPE_CROP,
    CASE_TYPE_PAGE,
    CROP_FILE,
    DIMENSION_SHORT_NAMES,
    LABEL_FILE,
    PAGE_FILE,
    STATUS_PASS,
    CaseError,
    CaseResult,
    EvalReport,
    ValueKey,
    case_run_plan,
    case_statuses,
    empty_label,
    empty_page_label,
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
from rassi.prompt import PROMPT_VERSION
from rassi.render import count_pages, render_pages
from rassi.spend import SpendTracker, UnknownModelPriceError, prices_for

EXIT_OK = 0
EXIT_USAGE_ERROR = 2
EXIT_CASES_FAILED = 1
DEFAULT_EVAL_MAX_SPEND_USD = 0.50


def add_eval_parser(subcommands) -> None:
    evaluation = subcommands.add_parser("eval", help="Measure prompt accuracy against hand labeled item crops and full pages in eval/cases.")
    actions = evaluation.add_subparsers(dest="eval_command", required=True)
    add = actions.add_parser("add", help="Crop one item table from a page of a PDF in data/in and start a new crop case.")
    add.add_argument("--file", type=Path, required=True, help="A PDF inside data/in")
    add.add_argument("--page", type=int, required=True, help="Page number in the PDF, starting at 1")
    add.add_argument("--item", type=int, required=True, help="Item number, 1 to 12")
    add.add_argument("--case-id", required=True, help="Folder name for the case, for example span-into-neighbour")
    add_page = actions.add_parser("add-page", help="Render one full page of a PDF in data/in and start a new page case.")
    add_page.add_argument("--file", type=Path, required=True, help="A PDF inside data/in")
    add_page.add_argument("--page", type=int, required=True, help="Page number in the PDF, starting at 1")
    add_page.add_argument("--case-id", required=True, help="Folder name for the case, for example pd4-page6-vertical-bleed")
    run = actions.add_parser("run", help="Send every labeled case to the model with the current prompt and score it.")
    run.add_argument("--model", default=DEFAULT_MODEL)
    run.add_argument("--max-spend", type=float, default=DEFAULT_EVAL_MAX_SPEND_USD, help="USD cap for this evaluation run")
    run.add_argument("--workers", type=int, default=DEFAULT_WORKERS)
    run.add_argument("--repeat", type=int, default=1, help="Send each case this many times and report how many runs got each value right")


def run_eval(arguments: argparse.Namespace, layout: DataLayout) -> int:
    if arguments.eval_command == "add":
        return run_eval_add(arguments, layout)
    if arguments.eval_command == "add-page":
        return run_eval_add_page(arguments, layout)
    return run_eval_run(arguments, layout)


def error(message: str) -> int:
    print(f"ERROR: {message}", file=sys.stderr)
    return EXIT_USAGE_ERROR


def new_case_directory(arguments: argparse.Namespace, layout: DataLayout) -> tuple[Path | None, str | None]:
    try:
        case_id = validate_case_id(arguments.case_id)
    except CaseError as problem:
        return None, str(problem)
    pdf_path: Path = arguments.file
    if not pdf_path.is_file():
        return None, f"{display_path(pdf_path)} does not exist"
    if not pdf_path.resolve().is_relative_to(layout.input_root.resolve()):
        return None, f"{display_path(pdf_path)} is not inside {display_path(layout.input_root)}. Cases may only be cut from approved PDFs in data/in"
    case_directory = layout.eval_cases_directory / case_id
    if case_directory.exists():
        return None, f"{display_path(case_directory)} already exists. Choose another --case-id or remove that folder by hand"
    return case_directory, None


def write_json(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def case_info(case_type: str, pdf_path: Path, page_number: int) -> dict:
    return {"case_type": case_type, "source_pdf": pdf_path.name, "page_number": page_number}


def run_eval_add(arguments: argparse.Namespace, layout: DataLayout) -> int:
    case_directory, problem = new_case_directory(arguments, layout)
    if problem:
        return error(problem)
    pdf_path: Path = arguments.file
    try:
        crop = crop_item(pdf_path, arguments.page, arguments.item)
    except TableDetectionError as detection_problem:
        return error(f"could not crop item {arguments.item} from page {arguments.page} of {display_path(pdf_path)}: {detection_problem}")
    case_directory.mkdir(parents=True)
    (case_directory / CROP_FILE).write_bytes(crop.png_bytes)
    write_json(case_directory / LABEL_FILE, empty_label(arguments.item))
    write_json(case_directory / CASE_FILE, case_info(CASE_TYPE_CROP, pdf_path, arguments.page))
    box = crop.box
    print(f"Wrote {display_path(case_directory / CROP_FILE)} ({box.right - box.left}x{box.bottom - box.top} px)")
    for note in crop.notes:
        print(f"Note: {note}")
    print(f"Wrote {display_path(case_directory / LABEL_FILE)} and {CASE_FILE}")
    print(f"Next: open the crop, fill in agreement, reason and confidence in {LABEL_FILE}, and write notes.md explaining why the case is hard.")
    return EXIT_OK


def run_eval_add_page(arguments: argparse.Namespace, layout: DataLayout) -> int:
    case_directory, problem = new_case_directory(arguments, layout)
    if problem:
        return error(problem)
    pdf_path: Path = arguments.file
    total_pages = count_pages(pdf_path)
    if not 1 <= arguments.page <= total_pages:
        return error(f"page {arguments.page} is outside {display_path(pdf_path)}, which has {total_pages} pages")
    page = render_pages(pdf_path, [arguments.page])[0]
    case_directory.mkdir(parents=True)
    (case_directory / PAGE_FILE).write_bytes(page.png_bytes)
    write_json(case_directory / LABEL_FILE, empty_page_label(arguments.page))
    write_json(case_directory / CASE_FILE, case_info(CASE_TYPE_PAGE, pdf_path, arguments.page))
    print(f"Wrote {display_path(case_directory / PAGE_FILE)} ({page.width_px}x{page.height_px} px)")
    print(f"Wrote {display_path(case_directory / LABEL_FILE)} and {CASE_FILE}")
    print(
        f"Next: open the page, fill in page_type, sid (null when there is no stamp) and every item's agreement, reason and confidence "
        f"in {LABEL_FILE}, and write notes.md explaining why the case is hard."
    )
    return EXIT_OK


def percent(fraction: float | None) -> str:
    return "n/a" if fraction is None else f"{fraction * 100:.1f}%"


def shown(value: str | None) -> str:
    return "null" if value is None else value


def value_cell(result: CaseResult, key: ValueKey) -> str:
    name = key.field if key.is_page_field else DIMENSION_SHORT_NAMES[key.field]
    correct, scored = result.correct_runs(key), len(result.scored_runs)
    text = f"{name}={shown(result.case.expected[key])} {correct}/{scored}"
    if correct < scored:
        text += "*"
    if result.is_unstable(key):
        text += " UNSTABLE"
    wrong = Counter(shown(answer) for answer in result.answers(key) if answer != result.case.expected[key])
    if wrong:
        text += " (got " + ", ".join(f"{answer} x{count}" for answer, count in wrong.most_common()) + ")"
    return text


def print_case(result: CaseResult) -> None:
    sources = Counter("cache" if run.from_cache else ("api" if run.response_id else "none") for run in result.runs)
    source_text = ", ".join(f"{source} {count}" for source, count in sorted(sources.items()))
    scored = len(result.scored_runs)
    breakdown = ""
    if result.mark_confidence_status is not None:
        breakdown = f"  values {result.values_status.upper()}, mark confidence {result.mark_confidence_status.upper()}"
    print(
        f"{result.case.case_id}  {result.case.case_type}  {result.status.upper()}  {result.runs_passed}/{scored} runs passed"
        f"{breakdown}  ({source_text})"
    )
    page_keys = [key for key in result.keys if key.is_page_field]
    if page_keys:
        print("  page  " + "  ".join(value_cell(result, key) for key in page_keys))
    rows = []
    for item_number in result.case.item_numbers:
        rows.append([f"Q{item_number}"] + [value_cell(result, key) for key in result.keys if key.item_number == item_number])
    column_count = max(len(row) for row in rows)
    rows = [row + [""] * (column_count - len(row)) for row in rows]
    widths = [max(len(row[column]) for row in rows) for column in range(column_count)]
    for row in rows:
        print("  " + "  ".join(cell.ljust(width) for cell, width in zip(row, widths)).rstrip())
    for run in result.runs:
        if run.score.error:
            print(f"  run {run.repeat_index + 1}: {run.score.error}")


def print_report(report: EvalReport) -> None:
    print()
    for result in report.results:
        print_case(result)
    print()
    summary = report.summary
    print(f"Runs per case:    {summary.repeat}")
    print(f"Cases passed:     {summary.cases_passed.correct}/{summary.cases_passed.total} ({percent(summary.cases_passed.fraction)}), every run correct")
    print(f"Runs passed:      {summary.runs_passed.correct}/{summary.runs_passed.total} ({percent(summary.runs_passed.fraction)})")
    print(f"Overall accuracy: {summary.overall.correct}/{summary.overall.total} values ({percent(summary.overall.fraction)})")
    for name, accuracy in summary.per_dimension.items():
        print(f"  {DIMENSION_SHORT_NAMES[name]} {name:<11} {accuracy.correct}/{accuracy.total} ({percent(accuracy.fraction)})")
    if summary.mark_confidence.total:
        print(
            f"Mark confidence:  {summary.mark_confidence.correct}/{summary.mark_confidence.total} "
            f"({percent(summary.mark_confidence.fraction)}), clear or unsure, scored apart from the values"
        )
    if summary.page_fields.total:
        print(f"Page fields:      {summary.page_fields.correct}/{summary.page_fields.total} ({percent(summary.page_fields.fraction)}), page_type and sid")
    print(f"Unstable values:  {summary.unstable_values}")
    if summary.not_run:
        print(f"Not run:          {summary.not_run} runs (--max-spend reached)")
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
    if arguments.repeat < 1:
        return error("--repeat must be 1 or more")
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
    plan = case_run_plan(library.cases, arguments.repeat)
    uncached = [
        (case, repeat_index)
        for case, repeat_index in plan
        if not cache.contains(eval_cache_key(case.image_png, arguments.model, repeat_index=repeat_index))
    ]
    extractor = None
    if uncached:
        load_dotenv(ENV_FILE)
        if not os.environ.get("OPENAI_API_KEY"):
            return error("OPENAI_API_KEY is not set in .env or the environment")
        extractor = PageExtractor(make_client(), arguments.model, cache, spend)
    print(
        f"Evaluating {len(library.cases)} cases x {arguments.repeat} runs with {arguments.model}, prompt {PROMPT_VERSION}: "
        f"{len(plan) - len(uncached)} cached, {len(uncached)} to send, max spend ${arguments.max_spend:.2f}"
    )

    previous_path = previous_results_path(layout.eval_results_directory)
    results = run_cases(library.cases, extractor, cache, spend, arguments.model, arguments.workers, arguments.repeat)
    report = EvalReport(
        timestamp=results_timestamp(datetime.now(timezone.utc)),
        model=arguments.model,
        results=results,
        summary=summarize(results, arguments.repeat),
        changes=status_changes(previous_statuses(previous_path), case_statuses(results)),
        previous_results=previous_path.name if previous_path else None,
        unlabeled=library.unlabeled,
    )
    print_report(report)
    results_path = write_report(report, layout.eval_results_directory)
    print(f"Results written to {display_path(results_path)}")
    if report.summary.not_run:
        return EXIT_CASES_FAILED
    return EXIT_OK if all(result.status == STATUS_PASS for result in results) else EXIT_CASES_FAILED
