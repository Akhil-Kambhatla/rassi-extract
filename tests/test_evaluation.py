import base64
import json
from pathlib import Path
from types import SimpleNamespace

import pymupdf
import pytest

from rassi import cli, eval_cli
from rassi.cache import cache_key
from rassi.evaluation import (
    CaseError,
    CaseLabel,
    CaseResult,
    CaseRun,
    EvalCase,
    StatusChange,
    ValueKey,
    eval_cache_key,
    failed_run_score,
    item_expectations,
    load_case,
    load_library,
    page_expectations,
    parse_label,
    parse_page_label,
    score_extraction,
    select_item,
    status_changes,
    summarize,
)
from rassi.paths import DataLayout
from rassi.prompt import EVAL_USER_INSTRUCTION, PROMPT_VERSION, USER_INSTRUCTION
from rassi.schema import SCHEMA_VERSION, PageExtraction

from tests.fakes import continuation_page, header_page, make_item
from tests.test_crop import PAGE_HEIGHT, PAGE_WIDTH, draw_form_page

VALID_LABEL = {"item_number": 6, "agreement": "5", "reason": "d", "confidence": "5"}
PAGE_ITEMS = [
    {"item_number": 7, "agreement": "blank", "reason": "blank", "confidence": "blank"},
    {"item_number": 8, "agreement": "4", "reason": "c", "confidence": "5"},
    {"item_number": 9, "agreement": "1", "reason": "a", "confidence": "6"},
    {"item_number": 10, "agreement": "5", "reason": "b", "confidence": "4"},
    {"item_number": 11, "agreement": "4", "reason": "c", "confidence": "2"},
    {"item_number": 12, "agreement": "5", "reason": "blank", "confidence": "blank"},
]
VALID_PAGE_LABEL = {"page_type": "continuation", "sid": None, "items": PAGE_ITEMS}


def label(**overrides) -> CaseLabel:
    return CaseLabel.model_validate({**VALID_LABEL, **overrides})


def write_case(
    root: Path,
    case_id: str,
    label_data,
    image: bytes = b"crop-bytes",
    notes: str | None = "why it is hard",
    case_type: str | None = "crop",
) -> Path:
    directory = root / case_id
    directory.mkdir(parents=True)
    (directory / ("page.png" if case_type == "page" else "crop.png")).write_bytes(image)
    (directory / "label.json").write_text(label_data if isinstance(label_data, str) else json.dumps(label_data))
    if case_type is not None:
        (directory / "case.json").write_text(json.dumps({"case_type": case_type}))
    if notes is not None:
        (directory / "notes.md").write_text(notes + "\n")
    return directory


def crop_case(case_label: CaseLabel | None = None, case_id: str = "c") -> EvalCase:
    return EvalCase(case_id, "crop", b"crop", item_expectations(case_label or label()), "")


def page_case(case_id: str = "p") -> EvalCase:
    return EvalCase(case_id, "page", b"page", page_expectations(parse_page_label(VALID_PAGE_LABEL, "label.json")), "")


def items_from_label(items: list[dict], **overrides_by_item: dict) -> list:
    return [make_item(**{**item, **overrides_by_item.get(f"q{item['item_number']}", {})}) for item in items]


def correct_page(**overrides_by_item: dict) -> PageExtraction:
    return continuation_page(items=items_from_label(PAGE_ITEMS, **overrides_by_item))


def result_for(case: EvalCase, extractions: list[PageExtraction | None]) -> CaseResult:
    runs = []
    for index, extraction in enumerate(extractions):
        score = failed_run_score(case, "api call failed: boom") if extraction is None else score_extraction(case, extraction)
        runs.append(CaseRun(case, index, score))
    return CaseResult(case, runs)


def test_valid_label_loads_with_notes_and_crop(tmp_path):
    case = load_case(write_case(tmp_path, "span-into-neighbour", VALID_LABEL))
    assert case.case_id == "span-into-neighbour"
    assert case.case_type == "crop"
    assert case.expected == item_expectations(label())
    assert case.image_png == b"crop-bytes"
    assert case.notes == "why it is hard"


@pytest.mark.parametrize(
    "overrides,problem",
    [
        ({"reason": "e"}, "reason"),
        ({"reason": "5"}, "reason"),
        ({"agreement": "7"}, "agreement"),
        ({"confidence": 5}, "confidence"),
        ({"item_number": 13}, "item_number"),
        ({"extra": "field"}, "extra"),
    ],
)
def test_invalid_labels_are_rejected_with_the_field_named(overrides, problem):
    with pytest.raises(CaseError, match=problem):
        parse_label({**VALID_LABEL, **overrides}, "label.json")


def test_label_accepts_every_extraction_flag_value():
    for value in ("blank", "ambiguous"):
        assert parse_label({**VALID_LABEL, "agreement": value, "reason": value, "confidence": value}, "label.json").reason == value


def test_label_accepts_comma_joined_multiple_marks():
    label = parse_label({**VALID_LABEL, "agreement": "1,4", "reason": "a,c,d", "confidence": "2,3,5"}, "label.json")
    assert (label.agreement, label.reason, label.confidence) == ("1,4", "a,c,d", "2,3,5")


def test_label_rejects_the_multiple_word():
    with pytest.raises(CaseError, match="reason"):
        parse_label({**VALID_LABEL, "reason": "multiple"}, "label.json")


def test_label_missing_a_field_is_rejected():
    data = dict(VALID_LABEL)
    del data["confidence"]
    with pytest.raises(CaseError, match="confidence"):
        parse_label(data, "label.json")


def test_library_skips_unlabeled_cases_and_reports_broken_ones(tmp_path):
    write_case(tmp_path, "good", VALID_LABEL)
    write_case(tmp_path, "fresh", {"item_number": 3, "agreement": None, "reason": None, "confidence": None})
    library = load_library(tmp_path)
    assert [case.case_id for case in library.cases] == ["good"]
    assert library.unlabeled == ["fresh"]

    write_case(tmp_path, "broken", "{not json")
    with pytest.raises(CaseError, match="not valid JSON"):
        load_library(tmp_path)


def test_partly_filled_label_is_an_error_not_unlabeled(tmp_path):
    write_case(tmp_path, "partial", {"item_number": 3, "agreement": "2", "reason": None, "confidence": None})
    with pytest.raises(CaseError, match="partial"):
        load_library(tmp_path)


def test_case_without_crop_or_with_bad_id_is_rejected(tmp_path):
    directory = write_case(tmp_path, "no-crop", VALID_LABEL)
    (directory / "crop.png").unlink()
    with pytest.raises(CaseError, match="missing crop.png"):
        load_case(directory)
    with pytest.raises(CaseError, match="case id"):
        load_case(write_case(tmp_path, "Bad Name", VALID_LABEL))


def test_case_without_case_json_or_with_unknown_type_is_rejected(tmp_path):
    with pytest.raises(CaseError, match="missing case.json"):
        load_library(write_case(tmp_path / "a", "no-type", VALID_LABEL, case_type=None).parent)
    directory = write_case(tmp_path / "b", "odd-type", VALID_LABEL)
    (directory / "case.json").write_text(json.dumps({"case_type": "strip"}))
    with pytest.raises(CaseError, match="case_type"):
        load_case(directory)


def test_page_case_loads_page_fields_and_all_eighteen_values(tmp_path):
    case = load_case(write_case(tmp_path, "full-page", VALID_PAGE_LABEL, image=b"page-bytes", case_type="page"))
    assert case.case_type == "page"
    assert case.image_png == b"page-bytes"
    assert case.item_numbers == [7, 8, 9, 10, 11, 12]
    assert len(case.expected) == 20
    assert case.expected[ValueKey(None, "page_type")] == "continuation"
    assert case.expected[ValueKey(None, "sid")] is None
    assert case.expected[ValueKey(8, "reason")] == "c"


def test_page_case_needs_page_png(tmp_path):
    directory = write_case(tmp_path, "full-page", VALID_PAGE_LABEL, case_type="page")
    (directory / "page.png").unlink()
    (directory / "crop.png").write_bytes(b"wrong file")
    with pytest.raises(CaseError, match="missing page.png"):
        load_case(directory)


def partial_page_data():
    return {
        "partial": True,
        "page_type": "continuation",
        "sid": None,
        "items": [{"item_number": number, **({"reason": "c"} if number == 8 else {})} for number in range(7, 13)],
    }


def test_explicit_partial_page_scores_only_adjudicated_values(tmp_path):
    case = load_case(write_case(tmp_path, "adjudicated", partial_page_data(), case_type="page"))
    assert set(case.expected) == {ValueKey(None, "page_type"), ValueKey(None, "sid"), ValueKey(8, "reason")}
    result = result_for(case, [correct_page(q9={"agreement": "ambiguous"})])
    assert result.status == "pass"
    assert summarize([result]).overall.total == 1


def test_partial_page_still_requires_all_six_extracted_items(tmp_path):
    case = load_case(write_case(tmp_path, "adjudicated", partial_page_data(), case_type="page"))
    extraction = correct_page()
    extraction.items = [item for item in extraction.items if item.item_number == 8]
    assert score_extraction(case, extraction).status == "fail"


def test_partial_page_requires_opt_in_and_an_adjudicated_value():
    data = partial_page_data()
    del data["partial"]
    with pytest.raises(CaseError):
        parse_page_label(data, "label.json")
    data = partial_page_data()
    data["items"][1].pop("reason")
    with pytest.raises(CaseError, match="at least one adjudicated"):
        parse_page_label(data, "label.json")


def test_partial_page_cli_prints_uneven_numbers_of_labels(tmp_path, capsys):
    data = partial_page_data()
    data["items"][2]["agreement"] = "1"
    data["items"][2]["confidence"] = "6"
    case = load_case(write_case(tmp_path, "adjudicated", data, case_type="page"))
    eval_cli.print_case(result_for(case, [correct_page()]))
    assert "Q9" in capsys.readouterr().out


@pytest.mark.parametrize(
    "overrides,problem",
    [
        ({"items": PAGE_ITEMS[:5]}, "holds items 7 to 12"),
        ({"items": PAGE_ITEMS[:5] + [PAGE_ITEMS[0]]}, "holds items 7 to 12"),
        ({"page_type": "header"}, "holds items 1 to 6"),
        ({"sid": "123456"}, "sid must be null"),
        ({"items": PAGE_ITEMS[:5] + [{**PAGE_ITEMS[5], "reason": "e"}]}, "reason"),
        ({"page_type": "cover"}, "page_type"),
    ],
)
def test_invalid_page_labels_are_rejected(overrides, problem):
    with pytest.raises(CaseError, match=problem):
        parse_page_label({**VALID_PAGE_LABEL, **overrides}, "label.json")


def test_header_page_label_keeps_its_sid():
    data = {"page_type": "header", "sid": "REDACTED", "items": [{**item, "item_number": item["item_number"] - 6} for item in PAGE_ITEMS]}
    assert page_expectations(parse_page_label(data, "label.json"))[ValueKey(None, "sid")] == "REDACTED"


def test_empty_page_skeleton_is_unlabeled_and_partial_page_label_is_an_error(tmp_path):
    skeleton = {"page_type": None, "sid": None, "items": [{**item, "agreement": None, "reason": None, "confidence": None} for item in PAGE_ITEMS]}
    write_case(tmp_path, "fresh-page", skeleton, case_type="page")
    assert load_library(tmp_path).unlabeled == ["fresh-page"]
    write_case(tmp_path, "partial-page", {**skeleton, "page_type": "continuation"}, case_type="page")
    with pytest.raises(CaseError, match="partial-page"):
        load_library(tmp_path)


def test_mixed_library_loads_crop_and_page_cases_together(tmp_path):
    write_case(tmp_path, "a-crop", VALID_LABEL)
    write_case(tmp_path, "b-page", VALID_PAGE_LABEL, case_type="page")
    library = load_library(tmp_path)
    assert [(case.case_id, case.case_type, len(case.expected)) for case in library.cases] == [("a-crop", "crop", 3), ("b-page", "page", 20)]


def test_full_match_passes():
    score = score_extraction(crop_case(), continuation_page(items=[make_item(6, agreement="5", reason="d", confidence="5")]))
    assert score.status == "pass"
    assert list(score.matches.values()) == [True, True, True]


def test_partial_match_fails_but_credits_matching_dimensions():
    case = crop_case()
    result = result_for(case, [continuation_page(items=[make_item(6, agreement="ambiguous", reason="d", confidence="5")])])
    assert result.runs[0].score.status == "fail"
    assert result.runs[0].score.matches[ValueKey(6, "agreement")] is False
    summary = summarize([result])
    assert summary.cases_passed.correct == 0
    assert summary.overall.correct == 2
    assert summary.overall.total == 3
    assert summary.per_dimension["agreement"].fraction == 0
    assert summary.per_dimension["reason"].fraction == 1


def test_wrong_item_number_fails_even_when_values_match():
    score = score_extraction(crop_case(), continuation_page(items=[make_item(5, agreement="5", reason="d", confidence="5")]))
    assert score.status == "fail"
    assert all(score.matches.values())


def test_summary_across_cases_and_not_run_cases():
    passing = crop_case(case_id="a")
    wrong = crop_case(case_id="b")
    results = [
        result_for(passing, [continuation_page(items=[make_item(6, agreement="5", reason="d", confidence="5")])]),
        result_for(wrong, [continuation_page(items=[make_item(6, agreement="6", reason="c,d", confidence="5")])]),
        result_for(crop_case(case_id="c"), [None]),
        CaseResult(crop_case(case_id="d"), [CaseRun(crop_case(case_id="d"), 0, failed_run_score(crop_case(), "not run: --max-spend reached"))]),
    ]
    assert [result.status for result in results] == ["pass", "fail", "fail", "not_run"]
    summary = summarize(results)
    assert summary.not_run == 1
    assert (summary.cases_passed.correct, summary.cases_passed.total) == (1, 3)
    assert (summary.overall.correct, summary.overall.total) == (4, 9)
    assert summary.per_dimension["confidence"].correct == 2


def test_page_case_scores_eighteen_values_and_page_fields_separately():
    case = page_case()
    result = result_for(case, [correct_page(q8={"reason": "c,d"}, q12={"agreement": "4"})])
    summary = summarize([result])
    assert (summary.overall.correct, summary.overall.total) == (16, 18)
    assert (summary.per_dimension["reason"].correct, summary.per_dimension["reason"].total) == (5, 6)
    assert (summary.page_fields.correct, summary.page_fields.total) == (2, 2)
    assert result.status == "fail"


def test_page_case_with_wrong_page_type_fails_without_costing_item_values():
    page = correct_page()
    wrong_type = PageExtraction(**{**page.model_dump(), "page_type": "header", "sid": "999"})
    result = result_for(page_case(), [wrong_type])
    summary = summarize([result])
    assert summary.overall.correct == 18
    assert summary.page_fields.correct == 0
    assert result.status == "fail"
    assert "should hold items 1 to 6" in result.runs[0].score.error


def test_page_case_missing_or_duplicated_items_count_as_wrong():
    page = correct_page()
    duplicated = continuation_page(items=page.items[:5] + [make_item(11)])
    score = score_extraction(page_case(), duplicated)
    assert score.status == "fail"
    assert not score.item_numbers_match
    assert score.matches[ValueKey(12, "agreement")] is False
    assert score.matches[ValueKey(11, "agreement")] is False
    assert score.matches[ValueKey(10, "agreement")] is True
    assert "appear more than once" in score.error


def test_page_case_keeps_model_notes_for_each_value():
    page = correct_page(q8={"reason_notes": "circle on c; arc from agreement circle on 4 crosses into d, discounted"})
    score = score_extraction(page_case(), page)
    assert score.model_notes[ValueKey(8, "reason")].endswith("discounted")


def test_repeated_runs_count_correct_runs_and_flag_unstable_values():
    case = page_case()
    result = result_for(case, [correct_page(), correct_page(q8={"reason": "c,d"}), correct_page(), correct_page(), correct_page()])
    reason_8 = ValueKey(8, "reason")
    assert result.correct_runs(reason_8) == 4
    assert result.is_unstable(reason_8)
    assert result.unstable_keys == [reason_8]
    assert result.runs_passed == 4
    assert result.status == "unstable"
    summary = summarize([result], repeat=5)
    assert summary.unstable_values == 1
    assert (summary.overall.correct, summary.overall.total) == (89, 90)
    assert (summary.runs_passed.correct, summary.runs_passed.total) == (4, 5)
    assert (summary.cases_passed.correct, summary.cases_passed.total) == (0, 1)


def test_consistently_wrong_value_is_not_unstable_but_varying_wrong_answers_are():
    case = page_case()
    steady = result_for(case, [correct_page(q8={"reason": "c,d"})] * 3)
    assert steady.correct_runs(ValueKey(8, "reason")) == 0
    assert steady.unstable_keys == []
    assert steady.status == "fail"
    varying = result_for(case, [correct_page(q8={"reason": "c,d"}), correct_page(q8={"reason": "ambiguous"})])
    assert varying.unstable_keys == [ValueKey(8, "reason")]
    assert varying.status == "fail"


def test_failed_run_makes_every_value_unstable_against_successful_runs():
    result = result_for(crop_case(), [continuation_page(items=[make_item(6, agreement="5", reason="d", confidence="5")]), None])
    assert len(result.unstable_keys) == 3
    assert result.status == "unstable"


def test_select_item_prefers_single_item_then_matching_number():
    single = continuation_page(items=[make_item(7)])
    assert select_item(single, 7) == (single.items[0], "")
    several = header_page()
    item, note = select_item(several, 4)
    assert item.item_number == 4
    assert "returned 6 items" in note
    item, note = select_item(header_page(items=[make_item(1), make_item(2)]), 9)
    assert item is None
    assert "expected exactly one" in note


def test_status_changes_since_previous_run():
    after = {"stays": "pass", "fixed": "pass", "added": "fail", "wobbles": "unstable"}
    before = {"stays": "pass", "fixed": "fail", "dropped": "pass", "wobbles": "pass"}
    assert status_changes(before, after) == [
        StatusChange("added", "new", "fail"),
        StatusChange("dropped", "pass", "removed"),
        StatusChange("fixed", "fail", "pass"),
        StatusChange("wobbles", "pass", "unstable"),
    ]
    assert status_changes(None, after) == []


def test_cache_key_includes_prompt_version_model_crop_and_repeat_index():
    base = eval_cache_key(b"crop", "gpt-5.6-terra")
    assert base == cache_key(b"crop", "gpt-5.6-terra", PROMPT_VERSION, SCHEMA_VERSION)
    assert base == eval_cache_key(b"crop", "gpt-5.6-terra", PROMPT_VERSION, repeat_index=0)
    assert base != eval_cache_key(b"crop", "gpt-5.6-terra", PROMPT_VERSION + "-next")
    assert base != eval_cache_key(b"crop", "gpt-5.6-luna")
    assert base != eval_cache_key(b"other crop", "gpt-5.6-terra")
    repeats = {eval_cache_key(b"crop", "gpt-5.6-terra", repeat_index=index) for index in range(5)}
    assert len(repeats) == 5


class FakeResponses:
    def __init__(self, pages_by_image: dict[bytes, list[PageExtraction]]):
        self.pages_by_image = pages_by_image
        self.requests = []
        self.calls_per_image: dict[bytes, int] = {}

    def create(self, **request):
        self.requests.append(request)
        image = base64.b64decode(request["input"][0]["content"][1]["image_url"].split(",", 1)[1])
        call_number = self.calls_per_image.get(image, 0)
        self.calls_per_image[image] = call_number + 1
        pages = self.pages_by_image[image]
        page = pages[call_number % len(pages)]
        return SimpleNamespace(
            id=f"response-{len(self.requests)}",
            status="completed",
            incomplete_details=None,
            output_text=page.model_dump_json(),
            usage=SimpleNamespace(
                input_tokens=1000,
                output_tokens=500,
                output_tokens_details=SimpleNamespace(reasoning_tokens=0),
                input_tokens_details=SimpleNamespace(cached_tokens=0),
            ),
        )

    def instruction_for(self, image: bytes) -> set[str]:
        texts = set()
        for request in self.requests:
            content = request["input"][0]["content"]
            if base64.b64decode(content[1]["image_url"].split(",", 1)[1]) == image:
                texts.add(content[0]["text"])
        return texts


@pytest.fixture
def layout(tmp_path) -> DataLayout:
    return DataLayout(input_root=tmp_path / "in", output_root=tmp_path / "out", cache_root=tmp_path / "cache", eval_root=tmp_path / "eval")


def install_fake_api(monkeypatch, pages_by_image: dict[bytes, list[PageExtraction]]) -> FakeResponses:
    responses = FakeResponses(pages_by_image)
    monkeypatch.setattr(eval_cli, "make_client", lambda: SimpleNamespace(responses=responses))
    monkeypatch.setattr(eval_cli, "load_dotenv", lambda path: None)
    monkeypatch.setenv("OPENAI_API_KEY", "test-key-not-real")
    return responses


@pytest.fixture
def fake_eval_api(monkeypatch, layout):
    write_case(layout.eval_cases_directory, "passes", VALID_LABEL, image=b"crop-one")
    write_case(layout.eval_cases_directory, "partial", {**VALID_LABEL, "item_number": 7, "reason": "a"}, image=b"crop-two")
    return install_fake_api(
        monkeypatch,
        {
            b"crop-one": [continuation_page(items=[make_item(6, agreement="5", reason="d", confidence="5")])],
            b"crop-two": [continuation_page(items=[make_item(7, agreement="5", reason="ambiguous", confidence="5")])],
        },
    )


def test_eval_run_scores_writes_results_and_reuses_cache(layout, fake_eval_api, capsys):
    assert cli.main(["eval", "run", "--workers", "1"], layout) == eval_cli.EXIT_CASES_FAILED
    assert len(fake_eval_api.requests) == 2
    assert fake_eval_api.requests[0]["input"][0]["content"][0]["text"] == EVAL_USER_INSTRUCTION
    output = capsys.readouterr().out
    assert "Cases passed:     1/2 (50.0%)" in output
    assert "Overall accuracy: 5/6 values (83.3%)" in output
    assert "B=a 0/1* (got ambiguous x1)" in output
    assert "Unstable values:  0" in output
    assert "no previous run to compare against" in output

    results = sorted(layout.eval_results_directory.glob("*.json"))
    assert len(results) == 1
    assert results[0].name.endswith(f"_{PROMPT_VERSION}.json")
    data = json.loads(results[0].read_text())
    assert {case["case_id"]: case["status"] for case in data["cases"]} == {"passes": "pass", "partial": "fail"}
    assert data["total_cost_usd"] == pytest.approx(0.016)
    assert data["summary"]["per_dimension"]["reason"] == {"correct": 1, "total": 2, "fraction": 0.5}

    (layout.eval_cases_directory / "partial" / "label.json").write_text(json.dumps({**VALID_LABEL, "item_number": 7, "reason": "ambiguous"}))
    results[0].rename(results[0].with_name("20000101T000000Z_" + PROMPT_VERSION + ".json"))
    assert cli.main(["eval", "run"], layout) == eval_cli.EXIT_OK
    assert len(fake_eval_api.requests) == 2
    output = capsys.readouterr().out
    assert "0 to send" in output
    assert "Total cost:       $0.0000" in output
    assert "partial: fail -> pass" in output


def test_eval_run_respects_max_spend(layout, fake_eval_api, capsys):
    assert cli.main(["eval", "run", "--workers", "1", "--max-spend", "0.02"], layout) == eval_cli.EXIT_CASES_FAILED
    assert fake_eval_api.requests == []
    output = capsys.readouterr().out
    assert "Not run:          2 runs" in output
    assert "NOT_RUN" in output


def test_eval_run_sends_page_cases_with_the_extraction_instruction(layout, monkeypatch, capsys):
    write_case(layout.eval_cases_directory, "a-crop", VALID_LABEL, image=b"crop-one")
    write_case(layout.eval_cases_directory, "b-page", VALID_PAGE_LABEL, image=b"page-one", case_type="page")
    responses = install_fake_api(
        monkeypatch,
        {
            b"crop-one": [continuation_page(items=[make_item(6, agreement="5", reason="d", confidence="5")])],
            b"page-one": [correct_page(q8={"reason": "c,d"})],
        },
    )
    assert cli.main(["eval", "run", "--workers", "1"], layout) == eval_cli.EXIT_CASES_FAILED
    assert responses.instruction_for(b"crop-one") == {EVAL_USER_INSTRUCTION}
    assert responses.instruction_for(b"page-one") == {USER_INSTRUCTION}
    output = capsys.readouterr().out
    assert "Overall accuracy: 20/21 values (95.2%)" in output
    assert "Page fields:      2/2 (100.0%)" in output
    assert "B=c 0/1* (got c,d x1)" in output
    data = json.loads(next(layout.eval_results_directory.glob("*.json")).read_text())
    page_result = next(case for case in data["cases"] if case["case_id"] == "b-page")
    assert page_result["case_type"] == "page"
    assert len(page_result["values"]) == 20
    assert page_result["runs"][0]["actual"]["Q8_B"] == "c,d"


def test_repeat_makes_independent_calls_instead_of_cache_hits(layout, monkeypatch, capsys):
    write_case(layout.eval_cases_directory, "b-page", VALID_PAGE_LABEL, image=b"page-one", case_type="page")
    responses = install_fake_api(
        monkeypatch,
        {b"page-one": [correct_page(), correct_page(q8={"reason": "c,d"}), correct_page(), correct_page(), correct_page()]},
    )
    assert cli.main(["eval", "run", "--workers", "1", "--repeat", "5", "--max-spend", "5"], layout) == eval_cli.EXIT_CASES_FAILED
    assert len(responses.requests) == 5
    output = capsys.readouterr().out
    assert "0 cached, 5 to send" in output
    assert "UNSTABLE  4/5 runs passed  (api 5)" in output
    assert "B=c 4/5* UNSTABLE (got c,d x1)" in output
    assert "Unstable values:  1" in output
    assert "Runs passed:      4/5 (80.0%)" in output
    data = json.loads(next(layout.eval_results_directory.glob("*.json")).read_text())
    assert data["api_calls"] == 5 and data["cache_hits"] == 0
    assert data["summary"]["unstable_values"] == 1
    assert data["cases"][0]["unstable_values"] == ["Q8_B"]
    assert [run["repeat_index"] for run in data["cases"][0]["runs"]] == [0, 1, 2, 3, 4]

    assert cli.main(["eval", "run", "--workers", "1", "--repeat", "5", "--max-spend", "5"], layout) == eval_cli.EXIT_CASES_FAILED
    assert len(responses.requests) == 5
    assert "5 cached, 0 to send" in capsys.readouterr().out

    assert cli.main(["eval", "run", "--workers", "1", "--repeat", "6", "--max-spend", "5"], layout) == eval_cli.EXIT_CASES_FAILED
    assert len(responses.requests) == 6
    assert "5 cached, 1 to send" in capsys.readouterr().out


def test_repeat_must_be_positive(layout, fake_eval_api, capsys):
    assert cli.main(["eval", "run", "--repeat", "0"], layout) == eval_cli.EXIT_USAGE_ERROR
    assert "--repeat must be 1 or more" in capsys.readouterr().err


def test_eval_add_refuses_pdfs_outside_data_in(layout, tmp_path, capsys):
    outside = Path(__file__).parent / "fixtures" / "synthetic_two_page.pdf"
    assert cli.main(["eval", "add", "--file", str(outside), "--page", "1", "--item", "1", "--case-id", "x"], layout) == eval_cli.EXIT_USAGE_ERROR
    assert "is not inside" in capsys.readouterr().err
    assert cli.main(["eval", "add-page", "--file", str(outside), "--page", "1", "--case-id", "x"], layout) == eval_cli.EXIT_USAGE_ERROR
    assert "is not inside" in capsys.readouterr().err
    assert not layout.eval_cases_directory.exists()


def test_eval_add_writes_nothing_when_detection_fails(layout, capsys):
    pdf = layout.input_root / "020421" / "020421_PD1_RASSI.pdf"
    pdf.parent.mkdir(parents=True)
    pdf.write_bytes((Path(__file__).parent / "fixtures" / "synthetic_two_page.pdf").read_bytes())
    assert cli.main(["eval", "add", "--file", str(pdf), "--page", "1", "--item", "1", "--case-id", "x"], layout) == eval_cli.EXIT_USAGE_ERROR
    assert "found 0 item tables" in capsys.readouterr().err
    assert not (layout.eval_cases_directory / "x").exists()


@pytest.fixture
def synthetic_pdf(layout) -> Path:
    pdf = layout.input_root / "020421" / "020421_PD1_RASSI.pdf"
    pdf.parent.mkdir(parents=True)
    document = pymupdf.open()
    draw_form_page(document.new_page(width=PAGE_WIDTH, height=PAGE_HEIGHT), 1, header=True)
    draw_form_page(document.new_page(width=PAGE_WIDTH, height=PAGE_HEIGHT), 7, header=False)
    document.save(pdf)
    document.close()
    return pdf


def test_eval_add_writes_crop_empty_label_and_case_type(layout, synthetic_pdf, capsys):
    arguments = ["eval", "add", "--file", str(synthetic_pdf), "--page", "2", "--item", "9", "--case-id", "new-case"]
    assert cli.main(arguments, layout) == eval_cli.EXIT_OK
    directory = layout.eval_cases_directory / "new-case"
    assert (directory / "crop.png").read_bytes().startswith(b"\x89PNG")
    assert json.loads((directory / "label.json").read_text()) == {
        "item_number": 9,
        "agreement": None,
        "agreement_mark_confidence": None,
        "reason": None,
        "reason_mark_confidence": None,
        "confidence": None,
        "confidence_mark_confidence": None,
    }
    assert json.loads((directory / "case.json").read_text()) == {"case_type": "crop", "source_pdf": "020421_PD1_RASSI.pdf", "page_number": 2}
    assert load_library(layout.eval_cases_directory).unlabeled == ["new-case"]
    assert cli.main(arguments, layout) == eval_cli.EXIT_USAGE_ERROR
    assert "already exists" in capsys.readouterr().err


@pytest.mark.parametrize("page_number,item_numbers", [(1, [1, 2, 3, 4, 5, 6]), (2, [7, 8, 9, 10, 11, 12])])
def test_eval_add_page_writes_page_and_six_item_skeleton(layout, synthetic_pdf, page_number, item_numbers):
    arguments = ["eval", "add-page", "--file", str(synthetic_pdf), "--page", str(page_number), "--case-id", "new-page"]
    assert cli.main(arguments, layout) == eval_cli.EXIT_OK
    directory = layout.eval_cases_directory / "new-page"
    image = (directory / "page.png").read_bytes()
    assert image.startswith(b"\x89PNG")
    skeleton = json.loads((directory / "label.json").read_text())
    assert skeleton["page_type"] is None and skeleton["sid"] is None
    assert [item["item_number"] for item in skeleton["items"]] == item_numbers
    assert all(item_values is None for item in skeleton["items"] for key, item_values in item.items() if key != "item_number")
    assert json.loads((directory / "case.json").read_text())["case_type"] == "page"
    assert load_library(layout.eval_cases_directory).unlabeled == ["new-page"]


def test_eval_add_page_rejects_a_page_outside_the_pdf(layout, synthetic_pdf, capsys):
    arguments = ["eval", "add-page", "--file", str(synthetic_pdf), "--page", "3", "--case-id", "new-page"]
    assert cli.main(arguments, layout) == eval_cli.EXIT_USAGE_ERROR
    assert "has 2 pages" in capsys.readouterr().err
    assert not (layout.eval_cases_directory / "new-page").exists()


def test_label_mark_confidence_is_optional_and_scored_only_when_given():
    assert set(crop_case().expected) == {ValueKey(6, "agreement"), ValueKey(6, "reason"), ValueKey(6, "confidence")}
    with_marks = crop_case(label(reason="c,d", reason_mark_confidence="unsure", agreement_mark_confidence="clear"))
    assert with_marks.expected[ValueKey(6, "reason_mark_confidence")] == "unsure"
    assert with_marks.expected[ValueKey(6, "agreement_mark_confidence")] == "clear"
    assert ValueKey(6, "confidence_mark_confidence") not in with_marks.expected
    assert ValueKey(6, "reason_mark_confidence").label == "Q6_B_mark"


def test_label_rejects_an_unknown_mark_confidence():
    with pytest.raises(CaseError, match="reason_mark_confidence"):
        parse_label({**VALID_LABEL, "reason_mark_confidence": "maybe"}, "label.json")


def test_case_can_pass_on_values_and_fail_on_mark_confidence():
    case = crop_case(label(reason="c,d", reason_mark_confidence="unsure"))
    separate = continuation_page(items=[make_item(6, agreement="5", reason="c,d", confidence="5", reason_mark_confidence="clear")])
    result = result_for(case, [separate])
    score = result.runs[0].score
    assert score.matches[ValueKey(6, "reason")] is True
    assert score.matches[ValueKey(6, "reason_mark_confidence")] is False
    assert result.status == "fail"
    assert result.values_status == "pass"
    assert result.mark_confidence_status == "fail"
    summary = summarize([result])
    assert (summary.overall.correct, summary.overall.total) == (3, 3)
    assert (summary.per_dimension["reason"].correct, summary.per_dimension["reason"].total) == (1, 1)
    assert (summary.mark_confidence.correct, summary.mark_confidence.total) == (0, 1)


def test_mark_confidence_right_and_values_wrong_are_reported_apart():
    case = crop_case(label(reason="c,d", reason_mark_confidence="clear"))
    result = result_for(case, [continuation_page(items=[make_item(6, agreement="5", reason="c", confidence="5")])])
    assert (result.values_status, result.mark_confidence_status, result.status) == ("fail", "pass", "fail")


def test_matching_values_and_mark_confidence_pass_and_unstable_mark_confidence_is_counted():
    case = crop_case(label(reason="c,d", reason_mark_confidence="clear"))
    right = continuation_page(items=[make_item(6, agreement="5", reason="c,d", confidence="5")])
    wrong = continuation_page(items=[make_item(6, agreement="5", reason="c,d", confidence="5", reason_mark_confidence="unsure")])
    assert result_for(case, [right]).status == "pass"
    mixed = result_for(case, [right, wrong])
    assert (mixed.values_status, mixed.mark_confidence_status, mixed.status) == ("pass", "unstable", "unstable")
    assert mixed.unstable_keys == [ValueKey(6, "reason_mark_confidence")]


def test_case_without_mark_confidence_labels_has_no_mark_confidence_status():
    result = result_for(crop_case(), [continuation_page(items=[make_item(6, agreement="5", reason="d", confidence="5")])])
    assert result.mark_confidence_status is None
    assert result.values_status == "pass"
    assert summarize([result]).mark_confidence.total == 0
