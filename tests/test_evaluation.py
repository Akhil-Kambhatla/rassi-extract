import base64
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from rassi import cli, eval_cli
from rassi.cache import cache_key
from rassi.evaluation import (
    CaseError,
    CaseLabel,
    StatusChange,
    eval_cache_key,
    load_case,
    load_library,
    parse_label,
    score_case,
    select_item,
    status_changes,
    summarize,
)
from rassi.paths import DataLayout
from rassi.prompt import EVAL_USER_INSTRUCTION, PROMPT_VERSION
from rassi.schema import SCHEMA_VERSION
import pymupdf

from tests.fakes import continuation_page, header_page, make_item
from tests.test_crop import PAGE_HEIGHT, PAGE_WIDTH, draw_form_page

VALID_LABEL = {"item_number": 6, "agreement": "5", "reason": "d", "confidence": "5"}


def label(**overrides) -> CaseLabel:
    return CaseLabel.model_validate({**VALID_LABEL, **overrides})


def write_case(root: Path, case_id: str, label_data, crop: bytes = b"crop-bytes", notes: str | None = "why it is hard") -> Path:
    directory = root / case_id
    directory.mkdir(parents=True)
    (directory / "crop.png").write_bytes(crop)
    (directory / "label.json").write_text(label_data if isinstance(label_data, str) else json.dumps(label_data))
    if notes is not None:
        (directory / "notes.md").write_text(notes + "\n")
    return directory


def test_valid_label_loads_with_notes_and_crop(tmp_path):
    case = load_case(write_case(tmp_path, "span-into-neighbour", VALID_LABEL))
    assert case.case_id == "span-into-neighbour"
    assert case.label == label()
    assert case.crop_png == b"crop-bytes"
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
    for value in ("blank", "ambiguous", "multiple"):
        assert parse_label({**VALID_LABEL, "agreement": value, "reason": value, "confidence": value}, "label.json").reason == value


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


def test_full_match_passes():
    score = score_case("c", label(), make_item(6, agreement="5", reason="d", confidence="5"))
    assert score.status == "pass"
    assert score.matches == {"agreement": True, "reason": True, "confidence": True}


def test_partial_match_fails_but_credits_matching_dimensions():
    score = score_case("c", label(), make_item(6, agreement="ambiguous", reason="d", confidence="5"))
    assert score.status == "fail"
    assert score.matches == {"agreement": False, "reason": True, "confidence": True}
    summary = summarize([score])
    assert summary.cases_passed.correct == 0
    assert summary.overall.correct == 2
    assert summary.overall.total == 3
    assert summary.per_dimension["agreement"].fraction == 0
    assert summary.per_dimension["reason"].fraction == 1


def test_wrong_item_number_fails_even_when_values_match():
    score = score_case("c", label(), make_item(5, agreement="5", reason="d", confidence="5"))
    assert score.status == "fail"
    assert all(score.matches.values())


def test_summary_across_cases_and_not_run_cases():
    scores = [
        score_case("a", label(), make_item(6, agreement="5", reason="d", confidence="5")),
        score_case("b", label(), make_item(6, agreement="6", reason="multiple", confidence="5")),
        score_case("c", label(), None, "api call failed: boom"),
        score_case("d", label(), None, "not run: --max-spend reached"),
    ]
    assert [score.status for score in scores] == ["pass", "fail", "fail", "not_run"]
    summary = summarize(scores)
    assert summary.not_run == 1
    assert (summary.cases_passed.correct, summary.cases_passed.total) == (1, 3)
    assert (summary.overall.correct, summary.overall.total) == (4, 9)
    assert summary.per_dimension["confidence"].correct == 2


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
    scores = [
        score_case("stays", label(), make_item(6, agreement="5", reason="d", confidence="5")),
        score_case("fixed", label(), make_item(6, agreement="5", reason="d", confidence="5")),
        score_case("added", label(), None, "api call failed: x"),
    ]
    before = {"stays": "pass", "fixed": "fail", "dropped": "pass"}
    assert status_changes(before, scores) == [
        StatusChange("added", "new", "fail"),
        StatusChange("dropped", "pass", "removed"),
        StatusChange("fixed", "fail", "pass"),
    ]
    assert status_changes(None, scores) == []


def test_cache_key_includes_prompt_version_model_and_crop():
    base = eval_cache_key(b"crop", "gpt-5.6-terra")
    assert base == cache_key(b"crop", "gpt-5.6-terra", PROMPT_VERSION, SCHEMA_VERSION)
    assert base == eval_cache_key(b"crop", "gpt-5.6-terra", PROMPT_VERSION)
    assert base != eval_cache_key(b"crop", "gpt-5.6-terra", PROMPT_VERSION + "-next")
    assert base != eval_cache_key(b"crop", "gpt-5.6-luna")
    assert base != eval_cache_key(b"other crop", "gpt-5.6-terra")


class FakeResponses:
    def __init__(self, items_by_crop: dict[bytes, object]):
        self.items_by_crop = items_by_crop
        self.requests = []

    def create(self, **request):
        self.requests.append(request)
        crop = base64.b64decode(request["input"][0]["content"][1]["image_url"].split(",", 1)[1])
        page = continuation_page(items=[self.items_by_crop[crop]])
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


@pytest.fixture
def layout(tmp_path) -> DataLayout:
    return DataLayout(input_root=tmp_path / "in", output_root=tmp_path / "out", cache_root=tmp_path / "cache", eval_root=tmp_path / "eval")


@pytest.fixture
def fake_eval_api(monkeypatch, layout):
    write_case(layout.eval_cases_directory, "passes", VALID_LABEL, crop=b"crop-one")
    write_case(layout.eval_cases_directory, "partial", {**VALID_LABEL, "item_number": 7, "reason": "a"}, crop=b"crop-two")
    responses = FakeResponses(
        {
            b"crop-one": make_item(6, agreement="5", reason="d", confidence="5"),
            b"crop-two": make_item(7, agreement="5", reason="ambiguous", confidence="5"),
        }
    )
    monkeypatch.setattr(eval_cli, "make_client", lambda: SimpleNamespace(responses=responses))
    monkeypatch.setattr(eval_cli, "load_dotenv", lambda path: None)
    monkeypatch.setenv("OPENAI_API_KEY", "test-key-not-real")
    return responses


def test_eval_run_scores_writes_results_and_reuses_cache(layout, fake_eval_api, capsys):
    assert cli.main(["eval", "run", "--workers", "1"], layout) == eval_cli.EXIT_CASES_FAILED
    assert len(fake_eval_api.requests) == 2
    assert fake_eval_api.requests[0]["input"][0]["content"][0]["text"] == EVAL_USER_INSTRUCTION
    output = capsys.readouterr().out
    assert "Cases passed:     1/2 (50.0%)" in output
    assert "Overall accuracy: 5/6 values (83.3%)" in output
    assert "B=ambiguous*" in output
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
    assert "Not run:          2" in output
    assert "NOT_RUN" in output


def test_eval_add_refuses_pdfs_outside_data_in(layout, tmp_path, capsys):
    outside = Path(__file__).parent / "fixtures" / "synthetic_two_page.pdf"
    assert cli.main(["eval", "add", "--file", str(outside), "--page", "1", "--item", "1", "--case-id", "x"], layout) == eval_cli.EXIT_USAGE_ERROR
    assert "is not inside" in capsys.readouterr().err
    assert not layout.eval_cases_directory.exists()


def test_eval_add_writes_nothing_when_detection_fails(layout, capsys):
    pdf = layout.input_root / "020421" / "020421_PD1_RASSI.pdf"
    pdf.parent.mkdir(parents=True)
    pdf.write_bytes((Path(__file__).parent / "fixtures" / "synthetic_two_page.pdf").read_bytes())
    assert cli.main(["eval", "add", "--file", str(pdf), "--page", "1", "--item", "1", "--case-id", "x"], layout) == eval_cli.EXIT_USAGE_ERROR
    assert "found 0 item tables" in capsys.readouterr().err
    assert not (layout.eval_cases_directory / "x").exists()


def test_eval_add_writes_crop_and_empty_label(layout, capsys):
    pdf = layout.input_root / "020421" / "020421_PD1_RASSI.pdf"
    pdf.parent.mkdir(parents=True)
    document = pymupdf.open()
    draw_form_page(document.new_page(width=PAGE_WIDTH, height=PAGE_HEIGHT), 1, header=True)
    draw_form_page(document.new_page(width=PAGE_WIDTH, height=PAGE_HEIGHT), 7, header=False)
    document.save(pdf)
    document.close()
    arguments = ["eval", "add", "--file", str(pdf), "--page", "2", "--item", "9", "--case-id", "new-case"]
    assert cli.main(arguments, layout) == eval_cli.EXIT_OK
    directory = layout.eval_cases_directory / "new-case"
    assert (directory / "crop.png").read_bytes().startswith(b"\x89PNG")
    assert json.loads((directory / "label.json").read_text()) == {"item_number": 9, "agreement": None, "reason": None, "confidence": None}
    assert load_library(layout.eval_cases_directory).unlabeled == ["new-case"]
    assert cli.main(arguments, layout) == eval_cli.EXIT_USAGE_ERROR
    assert "already exists" in capsys.readouterr().err
