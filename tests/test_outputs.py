import csv

from rassi import config
from rassi.filename import parse_source_filename
from rassi.paths import DataLayout, output_paths
from rassi.outputs import (
    FLAG_COLUMNS,
    LONG_COLUMNS,
    build_flag_rows,
    build_long_rows,
    build_wide_rows,
    flag_counts,
    numeric_value,
    wide_columns,
    write_csv,
)
from rassi.structure import identity_flags, pair_students
from tests.fakes import continuation_page, header_page, make_item, numbered

SOURCE = parse_source_filename("020421_PD1_RASSI.pdf")


def two_students():
    first_header_items = [make_item(number, agreement=str(number), reason="abcd"[(number - 1) % 4], confidence="6") for number in range(1, 7)]
    first_header_items[2] = make_item(3, agreement="ambiguous", reason="blank", confidence="multiple", mark_notes="circle spans 3 and 4")
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
    assert columns[7:11] == ["Q1_A", "Q1_B", "Q1_B_num", "Q1_C"]
    assert columns[-4:] == ["Q12_A", "Q12_B", "Q12_B_num", "Q12_C"]
    assert len(columns) == 7 + 12 * 4


def test_wide_rows_use_printed_item_numbers_and_map_reasons():
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
    assert first["Q2_B_num"] == 2
    assert first["Q4_B"] == "d"
    assert first["Q4_B_num"] == 4
    assert first["Q3_A"] == "ambiguous"
    assert first["Q3_B"] == "blank"
    assert first["Q3_B_num"] == config.MISSING_VALUE_CODE
    assert first["Q3_C"] == "multiple"
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
    assert q3["A"]["page_number"] == 1
    q8 = {row["dimension"]: row for row in rows if row["sid"] == "111111" and row["item"] == 8}
    assert q8["A"]["value_num"] == 4
    assert q8["B"]["value_num"] == 2
    assert q8["C"]["value_num"] == 5
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
    assert all(set(row) == set(FLAG_COLUMNS) for row in rows)


def test_missing_value_code_is_configurable(monkeypatch):
    monkeypatch.setattr("rassi.outputs.MISSING_VALUE_CODE", "NA")
    assert numeric_value("A", "blank") == "NA"
    assert numeric_value("B", "multiple") == "NA"
    assert numeric_value("C", "6") == 6


def test_output_paths_are_named_from_the_pdf_stem(tmp_path):
    layout = DataLayout(input_root=tmp_path / "in", output_root=tmp_path / "out", cache_root=tmp_path / "cache")
    paths = output_paths(SOURCE, layout)
    directory = tmp_path / "out" / "020421"
    assert paths.directory == directory
    assert paths.stem == "020421_PD1_RASSI"
    assert paths.wide_csv == directory / "020421_PD1_RASSI_wide.csv"
    assert paths.long_csv == directory / "020421_PD1_RASSI_long.csv"
    assert paths.flags_csv == directory / "020421_PD1_RASSI_flags.csv"
    assert paths.metrics_csv == directory / "020421_PD1_RASSI_metrics.csv"
    assert paths.manifest_json == directory / "020421_PD1_RASSI_manifest.json"
    assert paths.raw_directory == directory / "raw"
    assert paths.raw_page(3) == directory / "raw" / "020421_PD1_RASSI_page03.json"


def test_existing_raw_pages_only_match_their_own_stem(tmp_path):
    layout = DataLayout(input_root=tmp_path / "in", output_root=tmp_path / "out", cache_root=tmp_path / "cache")
    paths = output_paths(SOURCE, layout)
    other = output_paths(parse_source_filename("020421_PD11_RASSI.pdf"), layout)
    paths.raw_directory.mkdir(parents=True)
    paths.raw_page(1).write_text("{}")
    other.raw_page(1).write_text("{}")
    assert paths.existing_files() == [paths.raw_page(1)]


def test_csv_round_trip(tmp_path):
    layout = DataLayout(input_root=tmp_path / "in", output_root=tmp_path / "out", cache_root=tmp_path / "cache")
    paths = output_paths(SOURCE, layout)
    rows = build_wide_rows(pair_students(two_students()), SOURCE)
    write_csv(paths.wide_csv, wide_columns(), rows)
    with paths.wide_csv.open(newline="") as handle:
        read_back = list(csv.DictReader(handle))
    assert len(read_back) == 2
    assert read_back[0]["Q3_B_num"] == ""
    assert read_back[0]["Q2_B_num"] == "2"
