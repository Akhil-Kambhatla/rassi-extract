import base64
import csv
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from rassi import cli
from rassi.paths import DataLayout, output_paths
from rassi.filename import parse_source_filename
from rassi.render import RenderedPage
from tests.fakes import continuation_page, header_page

PAGES_PER_FILE = 4


def default_page_json(page_number: int) -> str:
    page = header_page(sid=f"10{page_number}") if page_number % 2 == 1 else continuation_page()
    return page.model_dump_json()


class FakeResponses:
    def __init__(self):
        self.calls = []
        self.output_overrides: dict[str, str] = {}

    def create(self, **request):
        image_url = request["input"][0]["content"][1]["image_url"]
        label = base64.b64decode(image_url.split(",", 1)[1]).decode("utf-8")
        self.calls.append(label)
        page_number = int(label.rsplit("|", 1)[1])
        output_text = self.output_overrides.get(label, default_page_json(page_number))
        return SimpleNamespace(
            id=f"response-{label}",
            status="completed",
            incomplete_details=None,
            output_text=output_text,
            usage=SimpleNamespace(
                input_tokens=1000,
                output_tokens=500,
                output_tokens_details=SimpleNamespace(reasoning_tokens=0),
                input_tokens_details=SimpleNamespace(cached_tokens=0),
            ),
        )


def fake_render(pdf_path: Path, page_numbers: list[int], dpi: int = 300) -> list[RenderedPage]:
    return [
        RenderedPage(page_number=number, png_bytes=f"{pdf_path.name}|{number}".encode("utf-8"), width_px=10, height_px=10)
        for number in page_numbers
    ]


@pytest.fixture
def layout(tmp_path) -> DataLayout:
    return DataLayout(input_root=tmp_path / "in", output_root=tmp_path / "out", cache_root=tmp_path / "cache")


@pytest.fixture
def fake_api(monkeypatch) -> FakeResponses:
    responses = FakeResponses()
    monkeypatch.setattr(cli, "count_pages", lambda pdf_path: PAGES_PER_FILE)
    monkeypatch.setattr(cli, "render_pages", fake_render)
    monkeypatch.setattr(cli, "make_client", lambda: SimpleNamespace(responses=responses))
    monkeypatch.setattr(cli, "load_dotenv", lambda path: None)
    monkeypatch.setenv("OPENAI_API_KEY", "test-key-not-real")
    return responses


def place(layout: DataLayout, relative: str) -> Path:
    path = layout.input_root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"placeholder, never opened")
    return path


def paths_for(layout: DataLayout, name: str):
    return output_paths(parse_source_filename(name), layout)


def test_single_file_writes_every_output_named_from_the_stem(layout, fake_api):
    pdf = place(layout, "020421/020421_PD1_RASSI.pdf")
    assert cli.main(["extract", "--file", str(pdf), "--workers", "1"], layout) == cli.EXIT_OK
    paths = paths_for(layout, "020421_PD1_RASSI.pdf")
    for path in paths.fixed_files():
        assert path.is_file(), path
    assert [path.name for path in paths.existing_raw_pages()] == [f"020421_PD1_RASSI_page0{number}.json" for number in range(1, 5)]
    assert sorted(path.name for path in layout.output_root.iterdir()) == ["020421"]
    manifest = json.loads(paths.manifest_json.read_text())
    assert manifest["status"] == "complete"
    assert manifest["pre_post"] == "PRE"
    with paths.metrics_csv.open(newline="") as handle:
        metrics = list(csv.DictReader(handle))
    assert [row["page_number"] for row in metrics] == ["1", "2", "3", "4"]
    assert [row["page_type"] for row in metrics] == ["header", "continuation", "header", "continuation"]
    assert [row["sid"] for row in metrics] == ["101", "", "103", ""]
    assert all(row["cache_hit"] == "False" and row["retry_count"] == "0" and row["status"] == "ok" for row in metrics)
    assert all(row["input_tokens"] == "1000" and row["cost_usd"] == "0.008" for row in metrics)
    assert all(row["latency_seconds"] != "" for row in metrics)
    timing = manifest["timing_and_cost"]
    assert timing["pages_called"] == 4
    assert timing["pages_cached"] == 0
    assert timing["cost_per_page_usd"] == pytest.approx(0.008)
    assert timing["cost_per_student_usd"] == pytest.approx(0.016)
    assert timing["projected_cost_50_page_pdf_usd"] == pytest.approx(0.4)
    assert timing["projected_cost_100_pdfs_usd"] == pytest.approx(40.0)
    assert manifest["workers"] == 1


def test_overwrite_guard_skips_existing_outputs(layout, fake_api, capsys):
    pdf = place(layout, "020421/020421_PD1_RASSI.pdf")
    assert cli.main(["extract", "--file", str(pdf), "--workers", "1"], layout) == cli.EXIT_OK
    paths = paths_for(layout, "020421_PD1_RASSI.pdf")
    paths.wide_csv.write_text("sentinel")
    capsys.readouterr()

    assert cli.main(["extract", "--file", str(pdf), "--workers", "1"], layout) == cli.EXIT_OK
    output = capsys.readouterr().out
    assert "outputs already exist" in output
    assert "--overwrite" in output
    assert "020421_PD1_RASSI_wide.csv" in output
    assert paths.wide_csv.read_text() == "sentinel"
    assert len(fake_api.calls) == PAGES_PER_FILE


def test_overwrite_flag_replaces_outputs_and_clears_stale_raw_pages(layout, fake_api):
    pdf = place(layout, "020421/020421_PD1_RASSI.pdf")
    assert cli.main(["extract", "--file", str(pdf), "--workers", "1"], layout) == cli.EXIT_OK
    paths = paths_for(layout, "020421_PD1_RASSI.pdf")
    paths.wide_csv.write_text("sentinel")

    assert cli.main(["extract", "--file", str(pdf), "--pages", "1-2", "--workers", "1", "--overwrite"], layout) == cli.EXIT_OK
    assert paths.wide_csv.read_text() != "sentinel"
    assert [path.name for path in paths.existing_raw_pages()] == ["020421_PD1_RASSI_page01.json", "020421_PD1_RASSI_page02.json"]
    assert len(fake_api.calls) == PAGES_PER_FILE


def test_all_teachers_prints_per_file_and_combined_totals(layout, fake_api, capsys):
    place(layout, "020421/020421_PD1_RASSI.pdf")
    place(layout, "115500/115500_PD2_RASSI.pdf")
    assert cli.main(["extract", "--workers", "1"], layout) == cli.EXIT_OK
    output = capsys.readouterr().out
    assert output.count("\nSUMMARY\n") == 2
    assert "RUN SUMMARY (2 files)" in output
    total_line = next(line for line in output.splitlines() if line.strip().startswith("TOTAL"))
    assert "$0.0640" in total_line
    assert paths_for(layout, "020421_PD1_RASSI.pdf").wide_csv.is_file()
    assert paths_for(layout, "115500_PD2_RASSI.pdf").wide_csv.is_file()


def test_teacher_option_only_processes_that_folder(layout, fake_api):
    place(layout, "020421/020421_PD1_RASSI.pdf")
    place(layout, "115500/115500_PD2_RASSI.pdf")
    assert cli.main(["extract", "--teacher", "115500", "--workers", "1"], layout) == cli.EXIT_OK
    assert not paths_for(layout, "020421_PD1_RASSI.pdf").directory.exists()
    assert paths_for(layout, "115500_PD2_RASSI.pdf").wide_csv.is_file()


def test_max_spend_applies_to_the_whole_invocation(layout, fake_api, capsys):
    place(layout, "020421/020421_PD1_RASSI.pdf")
    place(layout, "020421/020421_PD2_RASSI.pdf")
    place(layout, "020421/020421_PD3_RASSI.pdf")
    exit_code = cli.main(["extract", "--workers", "1", "--max-spend", "0.05"], layout)
    assert exit_code == cli.EXIT_INCOMPLETE
    output = capsys.readouterr().out
    assert len(fake_api.calls) == 6
    assert json.loads(paths_for(layout, "020421_PD1_RASSI.pdf").manifest_json.read_text())["status"] == "complete"
    second = json.loads(paths_for(layout, "020421_PD2_RASSI.pdf").manifest_json.read_text())
    assert second["status"] == "incomplete"
    assert second["pages_skipped_for_spend"] == [3, 4]
    assert not paths_for(layout, "020421_PD3_RASSI.pdf").directory.joinpath("020421_PD3_RASSI_manifest.json").exists()
    assert "NOT STARTED" in output


def test_bad_inputs_are_skipped_and_valid_files_still_run(layout, fake_api, capsys):
    place(layout, "020421/020421_PD1_RASSI.pdf")
    place(layout, "020421/999999_PD1_RASSI.pdf")
    place(layout, "020421/scan.pdf")
    (layout.input_root / "115500").mkdir()
    assert cli.main(["extract", "--workers", "1"], layout) == cli.EXIT_USAGE_ERROR
    errors = capsys.readouterr().err
    assert "999999_PD1_RASSI.pdf" in errors
    assert "scan.pdf" in errors
    assert "115500" in errors
    assert paths_for(layout, "020421_PD1_RASSI.pdf").wide_csv.is_file()


def test_missing_input_folder_is_a_message_not_a_traceback(layout, fake_api, capsys):
    assert cli.main(["extract"], layout) == cli.EXIT_USAGE_ERROR
    assert "does not exist" in capsys.readouterr().err
    assert fake_api.calls == []


def test_collision_stops_before_any_work(layout, fake_api, capsys, monkeypatch):
    place(layout, "020421/020421_PD1_RASSI.pdf")
    monkeypatch.setattr(cli, "output_collisions", lambda files, layout: ["a.pdf and b.pdf would both write outputs named x"])
    assert cli.main(["extract"], layout) == cli.EXIT_USAGE_ERROR
    assert "would both write outputs" in capsys.readouterr().err
    assert fake_api.calls == []
    assert not layout.output_root.exists()


def test_target_options_are_mutually_exclusive(layout):
    with pytest.raises(SystemExit):
        cli.main(["extract", "--teacher", "020421", "--file", "x.pdf"], layout)


def read_rows(path: Path) -> list[dict]:
    with path.open(newline="") as handle:
        return list(csv.DictReader(handle))


def test_cached_rerun_reports_zero_cost_and_excludes_latency(layout, fake_api, capsys):
    pdf = place(layout, "020421/020421_PD1_RASSI.pdf")
    assert cli.main(["extract", "--file", str(pdf), "--workers", "1"], layout) == cli.EXIT_OK
    assert cli.main(["extract", "--file", str(pdf), "--workers", "1", "--overwrite"], layout) == cli.EXIT_OK
    output = capsys.readouterr().out
    paths = paths_for(layout, "020421_PD1_RASSI.pdf")
    rows = read_rows(paths.metrics_csv)
    assert all(row["cache_hit"] == "True" and row["cost_usd"] == "0.0" and row["latency_seconds"] == "" for row in rows)
    timing = json.loads(paths.manifest_json.read_text())["timing_and_cost"]
    assert timing["pages_cached"] == 4
    assert timing["latency_mean_seconds"] is None
    assert timing["total_cost_usd"] == 0
    assert "no billed API pages" in timing["cost_basis"]
    assert "They differ because up to 1 API calls run concurrently" in output
    assert len(fake_api.calls) == PAGES_PER_FILE


def test_corrupt_pdf_is_recorded_and_the_run_continues(layout, fake_api, capsys, monkeypatch):
    place(layout, "020421/020421_PD1_RASSI.pdf")
    place(layout, "020421/020421_PD2_RASSI.pdf")

    def count_or_fail(pdf_path: Path) -> int:
        if pdf_path.name == "020421_PD1_RASSI.pdf":
            raise RuntimeError("Failed to open file: format error")
        return PAGES_PER_FILE

    monkeypatch.setattr(cli, "count_pages", count_or_fail)
    assert cli.main(["extract", "--workers", "1"], layout) == cli.EXIT_INCOMPLETE
    captured = capsys.readouterr()
    assert "020421_PD1_RASSI.pdf: could not read the PDF: RuntimeError: Failed to open file" in captured.err
    assert "failed_file" in captured.out
    assert "FAILED FILE:" in captured.out
    assert paths_for(layout, "020421_PD2_RASSI.pdf").wide_csv.is_file()


def test_truly_corrupt_pdf_bytes_are_caught(layout, fake_api, capsys, monkeypatch):
    from rassi.render import count_pages

    monkeypatch.setattr(cli, "count_pages", count_pages)
    pdf = place(layout, "020421/020421_PD1_RASSI.pdf")
    assert cli.main(["extract", "--file", str(pdf)], layout) == cli.EXIT_INCOMPLETE
    assert "could not read the PDF" in capsys.readouterr().err
    assert fake_api.calls == []


def test_file_outside_input_folder_is_an_error(layout, fake_api, capsys, tmp_path):
    outside = tmp_path / "elsewhere" / "020421" / "020421_PD1_RASSI.pdf"
    outside.parent.mkdir(parents=True)
    outside.write_bytes(b"placeholder")
    layout.input_root.mkdir(parents=True)
    assert cli.main(["extract", "--file", str(outside)], layout) == cli.EXIT_USAGE_ERROR
    assert "is not inside" in capsys.readouterr().err
    assert fake_api.calls == []


def test_pages_is_rejected_when_several_pdfs_match(layout, fake_api, capsys):
    place(layout, "020421/020421_PD1_RASSI.pdf")
    place(layout, "020421/020421_PD2_RASSI.pdf")
    assert cli.main(["extract", "--pages", "1-2"], layout) == cli.EXIT_USAGE_ERROR
    assert "matched 2 PDFs" in capsys.readouterr().err
    assert cli.main(["extract", "--teacher", "020421", "--pages", "1-2"], layout) == cli.EXIT_USAGE_ERROR
    assert fake_api.calls == []


def test_pages_is_allowed_when_one_pdf_matches(layout, fake_api):
    place(layout, "020421/020421_PD1_RASSI.pdf")
    (layout.input_root / "020415").mkdir()
    assert cli.main(["extract", "--teacher", "020421", "--pages", "1-2", "--workers", "1"], layout) == cli.EXIT_OK
    assert len(fake_api.calls) == 2


def test_seeded_empty_teacher_folders_do_not_fail_the_run(layout, fake_api, capsys):
    place(layout, "020421/020421_PD1_RASSI.pdf")
    for code in ["010401", "020415"]:
        (layout.input_root / code).mkdir()
        (layout.input_root / code / ".gitkeep").write_bytes(b"")
    assert cli.main(["extract", "--workers", "1"], layout) == cli.EXIT_OK
    assert "010401" in capsys.readouterr().err


def test_unknown_teacher_code_warns_and_processes(layout, fake_api, capsys):
    pdf = place(layout, "020412/020412_PD1_RASSI.pdf")
    assert cli.main(["extract", "--file", str(pdf), "--workers", "1"], layout) == cli.EXIT_OK
    assert "WARNING" in capsys.readouterr().err
    assert paths_for(layout, "020412_PD1_RASSI.pdf").wide_csv.is_file()


def invalid_response_cases():
    from tests.fakes import make_item

    five_items = header_page(sid="101", items=[make_item(number) for number in range(1, 6)]).model_dump_json()
    seven_items = header_page(sid="101", items=[make_item(number) for number in range(1, 8)]).model_dump_json()
    out_of_range = header_page(sid="101", items=[make_item(number) for number in (1, 2, 3, 4, 5, 13)]).model_dump_json()
    duplicated = header_page(sid="101", items=[make_item(number) for number in (1, 2, 3, 4, 5, 5)]).model_dump_json()
    not_json = '{"page_type": "header", "sid": "101"'
    bad_value = header_page(sid="101").model_dump_json().replace('"reason":"b"', '"reason":"e"', 1)
    return [
        ("five_items", five_items, "returned 5 items"),
        ("seven_items", seven_items, "returned 7 items"),
        ("out_of_range", out_of_range, "outside 1 to 12"),
        ("duplicated", duplicated, "appear more than once"),
        ("not_json", not_json, "did not match schema"),
        ("bad_value", bad_value, "did not match schema"),
    ]


@pytest.mark.parametrize("case, output_text, expected_reason", invalid_response_cases(), ids=[case[0] for case in invalid_response_cases()])
def test_invalid_page_response_fails_the_page_and_is_flagged(layout, fake_api, case, output_text, expected_reason):
    pdf = place(layout, "020421/020421_PD1_RASSI.pdf")
    fake_api.output_overrides["020421_PD1_RASSI.pdf|3"] = output_text
    assert cli.main(["extract", "--file", str(pdf), "--workers", "1"], layout) == cli.EXIT_INCOMPLETE
    paths = paths_for(layout, "020421_PD1_RASSI.pdf")
    assert not paths.wide_csv.exists()
    assert not paths.long_csv.exists()
    flags = read_rows(paths.flags_csv)
    assert [(row["flag_type"], row["page_number"]) for row in flags] == [("page_failed", "3")]
    assert expected_reason in flags[0]["reason"]
    metrics = {row["page_number"]: row for row in read_rows(paths.metrics_csv)}
    assert metrics["3"]["status"] == "failed"
    assert metrics["3"]["cost_usd"] == "0.008"
    assert expected_reason in metrics["3"]["error"]
    manifest = json.loads(paths.manifest_json.read_text())
    assert manifest["status"] == "incomplete"
    assert list(manifest["pages_failed"]) == ["3"]

    del fake_api.output_overrides["020421_PD1_RASSI.pdf|3"]
    assert cli.main(["extract", "--file", str(pdf), "--workers", "1", "--overwrite"], layout) == cli.EXIT_OK
    assert fake_api.calls.count("020421_PD1_RASSI.pdf|3") == 2
    assert fake_api.calls.count("020421_PD1_RASSI.pdf|1") == 1
