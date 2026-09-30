from rassi.structure import check_structure, file_pre_post, identity_flags, pair_students
from tests.fakes import continuation_page, header_page, make_item, numbered


def valid_sequence(student_count: int = 3):
    pages = []
    for index in range(student_count):
        pages += [header_page(sid=f"10000{index}"), continuation_page()]
    return numbered(pages)


def test_valid_sequence_has_no_violations():
    assert check_structure(valid_sequence()) == []


def test_missing_continuation_page_is_reported_at_the_right_page():
    pages = numbered([header_page(), continuation_page(), header_page(sid="2"), header_page(sid="3"), continuation_page()])
    violations = check_structure(pages)
    assert violations[0].page_number == 4
    assert "continuation" in violations[0].expected
    assert "header" in violations[0].found
    assert any("even page count" in violation.expected for violation in violations)


def test_swapped_pages_are_reported():
    pages = numbered([header_page(), continuation_page(), continuation_page(), header_page(sid="2")])
    violations = check_structure(pages)
    assert [violation.page_number for violation in violations] == [3, 4]
    assert "header" in violations[0].expected
    assert "continuation" in violations[0].found


def test_odd_page_count_is_reported():
    pages = numbered([header_page(), continuation_page(), header_page(sid="2")])
    violations = check_structure(pages)
    assert len(violations) == 1
    assert violations[0].page_number == 3
    assert "even page count" in violations[0].expected


def test_wrong_item_numbers_are_reported():
    bad_items = [make_item(number) for number in (1, 2, 3, 4, 5, 7)]
    pages = numbered([header_page(items=bad_items), continuation_page()])
    violations = check_structure(pages)
    assert len(violations) == 1
    assert violations[0].page_number == 1
    assert "items 1 to 6" in violations[0].expected


def test_continuation_with_header_items_is_reported():
    pages = numbered([header_page(), continuation_page(items=[make_item(number) for number in range(1, 7)])])
    violations = check_structure(pages)
    assert violations[0].page_number == 2
    assert "items 7 to 12" in violations[0].expected


def test_gap_in_page_numbers_is_reported():
    pages = [(1, header_page()), (2, continuation_page()), (5, header_page(sid="2")), (6, continuation_page())]
    violations = check_structure(pages)
    assert len(violations) == 1
    assert violations[0].page_number == 5


def test_identity_flags_for_redacted_missing_and_period_mismatch():
    pages = numbered(
        [
            header_page(sid="REDACTED"), continuation_page(),
            header_page(sid=None), continuation_page(),
            header_page(sid="555", stamp_period="2"), continuation_page(),
            header_page(sid="777", stamp_period="01"), continuation_page(),
        ]
    )
    students = pair_students(pages)
    flags = identity_flags(students, "1", file_pre_post(pages))
    by_type = {(flag.flag_type, flag.page_number) for flag in flags}
    assert ("sid_redacted", 1) in by_type
    assert ("sid_missing", 3) in by_type
    assert ("period_mismatch", 5) in by_type
    assert not any(flag.page_number == 7 for flag in flags)


def test_pre_post_disagreement_within_student_and_across_file():
    pages = numbered(
        [
            header_page(sid="1"), continuation_page(),
            header_page(sid="2"), continuation_page(pre_post="POST"),
            header_page(sid="3"), continuation_page(),
        ]
    )
    students = pair_students(pages)
    majority = file_pre_post(pages)
    assert majority == "PRE"
    flags = identity_flags(students, "1", majority)
    types = {(flag.flag_type, flag.page_number) for flag in flags}
    assert ("pre_post_student_mismatch", 3) in types
    assert ("pre_post_file_mismatch", 4) in types


def test_duplicate_sid_is_flagged():
    pages = numbered([header_page(sid="9"), continuation_page(), header_page(sid="9"), continuation_page()])
    flags = identity_flags(pair_students(pages), "1", "PRE")
    assert [flag.flag_type for flag in flags] == ["sid_duplicate", "sid_duplicate"]
