from pathlib import Path

import pytest

from rassi.discover import (
    InputError,
    InputFile,
    discover_all,
    discover_file,
    discover_teacher,
    output_collisions,
)
from rassi.filename import parse_source_filename
from rassi.paths import DataLayout


@pytest.fixture
def layout(tmp_path) -> DataLayout:
    return DataLayout(input_root=tmp_path / "in", output_root=tmp_path / "out", cache_root=tmp_path / "cache")


def place(layout: DataLayout, relative: str) -> Path:
    path = layout.input_root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"placeholder, never opened")
    return path


def names(discovery) -> list[str]:
    return [input_file.path.name for input_file in discovery.files]


def test_discovery_across_teacher_folders_is_sorted_and_stable(layout):
    for relative in [
        "300001/300001_PD10_RASSI.pdf",
        "020421/020421_PD3_RASSI.pdf",
        "300001/300001_PD2_RASSI.pdf",
        "020421/020421_PD1_RASSI.pdf",
        "115500/115500_PD4_RASSI.pdf",
    ]:
        place(layout, relative)
    expected = [
        "020421_PD1_RASSI.pdf",
        "020421_PD3_RASSI.pdf",
        "115500_PD4_RASSI.pdf",
        "300001_PD2_RASSI.pdf",
        "300001_PD10_RASSI.pdf",
    ]
    first = discover_all(layout)
    assert names(first) == expected
    assert names(discover_all(layout)) == expected
    assert first.problems == []


def test_teacher_code_mismatch_is_reported_and_skipped(layout):
    good = place(layout, "020421/020421_PD1_RASSI.pdf")
    misplaced = place(layout, "020421/999999_PD2_RASSI.pdf")
    discovery = discover_all(layout)
    assert [input_file.path for input_file in discovery.files] == [good]
    assert len(discovery.problems) == 1
    problem = discovery.problems[0]
    assert problem.path == misplaced
    assert "999999" in problem.message and "020421" in problem.message
    assert "999999_PD2_RASSI.pdf" in problem.describe()


def test_teacher_code_mismatch_with_file_option(layout):
    misplaced = place(layout, "020421/111111_PD1_RASSI.pdf")
    discovery = discover_file(layout, misplaced)
    assert discovery.files == []
    assert discovery.problems[0].path == misplaced


def test_unparseable_filename_is_skipped_not_fatal(layout):
    place(layout, "020421/020421_PD1_RASSI.pdf")
    bad = place(layout, "020421/020421 period 2 scan.pdf")
    notes = place(layout, "020421/notes.txt")
    discovery = discover_all(layout)
    assert names(discovery) == ["020421_PD1_RASSI.pdf"]
    assert {problem.path for problem in discovery.problems} == {bad, notes}
    assert all("expected format" in problem.message for problem in discovery.problems)


def test_empty_teacher_folder_is_reported_and_skipped(layout):
    place(layout, "020421/020421_PD1_RASSI.pdf")
    empty = layout.input_root / "020415"
    empty.mkdir()
    (empty / ".gitkeep").write_bytes(b"")
    discovery = discover_all(layout)
    assert names(discovery) == ["020421_PD1_RASSI.pdf"]
    assert discovery.problems == []
    assert len(discovery.warnings) == 1
    assert discovery.warnings[0].path == empty
    assert "no PDFs" in discovery.warnings[0].message


def test_empty_teacher_folder_with_teacher_option(layout):
    place(layout, "020421/020421_PD1_RASSI.pdf")
    (layout.input_root / "115500").mkdir()
    discovery = discover_teacher(layout, "115500")
    assert discovery.files == []
    assert "no PDFs" in discovery.problems[0].message


def test_missing_input_folder_is_a_clear_error(layout):
    with pytest.raises(InputError) as error:
        discover_all(layout)
    assert "does not exist" in str(error.value)
    assert str(layout.input_root) in str(error.value)


def test_input_folder_with_only_gitkeep_is_empty(layout):
    layout.input_root.mkdir(parents=True)
    (layout.input_root / ".gitkeep").write_bytes(b"")
    with pytest.raises(InputError) as error:
        discover_all(layout)
    assert "is empty" in str(error.value)


def test_unknown_teacher_folder_is_a_clear_error(layout):
    place(layout, "020421/020421_PD1_RASSI.pdf")
    with pytest.raises(InputError) as error:
        discover_teacher(layout, "999999")
    assert "999999" in str(error.value)


def test_teacher_option_rejects_paths(layout):
    place(layout, "020421/020421_PD1_RASSI.pdf")
    with pytest.raises(InputError):
        discover_teacher(layout, "../020421")


def test_pdf_directly_in_input_folder_is_skipped(layout):
    place(layout, "020421/020421_PD1_RASSI.pdf")
    loose = place(layout, "020421_PD2_RASSI.pdf")
    discovery = discover_all(layout)
    assert names(discovery) == ["020421_PD1_RASSI.pdf"]
    assert discovery.problems[0].path == loose


def test_output_collision_between_two_inputs(layout):
    first = InputFile(Path("in/abc/abc_PD1_RASSI.pdf"), parse_source_filename("abc_PD1_RASSI.pdf"))
    second = InputFile(Path("in/ABC/ABC_PD1_RASSI.pdf"), parse_source_filename("ABC_PD1_RASSI.pdf"))
    unrelated = InputFile(Path("in/abc/abc_PD2_RASSI.pdf"), parse_source_filename("abc_PD2_RASSI.pdf"))
    collisions = output_collisions([first, unrelated, second], layout)
    assert len(collisions) == 1
    assert "abc_PD1_RASSI.pdf" in collisions[0]
    assert "ABC_PD1_RASSI.pdf" in collisions[0]


def test_no_collision_for_distinct_inputs(layout):
    for relative in ["020421/020421_PD1_RASSI.pdf", "020421/020421_PD2_RASSI.pdf", "115500/115500_PD1_RASSI.pdf"]:
        place(layout, relative)
    assert output_collisions(discover_all(layout).files, layout) == []


def test_unknown_teacher_code_warns_but_still_processes(layout):
    place(layout, "020421/020421_PD1_RASSI.pdf")
    typo = place(layout, "020412/020412_PD1_RASSI.pdf")
    discovery = discover_all(layout)
    assert names(discovery) == ["020412_PD1_RASSI.pdf", "020421_PD1_RASSI.pdf"]
    assert discovery.problems == []
    assert [warning.path for warning in discovery.warnings] == [typo]
    assert "020412" in discovery.warnings[0].message


def test_file_option_warns_for_unknown_teacher_code(layout):
    pdf = place(layout, "999999/999999_PD1_RASSI.pdf")
    discovery = discover_file(layout, pdf)
    assert len(discovery.files) == 1
    assert "999999" in discovery.warnings[0].message


def test_file_outside_input_folder_is_rejected(layout, tmp_path):
    outside = tmp_path / "elsewhere" / "020421" / "020421_PD1_RASSI.pdf"
    outside.parent.mkdir(parents=True)
    outside.write_bytes(b"placeholder")
    layout.input_root.mkdir(parents=True)
    with pytest.raises(InputError) as error:
        discover_file(layout, outside)
    assert "is not inside" in str(error.value)
    assert "020421_PD1_RASSI.pdf" in str(error.value)


def test_file_nested_too_deep_is_rejected(layout):
    nested = place(layout, "020421/extra/020421/020421_PD1_RASSI.pdf")
    with pytest.raises(InputError):
        discover_file(layout, nested)


def test_file_reached_through_dot_dot_is_resolved(layout):
    pdf = place(layout, "020421/020421_PD1_RASSI.pdf")
    roundabout = layout.input_root / "020421" / ".." / "020421" / pdf.name
    discovery = discover_file(layout, roundabout)
    assert discovery.files[0].path == pdf.resolve()
