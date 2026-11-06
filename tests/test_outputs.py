import csv

import pytest
from pydantic import ValidationError

from rassi import config
from rassi.filename import parse_source_filename
from rassi.paths import DATA_DIR, DEFAULT_LAYOUT, DataLayout, output_paths
from rassi.outputs import (
    BLANKS_EXPLANATION,
    DOUBLE_ENTRIES_EXPLANATION,
    FLAG_COLUMNS,
    LONG_COLUMNS,
    UNCERTAIN_EXPLANATION,
    build_flag_rows,
    build_long_rows,
    build_review_text,
    build_wide_rows,
    flag_counts,
    numeric_value,
    remove_outputs,
    wide_columns,
    write_csv,
)
from rassi.schema import ItemExtraction
from rassi.structure import identity_flags, pair_students
from tests.fakes import continuation_page, header_page, make_item, numbered

SOURCE = parse_source_filename("020421_PD1_RASSI.pdf")


def two_students():
    first_header_items = [make_item(number, agreement=str(number), reason="abcd"[(number - 1) % 4], confidence="6") for number in range(1, 7)]
    first_header_items[2] = make_item(
        3,
        agreement="ambiguous",
        reason="blank",
        confidence="2,5",
        agreement_notes="circle spans 3 and 4",
        reason_notes="no mark",
        confidence_notes="circles on 2 and 5",
    )
    return numbered(
        [
            header_page(sid="111111", items=list(reversed(first_header_items))),
            continuation_page(),
            header_page(sid="REDACTED"),
            continuation_page(),
        ]
    )


def test_wide_columns_order():
    columns = wide_columns()
    assert columns[:7] == ["sid", "teacher_code", "period", "form", "pre_post", "source_pdf", "page_start"]
    assert columns[7:10] == ["Q1_A", "Q1_B", "Q1_C"]
    assert columns[-7:] == ["Q12_A", "Q12_B", "Q12_C", "n_multiple", "n_uncertain", "n_blank", "check"]
    assert len(columns) == 7 + 12 * 3 + 4


def test_wide_columns_have_no_reason_number_columns():
    assert not [column for column in wide_columns() if column.endswith("_num")]


def test_wide_rows_use_printed_item_numbers():
    rows = build_wide_rows(pair_students(two_students()), SOURCE)
    assert len(rows) == 2
    first = rows[0]
    assert first["sid"] == "111111"
    assert first["teacher_code"] == "020421"
    assert first["period"] == "1"
    assert first["form"] == "RASSI"
    assert first["pre_post"] == "PRE"
    assert first["source_pdf"] == "020421_PD1_RASSI.pdf"
    assert first["page_start"] == 1
    assert first["Q1_A"] == "1"
    assert first["Q2_B"] == "b"
    assert first["Q4_B"] == "d"
    assert first["Q3_A"] == "ambiguous?"
    assert first["Q3_B"] == "blank"
    assert first["Q3_C"] == "2,5"
    assert set(first) == set(wide_columns())
    assert rows[1]["page_start"] == 3
    assert rows[1]["sid"] == "REDACTED"


def test_long_rows_one_per_value():
    rows = build_long_rows(pair_students(two_students()), "gpt-5.6-terra")
    assert len(rows) == 2 * 12 * 3
    assert set(rows[0]) == set(LONG_COLUMNS)
    q3 = {row["dimension"]: row for row in rows if row["sid"] == "111111" and row["item"] == 3}
    assert q3["A"]["value"] == "ambiguous"
    assert q3["A"]["value_num"] == config.MISSING_VALUE_CODE
    assert q3["A"]["mark_notes"] == "circle spans 3 and 4"
    assert q3["B"]["mark_notes"] == "no mark"
    assert q3["C"]["mark_notes"] == "circles on 2 and 5"
    assert q3["A"]["page_number"] == 1
    q8 = {row["dimension"]: row for row in rows if row["sid"] == "111111" and row["item"] == 8}
    assert q3["C"]["value"] == "2,5"
    assert q3["C"]["value_num"] == config.MISSING_VALUE_CODE
    assert q8["A"]["value_num"] == 4
    assert q8["B"]["value"] == "b"
    assert q8["B"]["value_num"] == ""
    assert q8["C"]["value_num"] == 5
    assert all(row["value_num"] == "" for row in rows if row["dimension"] == "B")
    assert q8["A"]["page_number"] == 2
    assert all(row["model"] == "gpt-5.6-terra" for row in rows)


def test_flag_rows_include_values_and_identity_flags():
    students = pair_students(two_students())
    flags = identity_flags(students, "1", "PRE")
    rows = build_flag_rows(students, flags)
    counts = flag_counts(rows)
    assert counts == {"sid_redacted": 1, "value_ambiguous": 1, "value_blank": 1, "value_multiple": 1}
    ambiguous = next(row for row in rows if row["flag_type"] == "value_ambiguous")
    assert ambiguous["item"] == 3
    assert ambiguous["dimension"] == "A"
    assert ambiguous["page_number"] == 1
    assert ambiguous["reason"] == "circle spans 3 and 4"
    blank = next(row for row in rows if row["flag_type"] == "value_blank")
    assert blank["dimension"] == "B"
    assert blank["reason"] == "no mark"
    multiple = next(row for row in rows if row["flag_type"] == "value_multiple")
    assert multiple["dimension"] == "C"
    assert multiple["value"] == "2,5"
    assert multiple["reason"] == "circles on 2 and 5"
    assert all(set(row) == set(FLAG_COLUMNS) for row in rows)


def test_missing_value_code_is_configurable(monkeypatch):
    monkeypatch.setattr("rassi.outputs.MISSING_VALUE_CODE", "NA")
    assert numeric_value("A", "blank") == "NA"
    assert numeric_value("A", "1,4") == "NA"
    assert numeric_value("B", "a") == ""
    assert numeric_value("C", "6") == 6


def test_output_paths_are_named_from_the_pdf_stem(tmp_path):
    layout = DataLayout(input_root=tmp_path / "in", output_root=tmp_path / "out", cache_root=tmp_path / "cache")
    paths = output_paths(SOURCE, layout)
    directory = tmp_path / "out" / "020421" / "PD1"
    details = tmp_path / "details" / "020421" / "PD1"
    assert paths.directory == directory
    assert paths.details_directory == details
    assert paths.stem == "020421_PD1_RASSI"
    assert paths.wide_csv == directory / "020421_PD1_RASSI.csv"
    assert paths.flags_csv == directory / "020421_PD1_RASSI_flags.csv"
    assert paths.review_txt == directory / "020421_PD1_RASSI_review.txt"
    assert paths.long_csv == details / "020421_PD1_RASSI_long.csv"
    assert paths.metrics_csv == details / "020421_PD1_RASSI_metrics.csv"
    assert paths.manifest_json == details / "020421_PD1_RASSI_manifest.json"
    assert paths.raw_directory == details / "raw"
    assert paths.raw_page(3) == details / "raw" / "020421_PD1_RASSI_page03.json"


def test_default_layout_puts_details_beside_out():
    assert DEFAULT_LAYOUT.output_root == DATA_DIR / "out"
    assert DEFAULT_LAYOUT.details_root == DATA_DIR / "details"
    assert DEFAULT_LAYOUT.combined_students_csv == DATA_DIR / "out" / "_combined" / "all_students.csv"


@pytest.mark.parametrize(
    "relative",
    [
        "out/020421/PD1/020421_PD1_RASSI.csv",
        "out/020421/PD1/020421_PD1_RASSI_flags.csv",
        "out/020421/PD1/020421_PD1_RASSI_review.txt",
        "details/020421/PD1/020421_PD1_RASSI_long.csv",
        "details/020421/PD1/020421_PD1_RASSI_metrics.csv",
        "details/020421/PD1/020421_PD1_RASSI_manifest.json",
        "details/020421/PD1/raw/020421_PD1_RASSI_page01.json",
        "out/020421/PD1/020421_PD1_RASSI_wide.csv",
        "out/020421/PD1/020421_PD1_RASSI_long.csv",
        "out/020421/PD1/020421_PD1_RASSI_metrics.csv",
        "out/020421/PD1/020421_PD1_RASSI_manifest.json",
        "out/020421/PD1/raw/020421_PD1_RASSI_page01.json",
    ],
)
def test_existing_files_finds_outputs_in_either_tree_including_old_names(tmp_path, relative):
    layout = DataLayout(input_root=tmp_path / "in", output_root=tmp_path / "out", cache_root=tmp_path / "cache")
    paths = output_paths(SOURCE, layout)
    path = tmp_path / relative
    path.parent.mkdir(parents=True)
    path.write_text("x")
    assert paths.existing_files() == [path]


def test_remove_outputs_clears_both_trees_and_old_layout_files(tmp_path):
    layout = DataLayout(input_root=tmp_path / "in", output_root=tmp_path / "out", cache_root=tmp_path / "cache")
    paths = output_paths(SOURCE, layout)
    planted = paths.fixed_files() + [paths.raw_page(1)] + list(paths.legacy_files) + [paths.legacy_raw_directory / "020421_PD1_RASSI_page01.json"]
    for path in planted:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("x")
    neighbour = paths.legacy_raw_directory / "020421_PD11_RASSI_page01.json"
    neighbour.write_text("x")
    removed = remove_outputs(paths)
    assert sorted(removed) == sorted(planted)
    assert paths.existing_files() == []
    assert not paths.raw_directory.exists()
    assert neighbour.is_file()


def test_existing_raw_pages_only_match_their_own_stem(tmp_path):
    layout = DataLayout(input_root=tmp_path / "in", output_root=tmp_path / "out", cache_root=tmp_path / "cache")
    paths = output_paths(SOURCE, layout)
    paths.raw_directory.mkdir(parents=True)
    paths.raw_page(1).write_text("{}")
    (paths.raw_directory / "020421_PD11_RASSI_page01.json").write_text("{}")
    assert paths.existing_files() == [paths.raw_page(1)]


def test_csv_round_trip(tmp_path):
    layout = DataLayout(input_root=tmp_path / "in", output_root=tmp_path / "out", cache_root=tmp_path / "cache")
    paths = output_paths(SOURCE, layout)
    rows = build_wide_rows(pair_students(two_students()), SOURCE)
    write_csv(paths.wide_csv, wide_columns(), rows)
    with paths.wide_csv.open(newline="") as handle:
        read_back = list(csv.DictReader(handle))
    assert len(read_back) == 2
    assert read_back[0]["Q2_B"] == "b"
    assert "Q2_B_num" not in read_back[0]


def test_two_periods_for_one_teacher_write_to_separate_folders(tmp_path):
    layout = DataLayout(input_root=tmp_path / "in", output_root=tmp_path / "out", cache_root=tmp_path / "cache")
    first = output_paths(parse_source_filename("020421_PD1_RASSI.pdf"), layout)
    fourth = output_paths(parse_source_filename("020421_PD4_RASSI.pdf"), layout)
    assert first.directory == tmp_path / "out" / "020421" / "PD1"
    assert fourth.directory == tmp_path / "out" / "020421" / "PD4"
    assert first.raw_directory != fourth.raw_directory
    assert not set(first.fixed_files()) & set(fourth.fixed_files())
    for paths in (first, fourth):
        write_csv(paths.wide_csv, wide_columns(), [])
        paths.raw_directory.mkdir(parents=True)
        paths.raw_page(1).write_text("{}")
    assert first.existing_files() == [first.wide_csv, first.raw_page(1)]
    assert fourth.existing_files() == [fourth.wide_csv, fourth.raw_page(1)]


def student_with_multiple_marks(agreement: str, reason: str, confidence: str):
    header_items = [make_item(number) for number in range(1, 7)]
    header_items[0] = make_item(
        1,
        agreement=agreement,
        reason=reason,
        confidence=confidence,
        agreement_notes="separate circles",
        reason_notes="separate circles",
        confidence_notes="separate circles",
    )
    return pair_students(numbered([header_page(sid="222222", items=header_items), continuation_page()]))


def read_csv(path):
    with path.open(newline="") as handle:
        return list(csv.DictReader(handle))


@pytest.mark.parametrize(
    ("agreement", "reason", "confidence"),
    [("1,4", "a,b", "3,6"), ("2,3,5", "a,c,d", "1,2,3,4,5,6"), ("1,2,3,4,5,6", "a,b,c,d", "2,3,5")],
)
def test_multiple_marks_round_trip_through_wide_long_and_flag_csvs(tmp_path, agreement, reason, confidence):
    layout = DataLayout(input_root=tmp_path / "in", output_root=tmp_path / "out", cache_root=tmp_path / "cache")
    paths = output_paths(SOURCE, layout)
    students = student_with_multiple_marks(agreement, reason, confidence)
    write_csv(paths.wide_csv, wide_columns(), build_wide_rows(students, SOURCE))
    write_csv(paths.long_csv, LONG_COLUMNS, build_long_rows(students, "gpt-5.6-terra"))
    write_csv(paths.flags_csv, FLAG_COLUMNS, build_flag_rows(students, []))

    wide = read_csv(paths.wide_csv)[0]
    assert (wide["Q1_A"], wide["Q1_B"], wide["Q1_C"]) == (agreement, reason, confidence)

    long_q1 = {row["dimension"]: row for row in read_csv(paths.long_csv) if row["item"] == "1"}
    assert {dimension: row["value"] for dimension, row in long_q1.items()} == {"A": agreement, "B": reason, "C": confidence}
    assert all(row["value_num"] == "" for row in long_q1.values())

    flags = read_csv(paths.flags_csv)
    assert [(row["dimension"], row["flag_type"], row["value"]) for row in flags] == [
        ("A", "value_multiple", agreement),
        ("B", "value_multiple", reason),
        ("C", "value_multiple", confidence),
    ]


def test_ambiguous_is_never_converted_to_a_comma_list(tmp_path):
    layout = DataLayout(input_root=tmp_path / "in", output_root=tmp_path / "out", cache_root=tmp_path / "cache")
    paths = output_paths(SOURCE, layout)
    students = student_with_multiple_marks("ambiguous", "ambiguous", "ambiguous")
    write_csv(paths.wide_csv, wide_columns(), build_wide_rows(students, SOURCE))
    write_csv(paths.long_csv, LONG_COLUMNS, build_long_rows(students, "gpt-5.6-terra"))
    write_csv(paths.flags_csv, FLAG_COLUMNS, build_flag_rows(students, []))
    wide = read_csv(paths.wide_csv)[0]
    assert (wide["Q1_A"], wide["Q1_B"], wide["Q1_C"]) == ("ambiguous?", "ambiguous?", "ambiguous?")
    assert [row["value"] for row in read_csv(paths.long_csv) if row["item"] == "1"] == ["ambiguous"] * 3
    flags = read_csv(paths.flags_csv)
    assert {row["flag_type"] for row in flags} == {"value_ambiguous"}
    assert all(row["value"] == "ambiguous" and "," not in row["value"] for row in flags)


@pytest.mark.parametrize(
    ("field_name", "value"),
    [
        ("agreement", "multiple"),
        ("reason", "multiple"),
        ("agreement", "4,1"),
        ("agreement", "1,1"),
        ("agreement", "1, 4"),
        ("reason", "b,a"),
        ("reason", "a,e"),
        ("agreement", "ambiguous,1"),
        ("confidence", "1,7"),
    ],
)
def test_schema_rejects_the_multiple_word_and_malformed_comma_lists(field_name, value):
    with pytest.raises(ValidationError):
        make_item(1, **{field_name: value})


def test_schema_accepts_comma_lists_alongside_single_and_flag_values():
    item = make_item(1, agreement="1,4", reason="a,c,d", confidence="2,3,5")
    assert (item.agreement, item.reason, item.confidence) == ("1,4", "a,c,d", "2,3,5")
    assert make_item(1, agreement="ambiguous", reason="blank", confidence="6").agreement == "ambiguous"


def student_with_mark_confidence(sid: str = "333333", start: int = 1):
    header_items = [make_item(number) for number in range(1, 7)]
    header_items[0] = make_item(
        1,
        agreement="4",
        reason="c,d",
        confidence="blank",
        agreement_notes="circle on 4",
        reason_notes="one circle around c and d",
        confidence_notes="no mark",
        reason_mark_confidence="unsure",
        confidence_mark_confidence="unsure",
    )
    header_items[1] = make_item(
        2,
        agreement="ambiguous",
        reason="c,d",
        confidence="2,5",
        agreement_notes="mark sits on the border between 3 and 4",
        reason_notes="two separate circles, on c and on d",
        confidence_notes="two separate circles, one faint and broken",
        agreement_mark_confidence="clear",
        confidence_mark_confidence="unsure",
    )
    continuation_items = [make_item(number) for number in range(7, 13)]
    continuation_items[4] = make_item(11, reason="blank", reason_notes="no mark")
    return numbered([header_page(sid=sid, items=header_items), continuation_page(items=continuation_items)], start=start)


def test_question_mark_only_on_unsure_values_and_never_on_blank():
    row = build_wide_rows(pair_students(student_with_mark_confidence()), SOURCE)[0]
    assert row["Q1_A"] == "4"
    assert row["Q1_B"] == "c,d?"
    assert row["Q1_C"] == "blank"
    assert row["Q2_A"] == "ambiguous?"
    assert row["Q2_C"] == "2,5?"
    assert row["Q11_B"] == "blank"
    question_marked = [column for column, value in row.items() if str(value).endswith(config.UNSURE_VALUE_MARKER)]
    assert sorted(question_marked) == ["Q1_B", "Q2_A", "Q2_C"]


def test_separate_marks_on_two_options_stay_unmarked():
    row = build_wide_rows(pair_students(student_with_mark_confidence()), SOURCE)[0]
    assert row["Q2_B"] == "c,d"


def test_unsure_marker_comes_from_config(monkeypatch):
    monkeypatch.setattr("rassi.outputs.UNSURE_VALUE_MARKER", "(check)")
    row = build_wide_rows(pair_students(student_with_mark_confidence()), SOURCE)[0]
    assert row["Q1_B"] == "c,d(check)"


def test_review_columns_count_multiple_uncertain_and_blank_separately():
    pages = student_with_mark_confidence() + numbered([header_page(sid="444444"), continuation_page()], start=3)
    flagged, clean = build_wide_rows(pair_students(pages), SOURCE)
    assert (flagged["n_multiple"], flagged["n_uncertain"], flagged["n_blank"], flagged["check"]) == (3, 3, 2, "check")
    assert (clean["n_multiple"], clean["n_uncertain"], clean["n_blank"], clean["check"]) == (0, 0, 0, "")
    assert list(flagged)[-4:] == ["n_multiple", "n_uncertain", "n_blank", "check"]


def student_with_one_item(item: ItemExtraction):
    header_items = [make_item(number) for number in range(1, 7)]
    header_items[0] = item
    return pair_students(numbered([header_page(sid="666666", items=header_items), continuation_page()]))


def test_n_multiple_counts_comma_lists_whatever_their_mark_confidence():
    item = make_item(1, agreement="1,4", reason="a,c,d", confidence="2,5", confidence_mark_confidence="unsure")
    row = build_wide_rows(student_with_one_item(item), SOURCE)[0]
    assert (row["Q1_A"], row["Q1_B"], row["Q1_C"]) == ("1,4", "a,c,d", "2,5?")
    assert (row["n_multiple"], row["n_uncertain"]) == (3, 1)


def test_ambiguous_and_blank_are_not_double_entries():
    item = make_item(1, agreement="ambiguous", reason="blank", confidence="4")
    row = build_wide_rows(student_with_one_item(item), SOURCE)[0]
    assert (row["n_multiple"], row["n_uncertain"], row["n_blank"]) == (0, 1, 1)


def test_check_appears_for_a_confident_double_entry_alone():
    row = build_wide_rows(student_with_one_item(make_item(1, reason="c,d")), SOURCE)[0]
    assert (row["Q1_B"], row["n_multiple"], row["n_uncertain"], row["check"]) == ("c,d", 1, 0, "check")


def test_check_appears_for_an_uncertain_single_value_alone():
    row = build_wide_rows(student_with_one_item(make_item(1, agreement="4", agreement_mark_confidence="unsure")), SOURCE)[0]
    assert (row["Q1_A"], row["n_multiple"], row["n_uncertain"], row["check"]) == ("4?", 0, 1, "check")


def test_student_with_only_blanks_is_not_marked_for_checking():
    items = [make_item(number, agreement="blank", reason="blank", confidence="blank") for number in range(1, 7)]
    row = build_wide_rows(pair_students(numbered([header_page(items=items), continuation_page()])), SOURCE)[0]
    assert (row["n_multiple"], row["n_uncertain"], row["n_blank"], row["check"]) == (0, 0, 18, "")


def test_long_csv_keeps_the_raw_value_beside_the_marked_value(tmp_path):
    layout = DataLayout(input_root=tmp_path / "in", output_root=tmp_path / "out", cache_root=tmp_path / "cache")
    paths = output_paths(SOURCE, layout)
    write_csv(paths.long_csv, LONG_COLUMNS, build_long_rows(pair_students(student_with_mark_confidence()), "gpt-5.6-terra"))
    rows = {(row["item"], row["dimension"]): row for row in read_csv(paths.long_csv)}
    one_circle = rows[("1", "B")]
    assert (one_circle["value"], one_circle["value_marked"], one_circle["mark_confidence"]) == ("c,d", "c,d?", "unsure")
    separate = rows[("2", "B")]
    assert (separate["value"], separate["value_marked"], separate["mark_confidence"]) == ("c,d", "c,d", "clear")
    blank = rows[("1", "C")]
    assert (blank["value"], blank["value_marked"], blank["mark_confidence"]) == ("blank", "blank", "clear")
    ambiguous = rows[("2", "A")]
    assert (ambiguous["value"], ambiguous["value_marked"], ambiguous["mark_confidence"]) == ("ambiguous", "ambiguous?", "unsure")
    assert rows[("2", "C")]["value_num"] == ""
    assert rows[("1", "A")]["value_num"] == "4"


def test_review_log_lists_double_entries_then_uncertain_values_then_blanks():
    confident_double = [make_item(number) for number in range(1, 7)]
    confident_double[3] = make_item(4, agreement="1,3", agreement_notes="two separate circles, on 1 and on 3")
    pages = (
        student_with_mark_confidence(sid="0204210407")
        + numbered([header_page(sid="0204210409"), continuation_page()], start=3)
        + numbered([header_page(sid="0204210411", items=confident_double), continuation_page()], start=5)
    )
    text = build_review_text(pair_students(pages), "020421_PD4_RASSI.pdf", "020421", "4", "PRE")
    assert text == (
        "Review list for 020421_PD4_RASSI.pdf\n"
        "Teacher 020421, period 4, PRE\n"
        "\n"
        "Summary: 4 double entries, 3 uncertain values, 2 blanks. 2 students need checking.\n"
        "\n"
        "Double entries (4)\n"
        f"{DOUBLE_ENTRIES_EXPLANATION}\n"
        "\n"
        "Student 0204210407\n"
        "Item 1, Reason: c,d page 1 one circle around c and d\n"
        "Item 2, Reason: c,d page 1 two separate circles, on c and on d\n"
        "Item 2, Confidence: 2,5 page 1 two separate circles, one faint and broken\n"
        "\n"
        "Student 0204210411\n"
        "Item 4, Agreement: 1,3 page 5 two separate circles, on 1 and on 3\n"
        "\n"
        "Uncertain values (3)\n"
        f"{UNCERTAIN_EXPLANATION}\n"
        "\n"
        "Student 0204210407\n"
        "Item 1, Reason: c,d page 1 one circle around c and d\n"
        "Item 2, Agreement: ambiguous page 1 mark sits on the border between 3 and 4\n"
        "Item 2, Confidence: 2,5 page 1 two separate circles, one faint and broken\n"
        "\n"
        "Blanks (2)\n"
        f"{BLANKS_EXPLANATION}\n"
        "\n"
        "Student 0204210407 Item 1 Confidence, page 1\n"
        "Student 0204210407 Item 11 Reason, page 2\n"
    )


def test_review_log_shows_none_under_an_empty_section():
    items = [make_item(number) for number in range(1, 7)]
    items[0] = make_item(1, agreement="1,4", agreement_notes="two separate circles", reason="blank", reason_notes="no mark")
    students = pair_students(numbered([header_page(sid="777777", items=items), continuation_page()]))
    text = build_review_text(students, "020421_PD1_RASSI.pdf", "020421", "1", "PRE")
    assert "Summary: 1 double entry, 0 uncertain values, 1 blank. 1 student needs checking.\n" in text
    assert f"Double entries (1)\n{DOUBLE_ENTRIES_EXPLANATION}\n\nStudent 777777\nItem 1, Agreement: 1,4 page 1 two separate circles\n" in text
    assert f"\nUncertain values (0)\n{UNCERTAIN_EXPLANATION}\n\nNone\n" in text
    assert f"\nBlanks (1)\n{BLANKS_EXPLANATION}\n\nStudent 777777 Item 1 Reason, page 1\n" in text


def test_review_log_for_a_file_with_nothing_to_check():
    students = pair_students(numbered([header_page(sid="555555"), continuation_page(), header_page(sid="REDACTED"), continuation_page()]))
    text = build_review_text(students, "020421_PD1_RASSI.pdf", "020421", "1", None)
    assert text == (
        "Review list for 020421_PD1_RASSI.pdf\n"
        "Teacher 020421, period 1, PRE/POST undetermined\n"
        "\n"
        "Summary: 0 double entries, 0 uncertain values, 0 blanks. 0 students need checking.\n"
        "\n"
        "Double entries (0)\n"
        f"{DOUBLE_ENTRIES_EXPLANATION}\n"
        "\n"
        "None\n"
        "\n"
        "Uncertain values (0)\n"
        f"{UNCERTAIN_EXPLANATION}\n"
        "\n"
        "None\n"
        "\n"
        "Blanks (0)\n"
        f"{BLANKS_EXPLANATION}\n"
        "\n"
        "None\n"
    )


def test_review_log_names_redacted_students_by_starting_page():
    items = [make_item(number) for number in range(1, 7)]
    items[0] = make_item(1, agreement="ambiguous", agreement_notes="written words")
    students = pair_students(numbered([header_page(sid="REDACTED", items=items), continuation_page()], start=5))
    text = build_review_text(students, "020421_PD1_RASSI.pdf", "020421", "1", "POST")
    assert "Student REDACTED, starting page 5\nItem 1, Agreement: ambiguous page 5 written words\n" in text
