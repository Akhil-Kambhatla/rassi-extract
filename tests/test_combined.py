import csv

from rassi.combined import build_combined_outputs
from rassi.filename import parse_source_filename
from rassi.outputs import build_review_text, build_wide_rows, wide_columns, write_csv, write_text
from rassi.paths import DataLayout, output_paths
from rassi.structure import pair_students
from tests.fakes import continuation_page, header_page, make_item, numbered


def write_file_outputs(layout: DataLayout, filename: str, pages) -> None:
    source = parse_source_filename(filename)
    paths = output_paths(source, layout)
    students = pair_students(pages)
    write_csv(paths.wide_csv, wide_columns(), build_wide_rows(students, source))
    write_text(paths.review_txt, build_review_text(students, source.source_pdf, source.teacher_code, source.period, "PRE"))


def read_rows(path):
    with path.open(newline="") as handle:
        return list(csv.DictReader(handle))


def make_layout(tmp_path) -> DataLayout:
    return DataLayout(input_root=tmp_path / "in", output_root=tmp_path / "out", cache_root=tmp_path / "cache")


def test_combined_csv_and_review_cover_every_file(tmp_path):
    layout = make_layout(tmp_path)
    unsure_items = [make_item(number) for number in range(1, 7)]
    unsure_items[4] = make_item(5, reason="c,d", reason_notes="one circle around c and d", reason_mark_confidence="unsure")
    write_file_outputs(layout, "020421_PD10_RASSI.pdf", numbered([header_page(sid="0204211001"), continuation_page()]))
    write_file_outputs(layout, "020421_PD4_RASSI.pdf", numbered([header_page(sid="0204210407", items=unsure_items), continuation_page()]))
    write_file_outputs(layout, "010401_PD2_RASSI.pdf", numbered([header_page(sid="0104010201"), continuation_page()] * 2))

    result = build_combined_outputs(layout)

    assert result.student_count == 4
    assert result.skipped_old_format == []
    rows = read_rows(layout.combined_students_csv)
    assert list(rows[0]) == wide_columns()
    assert [(row["teacher_code"], row["period"], row["source_pdf"], row["sid"]) for row in rows] == [
        ("010401", "2", "010401_PD2_RASSI.pdf", "0104010201"),
        ("010401", "2", "010401_PD2_RASSI.pdf", "0104010201"),
        ("020421", "4", "020421_PD4_RASSI.pdf", "0204210407"),
        ("020421", "10", "020421_PD10_RASSI.pdf", "0204211001"),
    ]
    flagged = rows[2]
    assert (flagged["Q5_B"], flagged["n_multiple"], flagged["n_uncertain"], flagged["n_blank"], flagged["check"], flagged["pre_post"]) == (
        "c,d?",
        "1",
        "1",
        "0",
        "check",
        "PRE",
    )
    assert rows[3]["check"] == ""

    review = layout.combined_review_txt.read_text()
    headings = [line for line in review.splitlines() if line.startswith("=====")]
    assert headings == [
        "===== 010401_PD2_RASSI.pdf =====",
        "===== 020421_PD4_RASSI.pdf =====",
        "===== 020421_PD10_RASSI.pdf =====",
    ]
    assert "Item 5, Reason: c,d page 1 one circle around c and d" in review
    assert review.index("===== 020421_PD4_RASSI.pdf =====") < review.index("Review list for 020421_PD4_RASSI.pdf")


def test_combined_csv_carries_n_multiple_for_a_confident_double_entry(tmp_path):
    layout = make_layout(tmp_path)
    items = [make_item(number) for number in range(1, 7)]
    items[1] = make_item(2, agreement="2,5", agreement_notes="two separate circles, on 2 and on 5")
    write_file_outputs(layout, "020421_PD4_RASSI.pdf", numbered([header_page(sid="0204210403", items=items), continuation_page()]))

    build_combined_outputs(layout)

    row = read_rows(layout.combined_students_csv)[0]
    assert list(row)[-4:] == ["n_multiple", "n_uncertain", "n_blank", "check"]
    assert (row["Q2_A"], row["n_multiple"], row["n_uncertain"], row["check"]) == ("2,5", "1", "0", "check")
    review = layout.combined_review_txt.read_text()
    assert review.index("Double entries (1)") < review.index("Uncertain values (0)") < review.index("Blanks (0)")
    assert "Item 2, Agreement: 2,5 page 1 two separate circles, on 2 and on 5" in review


def test_combined_leaves_out_wide_csvs_from_an_older_version(tmp_path):
    layout = make_layout(tmp_path)
    write_file_outputs(layout, "020421_PD1_RASSI.pdf", numbered([header_page(sid="0204210101"), continuation_page()]))
    old_paths = output_paths(parse_source_filename("020421_PD4_RASSI.pdf"), layout)
    write_csv(old_paths.wide_csv, wide_columns()[:-4], [])

    result = build_combined_outputs(layout)

    assert result.skipped_old_format == [old_paths.wide_csv]
    assert [row["source_pdf"] for row in read_rows(layout.combined_students_csv)] == ["020421_PD1_RASSI.pdf"]


def test_combined_with_no_processed_files_writes_headers_only(tmp_path):
    layout = make_layout(tmp_path)
    result = build_combined_outputs(layout)
    assert result.student_count == 0
    assert layout.combined_students_csv.read_text().strip() == ",".join(wide_columns())
    assert layout.combined_review_txt.read_text() == "No processed files yet.\n"


def test_combined_reads_only_the_per_file_student_csv(tmp_path):
    layout = make_layout(tmp_path)
    write_file_outputs(layout, "020421_PD1_RASSI.pdf", numbered([header_page(sid="0204210101"), continuation_page()]))
    paths = output_paths(parse_source_filename("020421_PD1_RASSI.pdf"), layout)
    columns = wide_columns()
    stray_row = {column: "" for column in columns} | {"source_pdf": "stray.pdf"}
    write_csv(paths.flags_csv, columns, [stray_row])
    for name in ("020421_PD1_RASSI_wide.csv", "notes.csv", "020421_PD2_RASSI.csv", "999999_PD1_RASSI.csv"):
        write_csv(paths.directory / name, columns, [stray_row])

    result = build_combined_outputs(layout)

    assert result.included == [paths.wide_csv]
    assert result.skipped_old_format == []
    assert [row["source_pdf"] for row in read_rows(layout.combined_students_csv)] == ["020421_PD1_RASSI.pdf"]
