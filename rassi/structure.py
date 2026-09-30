from collections import Counter
from dataclasses import dataclass

from rassi.config import ALL_ITEM_NUMBERS, CONTINUATION_ITEM_NUMBERS, HEADER_ITEM_NUMBERS, ITEMS_PER_PAGE, REDACTED_SID
from rassi.filename import normalize_period
from rassi.schema import PageExtraction


@dataclass(frozen=True)
class StructureViolation:
    page_number: int | None
    expected: str
    found: str

    def describe(self) -> str:
        location = f"page {self.page_number}" if self.page_number is not None else "file"
        return f"{location}: expected {self.expected}, found {self.found}"


@dataclass(frozen=True)
class IdentityFlag:
    flag_type: str
    page_number: int | None
    sid: str | None
    reason: str


@dataclass(frozen=True)
class Student:
    header_page_number: int
    continuation_page_number: int
    header: PageExtraction
    continuation: PageExtraction

    @property
    def sid(self) -> str | None:
        return self.header.sid

    @property
    def pre_post(self) -> str:
        if self.header.pre_post != "unreadable":
            return self.header.pre_post
        return self.continuation.pre_post


def response_problems(extraction: PageExtraction) -> list[str]:
    problems = []
    item_numbers = [item.item_number for item in extraction.items]
    if len(item_numbers) != ITEMS_PER_PAGE:
        problems.append(f"returned {len(item_numbers)} items, expected {ITEMS_PER_PAGE}")
    out_of_range = sorted({number for number in item_numbers if number not in ALL_ITEM_NUMBERS})
    if out_of_range:
        problems.append(f"item numbers {out_of_range} are outside {ALL_ITEM_NUMBERS[0]} to {ALL_ITEM_NUMBERS[-1]}")
    duplicated = sorted(number for number, count in Counter(item_numbers).items() if count > 1)
    if duplicated:
        problems.append(f"item numbers {duplicated} appear more than once")
    expected = HEADER_ITEM_NUMBERS if extraction.page_type == "header" else CONTINUATION_ITEM_NUMBERS
    if not problems and sorted(item_numbers) != expected:
        problems.append(f"a {extraction.page_type} page should hold items {expected[0]} to {expected[-1]}, found {sorted(item_numbers)}")
    return problems


def expected_page_type(position: int) -> str:
    return "header" if position % 2 == 0 else "continuation"


def check_structure(pages: list[tuple[int, PageExtraction]]) -> list[StructureViolation]:
    violations = []
    for (previous_number, _), (page_number, _) in zip(pages, pages[1:]):
        if page_number != previous_number + 1:
            violations.append(
                StructureViolation(
                    page_number=page_number,
                    expected=f"page {previous_number + 1} to follow page {previous_number}",
                    found=f"page {page_number}; the page range has a gap",
                )
            )
    for position, (page_number, extraction) in enumerate(pages):
        expected_type = expected_page_type(position)
        if extraction.page_type != expected_type:
            violations.append(
                StructureViolation(
                    page_number=page_number,
                    expected=f"a {expected_type} page (position {position + 1} in the sequence)",
                    found=f"a {extraction.page_type} page",
                )
            )
            continue
        expected_items = HEADER_ITEM_NUMBERS if expected_type == "header" else CONTINUATION_ITEM_NUMBERS
        found_items = [item.item_number for item in extraction.items]
        if sorted(found_items) != expected_items:
            violations.append(
                StructureViolation(
                    page_number=page_number,
                    expected=f"items {expected_items[0]} to {expected_items[-1]} on a {expected_type} page",
                    found=f"items {found_items}",
                )
            )
    if len(pages) % 2 != 0:
        last_page_number = pages[-1][0] if pages else None
        violations.append(
            StructureViolation(
                page_number=last_page_number,
                expected="an even page count (2 pages per student)",
                found=f"{len(pages)} pages; page {last_page_number} has no partner page",
            )
        )
    return violations


def pair_students(pages: list[tuple[int, PageExtraction]]) -> list[Student]:
    students = []
    for index in range(0, len(pages) - 1, 2):
        header_number, header = pages[index]
        continuation_number, continuation = pages[index + 1]
        students.append(
            Student(
                header_page_number=header_number,
                continuation_page_number=continuation_number,
                header=header,
                continuation=continuation,
            )
        )
    return students


def file_pre_post(pages: list[tuple[int, PageExtraction]]) -> str | None:
    readable = Counter(extraction.pre_post for _, extraction in pages if extraction.pre_post != "unreadable")
    if not readable:
        return None
    ranked = readable.most_common()
    if len(ranked) > 1 and ranked[0][1] == ranked[1][1]:
        return None
    return ranked[0][0]


def identity_flags(students: list[Student], filename_period: str, majority_pre_post: str | None) -> list[IdentityFlag]:
    flags = []
    expected_period = normalize_period(filename_period)
    sid_counts = Counter(student.sid for student in students if student.sid and student.sid.strip().upper() != REDACTED_SID)

    if majority_pre_post is None and students:
        flags.append(
            IdentityFlag(
                flag_type="pre_post_file_undetermined",
                page_number=None,
                sid=None,
                reason="Could not determine a single PRE/POST value for the file",
            )
        )

    for student in students:
        sid = student.sid
        page = student.header_page_number
        if sid is None or not sid.strip():
            flags.append(IdentityFlag("sid_missing", page, sid, "Header page has no readable SID"))
        elif sid.strip().upper() == REDACTED_SID:
            flags.append(IdentityFlag("sid_redacted", page, sid, "SID stamp says REDACTED"))
        elif sid_counts[sid] > 1:
            flags.append(IdentityFlag("sid_duplicate", page, sid, f"SID {sid} appears on {sid_counts[sid]} students in this file"))

        stamp_period = normalize_period(student.header.stamp_period)
        if stamp_period is None or not stamp_period:
            flags.append(IdentityFlag("period_missing", page, sid, f"No readable PD stamp; filename period is {filename_period}"))
        elif stamp_period != expected_period:
            flags.append(
                IdentityFlag(
                    "period_mismatch",
                    page,
                    sid,
                    f"Stamp PD '{student.header.stamp_period}' does not match filename period {filename_period}",
                )
            )

        for page_number, extraction in (
            (student.header_page_number, student.header),
            (student.continuation_page_number, student.continuation),
        ):
            if extraction.pre_post == "unreadable":
                flags.append(IdentityFlag("pre_post_unreadable", page_number, sid, "PRE/POST label unreadable"))

        header_value = student.header.pre_post
        continuation_value = student.continuation.pre_post
        if "unreadable" not in (header_value, continuation_value) and header_value != continuation_value:
            flags.append(
                IdentityFlag(
                    "pre_post_student_mismatch",
                    page,
                    sid,
                    f"Page {student.header_page_number} says {header_value} but page {student.continuation_page_number} says {continuation_value}",
                )
            )

        if majority_pre_post is not None:
            for page_number, extraction in (
                (student.header_page_number, student.header),
                (student.continuation_page_number, student.continuation),
            ):
                if extraction.pre_post not in ("unreadable", majority_pre_post):
                    flags.append(
                        IdentityFlag(
                            "pre_post_file_mismatch",
                            page_number,
                            sid,
                            f"Page says {extraction.pre_post} but the rest of the file is {majority_pre_post}",
                        )
                    )
    return flags
