from types import SimpleNamespace

import httpx2
import openai
import pytest

from rassi.cache import ResponseCache
from rassi.extract import PageExtractor
from rassi.render import RenderedPage
from rassi.spend import SpendTracker
from tests.fakes import header_page

REQUEST = httpx2.Request("POST", "https://api.openai.invalid/v1/responses")


def rate_limit_error() -> openai.RateLimitError:
    return openai.RateLimitError("rate limited", response=httpx2.Response(429, request=REQUEST), body=None)


def server_error() -> openai.InternalServerError:
    return openai.InternalServerError("server error", response=httpx2.Response(503, request=REQUEST), body=None)


def bad_request_error() -> openai.BadRequestError:
    return openai.BadRequestError("image could not be decoded", response=httpx2.Response(400, request=REQUEST), body=None)


def completed_response():
    return SimpleNamespace(
        id="response-1",
        status="completed",
        incomplete_details=None,
        output_text=header_page(sid="101").model_dump_json(),
        usage=SimpleNamespace(
            input_tokens=1000,
            output_tokens=500,
            output_tokens_details=SimpleNamespace(reasoning_tokens=100),
            input_tokens_details=SimpleNamespace(cached_tokens=0),
        ),
    )


class ScriptedResponses:
    def __init__(self, script):
        self.script = list(script)
        self.attempts = 0

    def create(self, **request):
        self.attempts += 1
        outcome = self.script.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class SteppingClock:
    def __init__(self, step: float):
        self.now = 0.0
        self.step = step

    def __call__(self) -> float:
        self.now += self.step
        return self.now


def make_extractor(tmp_path, script, sleeps: list[float]):
    responses = ScriptedResponses(script)
    extractor = PageExtractor(
        SimpleNamespace(responses=responses),
        "gpt-5.6-terra",
        ResponseCache(tmp_path / "cache"),
        SpendTracker("gpt-5.6-terra", max_spend_usd=1.0, fallback_estimate_usd=0.01),
        log=lambda message: None,
        sleep=sleeps.append,
        clock=SteppingClock(1.5),
    )
    return extractor, responses


PAGE = RenderedPage(page_number=1, png_bytes=b"page-one", width_px=10, height_px=10)


def test_retries_rate_limit_and_server_errors_with_exponential_backoff(tmp_path):
    sleeps: list[float] = []
    extractor, responses = make_extractor(tmp_path, [rate_limit_error(), server_error(), completed_response()], sleeps)
    run = extractor.extract_pages([PAGE], 1, workers=1)
    assert run.failures == {}
    result = run.results[0]
    assert result.retries == 2
    assert result.latency_seconds == pytest.approx(1.5)
    assert result.usage.reasoning_tokens == 100
    assert result.cost_usd == pytest.approx(0.008)
    assert sleeps == [2.0, 4.0]
    assert responses.attempts == 3


def test_page_failing_every_attempt_is_recorded_not_dropped(tmp_path):
    sleeps: list[float] = []
    extractor, responses = make_extractor(tmp_path, [rate_limit_error(), rate_limit_error(), rate_limit_error()], sleeps)
    run = extractor.extract_pages([PAGE], 1, workers=1)
    assert run.results == []
    failure = run.failures[1]
    assert failure.retries == 2
    assert "RateLimitError after 3 attempts" in failure.reason
    assert failure.cost_usd == 0
    assert run.failed_pages == {1: failure.reason}
    assert responses.attempts == 3
    assert sleeps == [2.0, 4.0]
    assert extractor.spend.reserved_usd == 0


def test_non_retryable_error_fails_immediately(tmp_path):
    sleeps: list[float] = []
    extractor, responses = make_extractor(tmp_path, [bad_request_error()], sleeps)
    run = extractor.extract_pages([PAGE], 1, workers=1)
    assert "BadRequestError" in run.failures[1].reason
    assert responses.attempts == 1
    assert sleeps == []


def test_invalid_response_is_not_cached(tmp_path):
    sleeps: list[float] = []
    bad = completed_response()
    bad.output_text = header_page(sid="101", items=[]).model_dump_json()
    extractor, _ = make_extractor(tmp_path, [bad], sleeps)
    run = extractor.extract_pages([PAGE], 1, workers=1)
    assert "returned 0 items" in run.failures[1].reason
    assert run.failures[1].cost_usd == pytest.approx(0.008)
    assert not any((tmp_path / "cache").rglob("*.json"))
