import statistics
from dataclasses import dataclass

from rassi.config import PROJECTION_PAGES_PER_PDF, PROJECTION_PDF_COUNT
from rassi.extract import ExtractionRun

PAGE_STATUS_OK = "ok"
PAGE_STATUS_FAILED = "failed"
PAGE_STATUS_SKIPPED_FOR_SPEND = "skipped_for_spend"


@dataclass(frozen=True)
class PageMetric:
    page_number: int
    status: str
    sid: str | None
    page_type: str | None
    latency_seconds: float | None
    input_tokens: int
    output_tokens: int
    reasoning_tokens: int
    cost_usd: float
    cache_hit: bool
    retry_count: int
    model: str
    error: str


def page_metrics(run: ExtractionRun, model: str) -> list[PageMetric]:
    metrics = [
        PageMetric(
            page_number=result.page_number,
            status=PAGE_STATUS_OK,
            sid=result.extraction.sid,
            page_type=result.extraction.page_type,
            latency_seconds=result.latency_seconds,
            input_tokens=result.usage.input_tokens,
            output_tokens=result.usage.output_tokens,
            reasoning_tokens=result.usage.reasoning_tokens,
            cost_usd=result.cost_usd,
            cache_hit=result.from_cache,
            retry_count=result.retries,
            model=model,
            error="",
        )
        for result in run.results
    ]
    metrics += [
        PageMetric(
            page_number=failure.page_number,
            status=PAGE_STATUS_FAILED,
            sid=None,
            page_type=None,
            latency_seconds=failure.latency_seconds,
            input_tokens=failure.usage.input_tokens,
            output_tokens=failure.usage.output_tokens,
            reasoning_tokens=failure.usage.reasoning_tokens,
            cost_usd=failure.cost_usd,
            cache_hit=False,
            retry_count=failure.retries,
            model=model,
            error=failure.reason,
        )
        for failure in run.failures.values()
    ]
    metrics += [
        PageMetric(
            page_number=page_number,
            status=PAGE_STATUS_SKIPPED_FOR_SPEND,
            sid=None,
            page_type=None,
            latency_seconds=None,
            input_tokens=0,
            output_tokens=0,
            reasoning_tokens=0,
            cost_usd=0.0,
            cache_hit=False,
            retry_count=0,
            model=model,
            error="not started, --max-spend reached",
        )
        for page_number in run.skipped_for_spend
    ]
    return sorted(metrics, key=lambda metric: metric.page_number)


@dataclass(frozen=True)
class TimingAndCost:
    workers: int
    wall_clock_seconds: float
    pages_extracted: int
    pages_cached: int
    pages_called: int
    pages_failed: int
    students: float
    total_cost_usd: float
    latency_mean_seconds: float | None
    latency_median_seconds: float | None
    summed_seconds_per_student: float | None
    students_in_summed_mean: int
    wall_clock_seconds_per_student: float | None
    cost_per_page_usd: float | None
    cost_basis: str

    def cost_per(self, pages: int) -> float | None:
        return None if self.cost_per_page_usd is None else self.cost_per_page_usd * pages

    def as_dict(self) -> dict:
        return {
            "workers": self.workers,
            "wall_clock_seconds": round(self.wall_clock_seconds, 3),
            "pages_extracted": self.pages_extracted,
            "pages_cached": self.pages_cached,
            "pages_called": self.pages_called,
            "pages_failed": self.pages_failed,
            "students": self.students,
            "total_cost_usd": round(self.total_cost_usd, 6),
            "latency_mean_seconds": rounded(self.latency_mean_seconds),
            "latency_median_seconds": rounded(self.latency_median_seconds),
            "summed_processing_seconds_per_student": rounded(self.summed_seconds_per_student),
            "students_in_summed_processing_mean": self.students_in_summed_mean,
            "wall_clock_seconds_per_student": rounded(self.wall_clock_seconds_per_student),
            "cost_per_page_usd": rounded(self.cost_per_page_usd, 6),
            "cost_per_10_pages_usd": rounded(self.cost_per(10), 6),
            "cost_per_student_usd": rounded(self.cost_per(2), 6),
            f"projected_cost_{PROJECTION_PAGES_PER_PDF}_page_pdf_usd": rounded(self.cost_per(PROJECTION_PAGES_PER_PDF), 4),
            f"projected_cost_{PROJECTION_PDF_COUNT}_pdfs_usd": rounded(self.cost_per(PROJECTION_PAGES_PER_PDF * PROJECTION_PDF_COUNT), 2),
            "cost_basis": self.cost_basis,
        }


def rounded(value: float | None, digits: int = 3) -> float | None:
    return None if value is None else round(value, digits)


def was_billed(metric: PageMetric) -> bool:
    if metric.cache_hit:
        return False
    if metric.status == PAGE_STATUS_OK:
        return True
    return metric.input_tokens > 0 or metric.output_tokens > 0


def summed_latency_per_student(metrics: list[PageMetric], selected_pages: list[int]) -> list[float]:
    latency_by_page = {
        metric.page_number: metric.latency_seconds
        for metric in metrics
        if metric.status == PAGE_STATUS_OK and not metric.cache_hit and metric.latency_seconds is not None
    }
    sums = []
    for index in range(0, len(selected_pages) - 1, 2):
        header_page, continuation_page = selected_pages[index], selected_pages[index + 1]
        if header_page in latency_by_page and continuation_page in latency_by_page:
            sums.append(latency_by_page[header_page] + latency_by_page[continuation_page])
    return sums


@dataclass(frozen=True)
class FilePages:
    metrics: list[PageMetric]
    selected_pages: list[int]


def timing_and_cost(
    files: list[FilePages],
    wall_clock_seconds: float,
    workers: int,
    fallback_cost_per_page: tuple[float, str] | None = None,
) -> TimingAndCost:
    metrics = [metric for file_pages in files for metric in file_pages.metrics]
    extracted = [metric for metric in metrics if metric.status == PAGE_STATUS_OK]
    cached = [metric for metric in extracted if metric.cache_hit]
    called = [metric for metric in metrics if was_billed(metric)]
    latencies = [metric.latency_seconds for metric in extracted if not metric.cache_hit and metric.latency_seconds is not None]
    student_sums = [total for file_pages in files for total in summed_latency_per_student(file_pages.metrics, file_pages.selected_pages)]
    students = len(extracted) / 2
    total_cost = sum(metric.cost_usd for metric in metrics)
    if called:
        cost_per_page = sum(metric.cost_usd for metric in called) / len(called)
        basis = f"average of {len(called)} billed API pages in this run; cached pages cost $0 and are excluded"
    elif fallback_cost_per_page is not None:
        cost_per_page, fallback_basis = fallback_cost_per_page
        basis = f"no billed API pages in this run, so {fallback_basis}"
    else:
        cost_per_page, basis = None, "no API-called pages to base a cost on"
    return TimingAndCost(
        workers=workers,
        wall_clock_seconds=wall_clock_seconds,
        pages_extracted=len(extracted),
        pages_cached=len(cached),
        pages_called=len(called),
        pages_failed=sum(1 for metric in metrics if metric.status == PAGE_STATUS_FAILED),
        students=students,
        total_cost_usd=total_cost,
        latency_mean_seconds=statistics.mean(latencies) if latencies else None,
        latency_median_seconds=statistics.median(latencies) if latencies else None,
        summed_seconds_per_student=statistics.mean(student_sums) if student_sums else None,
        students_in_summed_mean=len(student_sums),
        wall_clock_seconds_per_student=wall_clock_seconds / students if students else None,
        cost_per_page_usd=cost_per_page,
        cost_basis=basis,
    )


def seconds_text(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.2f}s"


def usd_text(value: float | None) -> str:
    return "n/a" if value is None else f"${value:.4f}"


def timing_and_cost_lines(stats: TimingAndCost) -> list[str]:
    labelled = [
        ("workers", str(stats.workers)),
        ("wall clock, total", seconds_text(stats.wall_clock_seconds)),
        ("pages cached (excluded below)", str(stats.pages_cached)),
        ("API latency per page, mean", seconds_text(stats.latency_mean_seconds)),
        ("API latency per page, median", seconds_text(stats.latency_median_seconds)),
        (
            "summed processing per student",
            f"{seconds_text(stats.summed_seconds_per_student)} (mean over {stats.students_in_summed_mean} students with both pages called)",
        ),
        ("wall clock per student", f"{seconds_text(stats.wall_clock_seconds_per_student)} ({stats.students:g} students)"),
        ("cost per page", usd_text(stats.cost_per_page_usd)),
        ("cost per 10 pages", usd_text(stats.cost_per(10))),
        ("cost per student", usd_text(stats.cost_per(2))),
        (f"projected, one {PROJECTION_PAGES_PER_PDF} page PDF", usd_text(stats.cost_per(PROJECTION_PAGES_PER_PDF))),
        (f"projected, {PROJECTION_PDF_COUNT} such PDFs", usd_text(stats.cost_per(PROJECTION_PAGES_PER_PDF * PROJECTION_PDF_COUNT))),
        ("cost basis", stats.cost_basis),
    ]
    lines = [f"    {label + ':':<32} {value}" for label, value in labelled]
    lines.insert(
        7,
        "    NOTE: summed processing per student adds that student's two API latencies; wall clock per student\n"
        f"          divides total run time by students. They differ because up to {stats.workers} API calls run concurrently.",
    )
    return lines
