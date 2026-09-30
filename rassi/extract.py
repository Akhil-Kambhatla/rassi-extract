import base64
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timezone

import openai
from pydantic import ValidationError

from rassi.cache import ResponseCache, cache_key
from rassi.config import (
    API_BACKOFF_BASE_SECONDS,
    API_MAX_ATTEMPTS,
    API_TIMEOUT_SECONDS,
    IMAGE_DETAIL,
    REASONING_EFFORT_PREFERENCE,
)
from rassi.prompt import PROMPT_VERSION, SYSTEM_INSTRUCTIONS, USER_INSTRUCTION
from rassi.render import RenderedPage
from rassi.schema import SCHEMA_VERSION, PageExtraction, structured_output_format
from rassi.spend import SpendTracker, TokenUsage
from rassi.structure import response_problems

RETRYABLE_ERRORS = (openai.RateLimitError, openai.InternalServerError, openai.APIConnectionError)


class RetriesExhausted(RuntimeError):
    def __init__(self, last_error: Exception, retries: int):
        super().__init__(f"{type(last_error).__name__} after {retries + 1} attempts: {last_error}")
        self.retries = retries


class PageFailed(RuntimeError):
    def __init__(
        self,
        reason: str,
        retries: int = 0,
        usage: TokenUsage | None = None,
        cost_usd: float = 0.0,
        latency_seconds: float | None = None,
    ):
        super().__init__(reason)
        self.reason = reason
        self.retries = retries
        self.usage = usage if usage is not None else TokenUsage()
        self.cost_usd = cost_usd
        self.latency_seconds = latency_seconds


@dataclass
class PageResult:
    page_number: int
    extraction: PageExtraction
    model: str
    from_cache: bool
    usage: TokenUsage
    cost_usd: float
    reasoning_effort: str | None
    response_id: str | None
    cache_key: str
    latency_seconds: float | None = None
    retries: int = 0


@dataclass
class PageFailure:
    page_number: int
    reason: str
    retries: int
    usage: TokenUsage
    cost_usd: float
    latency_seconds: float | None


@dataclass
class ExtractionRun:
    results: list[PageResult] = field(default_factory=list)
    failures: dict[int, PageFailure] = field(default_factory=dict)
    skipped_for_spend: list[int] = field(default_factory=list)
    cache_hits: int = 0

    @property
    def failed_pages(self) -> dict[int, str]:
        return {page_number: failure.reason for page_number, failure in sorted(self.failures.items())}


@dataclass
class ApiCall:
    response: object
    effort: str | None
    latency_seconds: float
    retries: int


def png_data_url(png_bytes: bytes) -> str:
    return "data:image/png;base64," + base64.b64encode(png_bytes).decode("ascii")


def build_input(png_bytes: bytes) -> list[dict]:
    return [
        {
            "role": "user",
            "content": [
                {"type": "input_text", "text": USER_INSTRUCTION},
                {"type": "input_image", "image_url": png_data_url(png_bytes), "detail": IMAGE_DETAIL},
            ],
        }
    ]


def usage_from_response(response) -> TokenUsage:
    usage = response.usage
    if usage is None:
        return TokenUsage()
    reasoning_tokens = getattr(getattr(usage, "output_tokens_details", None), "reasoning_tokens", 0) or 0
    cached_tokens = getattr(getattr(usage, "input_tokens_details", None), "cached_tokens", 0) or 0
    return TokenUsage(
        input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
        reasoning_tokens=reasoning_tokens,
        cached_input_tokens=cached_tokens,
    )


def is_reasoning_effort_rejection(error: openai.BadRequestError) -> bool:
    text = str(error).lower()
    return "reasoning" in text or "effort" in text


def backoff_seconds(failed_attempt: int) -> float:
    return API_BACKOFF_BASE_SECONDS * 2 ** (failed_attempt - 1)


class PageExtractor:
    def __init__(
        self,
        client: openai.OpenAI,
        model: str,
        cache: ResponseCache,
        spend: SpendTracker,
        log=print,
        sleep=time.sleep,
        clock=time.perf_counter,
    ):
        self.client = client
        self.model = model
        self.cache = cache
        self.spend = spend
        self.log = log
        self.sleep = sleep
        self.clock = clock
        self._effort_candidates = list(REASONING_EFFORT_PREFERENCE)
        self._effort_lock = threading.Lock()
        self._log_lock = threading.Lock()

    def say(self, message: str) -> None:
        with self._log_lock:
            self.log(message)

    def current_effort(self) -> str | None:
        with self._effort_lock:
            return self._effort_candidates[0] if self._effort_candidates else None

    def reject_effort(self, effort: str | None) -> None:
        with self._effort_lock:
            if self._effort_candidates and self._effort_candidates[0] == effort:
                self._effort_candidates.pop(0)
                self.say(f"  model rejected reasoning effort '{effort}', trying the next lowest")

    def create_with_retries(self, request: dict, page_number: int) -> tuple[object, float, int]:
        for attempt in range(1, API_MAX_ATTEMPTS + 1):
            started = self.clock()
            try:
                response = self.client.responses.create(**request)
                return response, self.clock() - started, attempt - 1
            except RETRYABLE_ERRORS as error:
                if attempt == API_MAX_ATTEMPTS:
                    raise RetriesExhausted(error, attempt - 1) from error
                delay = backoff_seconds(attempt)
                self.say(
                    f"page {page_number:>3}  {type(error).__name__} on attempt {attempt}/{API_MAX_ATTEMPTS}, retrying in {delay:g}s"
                )
                self.sleep(delay)
        raise AssertionError("unreachable")

    def request_page(self, png_bytes: bytes, page_number: int) -> ApiCall:
        retries = 0
        while True:
            effort = self.current_effort()
            request = {
                "model": self.model,
                "instructions": SYSTEM_INSTRUCTIONS,
                "input": build_input(png_bytes),
                "text": {"format": structured_output_format()},
                "store": False,
            }
            if effort is not None:
                request["reasoning"] = {"effort": effort}
            try:
                response, latency, attempt_retries = self.create_with_retries(request, page_number)
                return ApiCall(response, effort, latency, retries + attempt_retries)
            except RetriesExhausted as error:
                raise PageFailed(str(error), retries=retries + error.retries) from error
            except openai.BadRequestError as error:
                if effort is not None and is_reasoning_effort_rejection(error):
                    self.reject_effort(effort)
                    continue
                raise PageFailed(f"{type(error).__name__}: {error}", retries=retries) from error

    def extract_page(self, page: RenderedPage, total_pages: int) -> PageResult | None:
        key = cache_key(page.png_bytes, self.model, PROMPT_VERSION, SCHEMA_VERSION)
        cached = self.cache.load(key)
        if cached is not None:
            self.say(f"page {page.page_number:>3}/{total_pages}  cache hit  $0.0000  running ${self.spend.spent_usd:.4f}")
            return PageResult(
                page_number=page.page_number,
                extraction=PageExtraction.model_validate(cached["extraction"]),
                model=self.model,
                from_cache=True,
                usage=TokenUsage(),
                cost_usd=0.0,
                reasoning_effort=cached.get("reasoning_effort"),
                response_id=cached.get("response_id"),
                cache_key=key,
            )

        reservation = self.spend.try_reserve_call()
        if reservation is None:
            return None

        try:
            call = self.request_page(page.png_bytes, page.page_number)
        except Exception:
            self.spend.release_reservation(reservation)
            raise

        response = call.response
        usage = usage_from_response(response)
        call_cost, running_total = self.spend.record_call(reservation, usage)
        self.say(
            f"page {page.page_number:>3}/{total_pages}  api call   {call.latency_seconds:.1f}s  in={usage.input_tokens:,} "
            f"out={usage.output_tokens:,} (reasoning {usage.reasoning_tokens:,})  retries {call.retries}  "
            f"${call_cost:.4f}  running ${running_total:.4f}"
        )

        def failed(reason: str) -> PageFailed:
            return PageFailed(reason, call.retries, usage, call_cost, call.latency_seconds)

        if response.status != "completed":
            detail = getattr(response.incomplete_details, "reason", None) if response.incomplete_details else None
            raise failed(f"response status '{response.status}'" + (f" ({detail})" if detail else ""))

        try:
            extraction = PageExtraction.model_validate_json(response.output_text)
        except ValidationError as error:
            raise failed(f"model output did not match schema: {error}") from error

        problems = response_problems(extraction)
        if problems:
            raise failed("invalid page response, not cached: " + "; ".join(problems))

        self.cache.store(
            key,
            {
                "model": self.model,
                "prompt_version": PROMPT_VERSION,
                "schema_version": SCHEMA_VERSION,
                "reasoning_effort": call.effort,
                "response_id": response.id,
                "usage": usage.as_dict(),
                "cost_usd": call_cost,
                "latency_seconds": call.latency_seconds,
                "retries": call.retries,
                "created_at": datetime.now(timezone.utc).isoformat(),
                "extraction": extraction.model_dump(),
            },
        )
        return PageResult(
            page_number=page.page_number,
            extraction=extraction,
            model=self.model,
            from_cache=False,
            usage=usage,
            cost_usd=call_cost,
            reasoning_effort=call.effort,
            response_id=response.id,
            cache_key=key,
            latency_seconds=call.latency_seconds,
            retries=call.retries,
        )

    def record_failure(self, run: ExtractionRun, page_number: int, total_pages: int, error: Exception) -> None:
        if isinstance(error, PageFailed):
            failure = PageFailure(page_number, error.reason, error.retries, error.usage, error.cost_usd, error.latency_seconds)
        else:
            failure = PageFailure(page_number, f"{type(error).__name__}: {error}", 0, TokenUsage(), 0.0, None)
        run.failures[page_number] = failure
        self.say(f"page {page_number:>3}/{total_pages}  FAILED  {failure.reason}")

    def extract_pages(self, pages: list[RenderedPage], total_pages: int, workers: int) -> ExtractionRun:
        run = ExtractionRun()
        with ThreadPoolExecutor(max_workers=max(1, workers)) as executor:
            futures = {executor.submit(self.extract_page, page, total_pages): page for page in pages}
            for future in as_completed(futures):
                page = futures[future]
                try:
                    result = future.result()
                except Exception as error:
                    self.record_failure(run, page.page_number, total_pages, error)
                    continue
                if result is None:
                    run.skipped_for_spend.append(page.page_number)
                    continue
                if result.from_cache:
                    run.cache_hits += 1
                run.results.append(result)
        run.results.sort(key=lambda result: result.page_number)
        run.skipped_for_spend.sort()
        return run


def make_client() -> openai.OpenAI:
    return openai.OpenAI(max_retries=0, timeout=API_TIMEOUT_SECONDS)
