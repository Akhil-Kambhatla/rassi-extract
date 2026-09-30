import argparse
import json
import os
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

from rassi.cache import ResponseCache, cache_key
from rassi.config import (
    CACHED_INPUT_TOKENS_SOURCE,
    DEFAULT_MAX_SPEND_USD,
    DEFAULT_MODEL,
    DEFAULT_WORKERS,
    LONG_CONTEXT_SURCHARGE_NOTE,
    RENDER_DPI,
)
from rassi.discover import (
    Discovery,
    InputError,
    InputFile,
    discover_all,
    discover_file,
    discover_teacher,
    output_collisions,
)
from rassi.eval_cli import add_eval_parser, run_eval
from rassi.extract import ExtractionRun, PageExtractor, make_client
from rassi.filename import SourceFileInfo
from rassi.metrics import FilePages, PageMetric, TimingAndCost, page_metrics, timing_and_cost, timing_and_cost_lines
from rassi.outputs import (
    FLAG_COLUMNS,
    LONG_COLUMNS,
    METRICS_COLUMNS,
    build_flag_rows,
    build_long_rows,
    build_metrics_rows,
    build_page_failure_flag_rows,
    build_wide_rows,
    flag_counts,
    remove_outputs,
    wide_columns,
    write_csv,
    write_json,
    write_raw_page,
)
from rassi.paths import DEFAULT_LAYOUT, ENV_FILE, DataLayout, OutputPaths, display_path, output_paths
from rassi.prompt import PROMPT_VERSION
from rassi.render import RenderedPage, count_pages, parse_page_selection, render_pages
from rassi.schema import SCHEMA_VERSION
from rassi.spend import (
    SpendSnapshot,
    SpendTracker,
    TokenUsage,
    cost_usd,
    default_estimated_cost_per_page,
    prices_for,
)
from rassi.structure import check_structure, file_pre_post, identity_flags, pair_students

EXIT_OK = 0
EXIT_USAGE_ERROR = 2
EXIT_INCOMPLETE = 3
EXIT_STRUCTURE_VIOLATION = 4

STATUS_FAILED_FILE = "failed_file"

STATUS_EXIT_CODES = {
    "complete": EXIT_OK,
    "dry_run": EXIT_OK,
    "skipped_existing": EXIT_OK,
    "skipped_error": EXIT_USAGE_ERROR,
    "incomplete": EXIT_INCOMPLETE,
    "not_started": EXIT_INCOMPLETE,
    STATUS_FAILED_FILE: EXIT_INCOMPLETE,
    "structure_violation": EXIT_STRUCTURE_VIOLATION,
}


@dataclass
class FileOutcome:
    input_file: InputFile
    status: str
    students: int = 0
    pages: int = 0
    cache_hits: int = 0
    spend: SpendSnapshot = field(default_factory=SpendSnapshot)
    estimated_calls: int = 0
    estimated_cost_usd: float = 0.0
    error: str = ""
    file_pages: FilePages | None = None

    @property
    def exit_code(self) -> int:
        return STATUS_EXIT_CODES[self.status]


@dataclass
class RunContext:
    model: str
    max_spend: float
    pages: str | None
    workers: int
    overwrite: bool
    dry_run: bool
    layout: DataLayout
    cache: ResponseCache
    per_call_estimate: float
    per_call_basis: str
    spend: SpendTracker | None = None
    extractor: PageExtractor | None = None


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m rassi", description="Extract RASSI answer sheet responses to CSV.")
    subcommands = parser.add_subparsers(dest="command", required=True)
    extract = subcommands.add_parser(
        "extract",
        help="Extract redacted RASSI PDFs. With no target, every teacher folder in data/in is processed.",
    )
    target = extract.add_mutually_exclusive_group()
    target.add_argument("--teacher", default=None, help="Process one teacher folder, data/in/<teacher_code>")
    target.add_argument("--file", type=Path, default=None, help="Process one PDF, data/in/<teacher_code>/<teacher_code>_PD<period>_RASSI.pdf")
    extract.add_argument("--model", default=DEFAULT_MODEL)
    extract.add_argument("--max-spend", type=float, default=DEFAULT_MAX_SPEND_USD, help="USD cap for the whole invocation, across all files")
    extract.add_argument("--pages", default=None, help="Subset of pages in each file, for example 1-4 or 1-2,5-6")
    extract.add_argument("--dry-run", action="store_true", help="Render and estimate cost without calling the API")
    extract.add_argument("--workers", type=int, default=DEFAULT_WORKERS)
    extract.add_argument("--overwrite", action="store_true", help="Replace outputs that already exist in data/out")
    add_eval_parser(subcommands)
    return parser


def price_text(model: str) -> str:
    prices = prices_for(model)
    return (
        f"${prices['input']:.2f} in / ${prices['cached_input']:.2f} cached in / "
        f"${prices['output']:.2f} out per 1M tokens"
    )


def pricing_record(model: str) -> dict:
    return {
        "prices_per_million_tokens": prices_for(model),
        "cached_input_tokens_source": CACHED_INPUT_TOKENS_SOURCE,
        "long_context_surcharge": LONG_CONTEXT_SURCHARGE_NOTE,
    }


def token_lines(usage: TokenUsage, model: str, indent: str) -> list[str]:
    cached_rate = prices_for(model)["cached_input"]
    return [
        f"{indent}tokens:            in {usage.input_tokens:,} / out {usage.output_tokens:,} (reasoning {usage.reasoning_tokens:,})",
        f"{indent}cached input:      {usage.cached_input_tokens:,} of the input tokens, billed at ${cached_rate:.2f} per 1M",
    ]


def estimated_cost_per_call(model: str, cache: ResponseCache) -> tuple[float, str]:
    observed = cache.observed_usage_for_model(model)
    if observed:
        costs = [cost_usd(model, TokenUsage.from_dict(usage)) for usage in observed]
        return sum(costs) / len(costs), f"average of {len(costs)} cached {model} calls"
    return default_estimated_cost_per_page(model), "default token estimate in config.py"


def run_dry_run(
    input_file: InputFile,
    total_pages: int,
    rendered: list[RenderedPage],
    context: RunContext,
    existing_outputs: list[Path],
) -> FileOutcome:
    source = input_file.source
    model = context.model
    uncached_pages = [
        page
        for page in rendered
        if not context.cache.contains(cache_key(page.png_bytes, model, PROMPT_VERSION, SCHEMA_VERSION))
    ]
    estimate = context.per_call_estimate * len(uncached_pages)
    sizes_mb = [len(page.png_bytes) / 1_000_000 for page in rendered]
    first = rendered[0]
    print("DRY RUN, no API calls made")
    print(f"  file:            {display_path(input_file.path)}")
    print(f"  teacher code:    {source.teacher_code}")
    print(f"  period:          {source.period}")
    print(f"  form:            {source.form}")
    print(f"  pages in PDF:    {total_pages}")
    print(f"  pages selected:  {len(rendered)} ({rendered[0].page_number}-{rendered[-1].page_number})")
    print(f"  rendered at:     {RENDER_DPI} DPI, {first.width_px}x{first.height_px}px, PNG {min(sizes_mb):.2f}-{max(sizes_mb):.2f} MB per page")
    print(f"  student count:   {len(rendered) / 2:g} (at 2 pages per student)")
    if len(rendered) % 2 != 0:
        print("  WARNING:         odd page count, the structure check will fail")
    print(f"  model:           {model} ({price_text(model)})")
    print(f"  cached pages:    {len(rendered) - len(uncached_pages)}")
    print(f"  API calls:       {len(uncached_pages)}")
    print(f"  est. per call:   ${context.per_call_estimate:.4f} ({context.per_call_basis})")
    print(f"  est. total cost: ${estimate:.2f}")
    print(f"  max spend:       ${context.max_spend:.2f} for the whole invocation")
    if estimate > context.max_spend:
        print("  WARNING:         estimate exceeds --max-spend; the run would stop before finishing")
    if existing_outputs and not context.overwrite:
        print(f"  NOTE:            {len(existing_outputs)} outputs already exist; a real run skips this file without --overwrite")
    return FileOutcome(
        input_file=input_file,
        status="dry_run",
        pages=len(rendered),
        cache_hits=len(rendered) - len(uncached_pages),
        estimated_calls=len(uncached_pages),
        estimated_cost_usd=estimate,
    )


def raw_payload(result, source: SourceFileInfo) -> dict:
    return {
        "source_pdf": source.source_pdf,
        "page_number": result.page_number,
        "model": result.model,
        "prompt_version": PROMPT_VERSION,
        "schema_version": SCHEMA_VERSION,
        "reasoning_effort": result.reasoning_effort,
        "response_id": result.response_id,
        "from_cache": result.from_cache,
        "usage": result.usage.as_dict(),
        "cost_usd": result.cost_usd,
        "cache_key": result.cache_key,
        "extraction": result.extraction.model_dump(),
    }


def build_manifest(
    source: SourceFileInfo,
    model: str,
    max_spend: float,
    selected_pages: list[int],
    run: ExtractionRun,
    spend: SpendSnapshot,
    status: str,
    student_count: int,
    counts: dict[str, int],
    violations: list[str],
    pre_post: str | None,
    stats: TimingAndCost,
) -> dict:
    efforts = sorted({result.reasoning_effort for result in run.results if result.reasoning_effort})
    return {
        "status": status,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "source_pdf": source.source_pdf,
        "teacher_code": source.teacher_code,
        "period": source.period,
        "form": source.form,
        "pre_post": pre_post,
        "model": model,
        "prompt_version": PROMPT_VERSION,
        "schema_version": SCHEMA_VERSION,
        "reasoning_effort": efforts,
        "render_dpi": RENDER_DPI,
        "pages_selected": selected_pages,
        "pages_processed": len(run.results),
        "pages_failed": run.failed_pages,
        "pages_skipped_for_spend": run.skipped_for_spend,
        "students": student_count,
        "cache_hits": run.cache_hits,
        "calls_made": spend.calls_made,
        "input_tokens": spend.usage.input_tokens,
        "cached_input_tokens": spend.usage.cached_input_tokens,
        "output_tokens": spend.usage.output_tokens,
        "reasoning_tokens": spend.usage.reasoning_tokens,
        "total_cost_usd": round(spend.spent_usd, 6),
        "pricing": pricing_record(model),
        "max_spend_usd": max_spend,
        "max_spend_scope": "invocation",
        "workers": stats.workers,
        "timing_and_cost": stats.as_dict(),
        "flag_counts": counts,
        "structure_violations": violations,
    }


def write_manifest_and_metrics(paths: OutputPaths, manifest: dict, metrics: list[PageMetric]) -> list[Path]:
    write_json(paths.manifest_json, manifest)
    write_csv(paths.metrics_csv, METRICS_COLUMNS, build_metrics_rows(metrics))
    return [paths.metrics_csv, paths.manifest_json]


def print_summary(
    status: str,
    student_count: int,
    run: ExtractionRun,
    spend: SpendSnapshot,
    counts: dict[str, int],
    written: list[Path],
    stats: TimingAndCost,
    model: str,
) -> None:
    print()
    print("SUMMARY")
    print(f"  status:            {status}")
    print(f"  students:          {student_count}")
    print(f"  pages extracted:   {len(run.results)}")
    print(f"  cache hits:        {run.cache_hits}")
    print(f"  API calls:         {spend.calls_made}")
    for line in token_lines(spend.usage, model, "  "):
        print(line)
    print(f"  total cost:        ${spend.spent_usd:.4f}")
    if run.failed_pages:
        print(f"  failed pages:      {sorted(run.failed_pages)}")
    print("  timing and cost:")
    for line in timing_and_cost_lines(stats):
        print(line)
    if counts:
        print("  flags:")
        for flag_type, count in counts.items():
            print(f"    {flag_type:<28} {count}")
    else:
        print("  flags:             none")
    print("  outputs:")
    for path in written:
        print(f"    {display_path(path)}")


def write_results(
    input_file: InputFile,
    context: RunContext,
    paths: OutputPaths,
    selected_pages: list[int],
    run: ExtractionRun,
    spend: SpendSnapshot,
    wall_clock_seconds: float,
) -> FileOutcome:
    source = input_file.source
    removed = remove_outputs(paths)
    if removed:
        print(f"removed {len(removed)} earlier outputs for {paths.stem}")

    pages = [(result.page_number, result.extraction) for result in run.results]
    majority_pre_post = file_pre_post(pages)
    metrics = page_metrics(run, context.model, selected_pages)
    file_pages = FilePages(metrics, selected_pages)
    stats = timing_and_cost(
        [file_pages], wall_clock_seconds, context.workers, (context.per_call_estimate, context.per_call_basis)
    )
    written: list[Path] = []
    for result in run.results:
        write_raw_page(paths, result.page_number, raw_payload(result, source))
    if run.results:
        written.append(paths.raw_directory)

    def outcome(status: str, student_count: int) -> FileOutcome:
        return FileOutcome(
            input_file=input_file,
            status=status,
            students=student_count,
            pages=len(run.results),
            cache_hits=run.cache_hits,
            spend=spend,
            file_pages=file_pages,
        )

    if run.failed_pages or run.skipped_for_spend:
        status = "incomplete"
        print()
        if run.skipped_for_spend:
            print(
                f"STOPPED: --max-spend ${context.max_spend:.2f} for this invocation would be exceeded. "
                f"{len(run.skipped_for_spend)} pages not started: {run.skipped_for_spend}"
            )
        for page_number, reason in sorted(run.failed_pages.items()):
            print(f"FAILED: page {page_number}: {reason}")
        print("Student CSVs were not written. Completed pages are cached, so re-running with --overwrite costs nothing for them.")
        failure_flag_rows = build_page_failure_flag_rows(run.failed_pages)
        counts = flag_counts(failure_flag_rows)
        if failure_flag_rows:
            write_csv(paths.flags_csv, FLAG_COLUMNS, failure_flag_rows)
            written.append(paths.flags_csv)
        manifest = build_manifest(
            source, context.model, context.max_spend, selected_pages, run, spend, status, 0, counts, [], majority_pre_post, stats
        )
        written += write_manifest_and_metrics(paths, manifest, metrics)
        print_summary(status, 0, run, spend, counts, written, stats, context.model)
        return outcome(status, 0)

    violations = check_structure(pages)
    if violations:
        status = "structure_violation"
        print()
        print("STRUCTURE CHECK FAILED. Student CSVs were not written.")
        for violation in violations:
            print(f"  {violation.describe()}")
        descriptions = [violation.describe() for violation in violations]
        manifest = build_manifest(
            source, context.model, context.max_spend, selected_pages, run, spend, status, 0, {}, descriptions, majority_pre_post, stats
        )
        written += write_manifest_and_metrics(paths, manifest, metrics)
        print_summary(status, 0, run, spend, {}, written, stats, context.model)
        return outcome(status, 0)

    students = pair_students(pages)
    flags = identity_flags(students, source.period, majority_pre_post)
    wide_rows = build_wide_rows(students, source)
    long_rows = build_long_rows(students, context.model)
    flag_rows = build_flag_rows(students, flags)
    counts = flag_counts(flag_rows)

    write_csv(paths.wide_csv, wide_columns(), wide_rows)
    write_csv(paths.long_csv, LONG_COLUMNS, long_rows)
    write_csv(paths.flags_csv, FLAG_COLUMNS, flag_rows)
    status = "complete"
    manifest = build_manifest(
        source, context.model, context.max_spend, selected_pages, run, spend, status, len(students), counts, [], majority_pre_post, stats
    )
    write_manifest_and_metrics(paths, manifest, metrics)
    written = [paths.wide_csv, paths.long_csv, paths.flags_csv, paths.metrics_csv, paths.manifest_json, paths.raw_directory]
    print_summary(status, len(students), run, spend, counts, written, stats, context.model)
    return outcome(status, len(students))


def process_file(input_file: InputFile, context: RunContext) -> FileOutcome:
    started = time.perf_counter()
    pdf_path = input_file.path
    paths = output_paths(input_file.source, context.layout)
    existing_outputs = blocking_outputs(paths)
    if existing_outputs and not context.overwrite and not context.dry_run:
        print(f"SKIPPED: {display_path(pdf_path)}: outputs already exist; pass --overwrite to replace them:")
        for path in existing_outputs:
            print(f"    {display_path(path)}")
        return FileOutcome(input_file=input_file, status="skipped_existing")

    try:
        total_pages = count_pages(pdf_path)
    except Exception as error:
        return unreadable_pdf(input_file, error, paths, context)
    try:
        selected_pages = parse_page_selection(context.pages, total_pages)
    except ValueError as error:
        print(f"ERROR: {display_path(pdf_path)}: {error}", file=sys.stderr)
        return FileOutcome(input_file=input_file, status="skipped_error")

    print(f"Rendering {len(selected_pages)} of {total_pages} pages of {display_path(pdf_path)} at {RENDER_DPI} DPI...")
    try:
        rendered = render_pages(pdf_path, selected_pages)
    except Exception as error:
        return unreadable_pdf(input_file, error, paths, context)

    if context.dry_run:
        return run_dry_run(input_file, total_pages, rendered, context, existing_outputs)

    before = context.spend.snapshot()
    run = context.extractor.extract_pages(rendered, total_pages, context.workers)
    file_spend = context.spend.snapshot().since(before)
    wall_clock_seconds = time.perf_counter() - started
    return write_results(input_file, context, paths, selected_pages, run, file_spend, wall_clock_seconds)


def manifest_status(manifest_path: Path) -> str | None:
    try:
        return json.loads(manifest_path.read_text(encoding="utf-8")).get("status")
    except (OSError, ValueError, AttributeError):
        return None


def blocking_outputs(paths: OutputPaths) -> list[Path]:
    existing = paths.existing_files()
    if existing == [paths.manifest_json] and manifest_status(paths.manifest_json) == STATUS_FAILED_FILE:
        return []
    return existing


def failed_file_manifest(input_file: InputFile, reason: str) -> dict:
    return {
        "status": STATUS_FAILED_FILE,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "path": display_path(input_file.path),
        "source_pdf": input_file.source.source_pdf,
        "teacher_code": input_file.source.teacher_code,
        "period": input_file.source.period,
        "form": input_file.source.form,
        "error": reason,
    }


def unreadable_pdf(input_file: InputFile, error: Exception, paths: OutputPaths, context: RunContext) -> FileOutcome:
    reason = f"{type(error).__name__}: {error}"
    print(f"FAILED: {display_path(input_file.path)}: could not read the PDF: {reason}", file=sys.stderr)
    if not context.dry_run:
        remove_outputs(paths)
        write_json(paths.manifest_json, failed_file_manifest(input_file, reason))
        print(f"  failure recorded in {display_path(paths.manifest_json)}", file=sys.stderr)
    return FileOutcome(input_file=input_file, status=STATUS_FAILED_FILE, error=reason)


def discover_inputs(arguments: argparse.Namespace, layout: DataLayout) -> Discovery:
    if arguments.file is not None:
        return discover_file(layout, arguments.file)
    if arguments.teacher is not None:
        return discover_teacher(layout, arguments.teacher)
    return discover_all(layout)


def print_run_summary(outcomes: list[FileOutcome], problem_count: int, context: RunContext, wall_clock_seconds: float) -> None:
    cost_label = "est. cost" if context.dry_run else "cost"
    calls_label = "est. calls" if context.dry_run else "calls"
    name_width = max(len(display_path(outcome.input_file.path)) for outcome in outcomes)
    name_width = max(name_width, len("TOTAL"))
    print()
    print(f"RUN SUMMARY ({len(outcomes)} files)")
    print(f"  {'file':<{name_width}}  {'status':<20} {'students':>8} {'pages':>6} {'cached':>6} {calls_label:>10} {cost_label:>10}")
    for outcome in outcomes:
        calls = outcome.estimated_calls if context.dry_run else outcome.spend.calls_made
        cost = outcome.estimated_cost_usd if context.dry_run else outcome.spend.spent_usd
        print(
            f"  {display_path(outcome.input_file.path):<{name_width}}  {outcome.status:<20} {outcome.students:>8} "
            f"{outcome.pages:>6} {outcome.cache_hits:>6} {calls:>10} {'$' + format(cost, '.4f'):>10}"
        )
    total_students = sum(outcome.students for outcome in outcomes)
    total_pages = sum(outcome.pages for outcome in outcomes)
    total_cached = sum(outcome.cache_hits for outcome in outcomes)
    if context.dry_run:
        total_calls = sum(outcome.estimated_calls for outcome in outcomes)
        total_cost = sum(outcome.estimated_cost_usd for outcome in outcomes)
    else:
        total_calls = context.spend.calls_made
        total_cost = context.spend.spent_usd
    print(
        f"  {'TOTAL':<{name_width}}  {'':<20} {total_students:>8} {total_pages:>6} {total_cached:>6} "
        f"{total_calls:>10} {'$' + format(total_cost, '.4f'):>10}"
    )
    if not context.dry_run:
        for line in token_lines(context.spend.usage, context.model, "  "):
            print(line)
    print(f"  max spend:   ${context.max_spend:.2f} for the whole invocation")
    failed_files = [outcome for outcome in outcomes if outcome.status == STATUS_FAILED_FILE]
    for outcome in failed_files:
        print(f"  FAILED FILE: {display_path(outcome.input_file.path)}: {outcome.error}")
    if not context.dry_run:
        combined = timing_and_cost(
            [outcome.file_pages for outcome in outcomes if outcome.file_pages is not None],
            wall_clock_seconds,
            context.workers,
            (context.per_call_estimate, context.per_call_basis),
        )
        print("  combined timing and cost:")
        for line in timing_and_cost_lines(combined):
            print(line)
    if context.dry_run and total_cost > context.max_spend:
        print("  WARNING:     combined estimate exceeds --max-spend; the run would stop before finishing")
    status_counts: dict[str, int] = {}
    for outcome in outcomes:
        status_counts[outcome.status] = status_counts.get(outcome.status, 0) + 1
    print("  statuses:    " + ", ".join(f"{status} {count}" for status, count in sorted(status_counts.items())))
    if problem_count:
        print(f"  input problems: {problem_count}, listed at the start of the run")


def run_extract(arguments: argparse.Namespace, layout: DataLayout) -> int:
    try:
        prices_for(arguments.model)
    except ValueError as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return EXIT_USAGE_ERROR
    if arguments.max_spend <= 0:
        print("ERROR: --max-spend must be greater than 0", file=sys.stderr)
        return EXIT_USAGE_ERROR
    if arguments.workers < 1:
        print("ERROR: --workers must be at least 1", file=sys.stderr)
        return EXIT_USAGE_ERROR

    try:
        discovery = discover_inputs(arguments, layout)
    except InputError as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return EXIT_USAGE_ERROR

    for problem in discovery.problems:
        print(f"SKIPPED: {problem.describe()}", file=sys.stderr)
    for warning in discovery.warnings:
        print(f"WARNING: {warning.describe()}", file=sys.stderr)

    collisions = output_collisions(discovery.files, layout)
    if collisions:
        for collision in collisions:
            print(f"ERROR: {collision}", file=sys.stderr)
        print("ERROR: rename or remove one of each colliding pair; nothing was processed", file=sys.stderr)
        return EXIT_USAGE_ERROR

    if not discovery.files:
        print("ERROR: no valid RASSI PDFs to process", file=sys.stderr)
        return EXIT_USAGE_ERROR

    if arguments.pages is not None and arguments.file is None and len(discovery.files) != 1:
        print(
            f"ERROR: --pages selects pages within one PDF, but this run matched {len(discovery.files)} PDFs. "
            "Use --pages with --file, or with a --teacher folder that holds exactly one PDF.",
            file=sys.stderr,
        )
        return EXIT_USAGE_ERROR

    cache = ResponseCache(layout.cache_root)
    per_call, basis = estimated_cost_per_call(arguments.model, cache)
    context = RunContext(
        model=arguments.model,
        max_spend=arguments.max_spend,
        pages=arguments.pages,
        workers=arguments.workers,
        overwrite=arguments.overwrite,
        dry_run=arguments.dry_run,
        layout=layout,
        cache=cache,
        per_call_estimate=per_call,
        per_call_basis=basis,
    )

    if not arguments.dry_run:
        load_dotenv(ENV_FILE)
        if not os.environ.get("OPENAI_API_KEY"):
            print("ERROR: OPENAI_API_KEY is not set in .env or the environment", file=sys.stderr)
            return EXIT_USAGE_ERROR
        context.spend = SpendTracker(arguments.model, arguments.max_spend, fallback_estimate_usd=per_call)
        context.extractor = PageExtractor(make_client(), arguments.model, cache, context.spend)
        print(
            f"Extracting {len(discovery.files)} files with {arguments.model}, {arguments.workers} workers, "
            f"max spend ${arguments.max_spend:.2f} for the whole invocation"
        )

    multiple_files = len(discovery.files) > 1
    run_started = time.perf_counter()
    outcomes: list[FileOutcome] = []
    for position, input_file in enumerate(discovery.files, start=1):
        if multiple_files:
            print()
            print(f"=== [{position}/{len(discovery.files)}] {display_path(input_file.path)} ===")
        if context.spend is not None and context.spend.limit_reached:
            print(f"NOT STARTED: --max-spend ${context.max_spend:.2f} for this invocation was reached by an earlier file")
            outcomes.append(FileOutcome(input_file=input_file, status="not_started"))
            continue
        outcomes.append(process_file(input_file, context))

    if multiple_files:
        print_run_summary(outcomes, len(discovery.problems), context, time.perf_counter() - run_started)

    exit_codes = [outcome.exit_code for outcome in outcomes]
    if discovery.problems:
        exit_codes.append(EXIT_USAGE_ERROR)
    return max(exit_codes)


def main(argv: list[str] | None = None, layout: DataLayout = DEFAULT_LAYOUT) -> int:
    arguments = build_parser().parse_args(argv)
    if arguments.command == "extract":
        return run_extract(arguments, layout)
    if arguments.command == "eval":
        return run_eval(arguments, layout)
    return EXIT_USAGE_ERROR
