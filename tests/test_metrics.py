from pathlib import Path

import pytest

from rassi.metrics import (
    PAGE_STATUS_FAILED,
    PAGE_STATUS_OK,
    FilePages,
    PageMetric,
    timing_and_cost,
    timing_and_cost_lines,
)
from rassi.render import count_pages, render_pages

FIXTURE_PDF = Path(__file__).parent / "fixtures" / "synthetic_two_page.pdf"


def metric(page_number: int, latency: float | None, cost: float, cache_hit: bool = False, status: str = PAGE_STATUS_OK) -> PageMetric:
    return PageMetric(
        page_number=page_number,
        status=status,
        sid=None,
        page_type=None,
        latency_seconds=latency,
        input_tokens=0 if cache_hit else 1000,
        output_tokens=0 if cache_hit else 500,
        reasoning_tokens=0,
        cost_usd=cost,
        cache_hit=cache_hit,
        retry_count=0,
        model="gpt-5.6-terra",
        error="",
    )


def test_timing_and_cost_statistics():
    metrics = [
        metric(1, 4.0, 0.01),
        metric(2, 6.0, 0.03),
        metric(3, None, 0.0, cache_hit=True),
        metric(4, 2.0, 0.02),
        metric(5, 8.0, 0.02),
        metric(6, 10.0, 0.02),
    ]
    stats = timing_and_cost([FilePages(metrics, [1, 2, 3, 4, 5, 6])], wall_clock_seconds=12.0, workers=4)
    assert stats.pages_cached == 1
    assert stats.pages_called == 5
    assert stats.latency_mean_seconds == pytest.approx(6.0)
    assert stats.latency_median_seconds == pytest.approx(6.0)
    assert stats.students_in_summed_mean == 2
    assert stats.summed_seconds_per_student == pytest.approx((10.0 + 18.0) / 2)
    assert stats.students == 3
    assert stats.wall_clock_seconds_per_student == pytest.approx(4.0)
    assert stats.cost_per_page_usd == pytest.approx(0.02)
    assert stats.cost_per(10) == pytest.approx(0.2)
    assert stats.cost_per(2) == pytest.approx(0.04)
    assert stats.as_dict()["projected_cost_50_page_pdf_usd"] == pytest.approx(1.0)
    assert stats.as_dict()["projected_cost_100_pdfs_usd"] == pytest.approx(100.0)
    text = "\n".join(timing_and_cost_lines(stats))
    assert "They differ because up to 4 API calls run concurrently" in text


def test_failed_page_without_a_billed_response_does_not_dilute_cost_per_page():
    metrics = [metric(1, 3.0, 0.02), metric(2, None, 0.0, status=PAGE_STATUS_FAILED)]
    unbilled_failure = metrics[1].__class__(**{**metrics[1].__dict__, "input_tokens": 0, "output_tokens": 0})
    stats = timing_and_cost([FilePages([metrics[0], unbilled_failure], [1, 2])], 5.0, 1)
    assert stats.pages_failed == 1
    assert stats.cost_per_page_usd == pytest.approx(0.02)
    assert stats.students_in_summed_mean == 0


def test_combined_statistics_keep_student_pairs_within_each_file():
    first = FilePages([metric(1, 1.0, 0.01), metric(2, 2.0, 0.01)], [1, 2])
    second = FilePages([metric(1, 3.0, 0.01), metric(2, 4.0, 0.01)], [1, 2])
    stats = timing_and_cost([first, second], 10.0, 2)
    assert stats.students_in_summed_mean == 2
    assert stats.summed_seconds_per_student == pytest.approx(5.0)


def test_all_cached_run_uses_the_fallback_cost_basis():
    stats = timing_and_cost([FilePages([metric(1, None, 0.0, cache_hit=True)], [1])], 1.0, 1, (0.024, "default token estimate"))
    assert stats.cost_per_page_usd == pytest.approx(0.024)
    assert "no billed API pages" in stats.cost_basis


def test_synthetic_fixture_pdf_renders():
    assert count_pages(FIXTURE_PDF) == 2
    rendered = render_pages(FIXTURE_PDF, [1, 2], dpi=72)
    assert [page.page_number for page in rendered] == [1, 2]
    assert rendered[0].png_bytes.startswith(b"\x89PNG")
    assert (rendered[0].width_px, rendered[0].height_px) == (612, 792)
