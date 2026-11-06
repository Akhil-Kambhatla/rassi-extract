import json
import re
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, ValidationError, field_validator, model_validator

from rassi.cache import ResponseCache, cache_key
from rassi.config import ALL_ITEM_NUMBERS, CONTINUATION_ITEM_NUMBERS, HEADER_ITEM_NUMBERS
from rassi.extract import PageExtractor, PageFailed, usage_from_response
from rassi.prompt import EVAL_USER_INSTRUCTION, PROMPT_VERSION, USER_INSTRUCTION
from rassi.schema import SCHEMA_VERSION, ItemExtraction, MarkConfidence, PageExtraction, ReasonValue, ScaleValue
from rassi.spend import SpendTracker
from rassi.structure import response_problems

CROP_FILE = "crop.png"
PAGE_FILE = "page.png"
LABEL_FILE = "label.json"
NOTES_FILE = "notes.md"
CASE_FILE = "case.json"
CASE_TYPE_CROP = "crop"
CASE_TYPE_PAGE = "page"
IMAGE_FILES = {CASE_TYPE_CROP: CROP_FILE, CASE_TYPE_PAGE: PAGE_FILE}
USER_INSTRUCTIONS = {CASE_TYPE_CROP: EVAL_USER_INSTRUCTION, CASE_TYPE_PAGE: USER_INSTRUCTION}
DIMENSION_FIELDS = ("agreement", "reason", "confidence")
MARK_CONFIDENCE_FIELDS = {name: f"{name}_mark_confidence" for name in DIMENSION_FIELDS}
SCORED_ITEM_FIELDS = tuple(field for name in DIMENSION_FIELDS for field in (name, MARK_CONFIDENCE_FIELDS[name]))
DIMENSION_SHORT_NAMES = {"agreement": "A", "reason": "B", "confidence": "C"}
DIMENSION_SHORT_NAMES.update({MARK_CONFIDENCE_FIELDS[name]: f"{DIMENSION_SHORT_NAMES[name]}_mark" for name in DIMENSION_FIELDS})
NOTE_FIELDS = {"agreement": "agreement_notes", "reason": "reason_notes", "confidence": "confidence_notes"}
PAGE_FIELDS = ("page_type", "sid")
CASE_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
RESULTS_FILE_PATTERN = re.compile(r"^\d{8}T\d{6}Z_.+\.json$")

STATUS_PASS = "pass"
STATUS_FAIL = "fail"
STATUS_UNSTABLE = "unstable"
STATUS_NOT_RUN = "not_run"


class CaseError(ValueError):
    pass


class CaseLabel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    item_number: int
    agreement: ScaleValue
    reason: ReasonValue
    confidence: ScaleValue
    agreement_mark_confidence: MarkConfidence | None = None
    reason_mark_confidence: MarkConfidence | None = None
    confidence_mark_confidence: MarkConfidence | None = None

    @field_validator("item_number")
    @classmethod
    def item_number_in_range(cls, value: int) -> int:
        if value not in ALL_ITEM_NUMBERS:
            raise ValueError(f"must be {ALL_ITEM_NUMBERS[0]} to {ALL_ITEM_NUMBERS[-1]}")
        return value


def item_numbers_for_page_type(page_type: str) -> list[int]:
    return HEADER_ITEM_NUMBERS if page_type == "header" else CONTINUATION_ITEM_NUMBERS


def page_type_for_page_number(page_number: int) -> str:
    return "header" if page_number % 2 == 1 else "continuation"


class PageLabel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    page_type: Literal["header", "continuation"]
    sid: str | None
    items: list[CaseLabel]

    @model_validator(mode="after")
    def items_and_sid_fit_page_type(self) -> "PageLabel":
        expected = item_numbers_for_page_type(self.page_type)
        found = sorted(item.item_number for item in self.items)
        if found != expected:
            raise ValueError(f"a {self.page_type} page holds items {expected[0]} to {expected[-1]} once each, found {found}")
        if self.page_type == "continuation" and self.sid is not None:
            raise ValueError("a continuation page has no SID stamp, so sid must be null")
        return self


class PartialCaseLabel(CaseLabel):
    agreement: ScaleValue | None = None
    reason: ReasonValue | None = None
    confidence: ScaleValue | None = None


class PartialPageLabel(PageLabel):
    partial: Literal[True]
    items: list[PartialCaseLabel]

    @model_validator(mode="after")
    def has_scored_value(self) -> "PartialPageLabel":
        if not any(getattr(item, name) is not None for item in self.items for name in DIMENSION_FIELDS):
            raise ValueError("a partial page label needs at least one adjudicated value")
        return self


class CaseInfo(BaseModel):
    model_config = ConfigDict(extra="forbid")

    case_type: Literal["crop", "page"]
    source_pdf: str | None = None
    page_number: int | None = None


@dataclass(frozen=True)
class ValueKey:
    item_number: int | None
    field: str

    @property
    def label(self) -> str:
        if self.item_number is None:
            return self.field
        return f"Q{self.item_number}_{DIMENSION_SHORT_NAMES[self.field]}"

    @property
    def is_page_field(self) -> bool:
        return self.item_number is None

    @property
    def is_mark_confidence(self) -> bool:
        return self.field in MARK_CONFIDENCE_FIELDS.values()

    def sort_key(self) -> tuple:
        if self.item_number is None:
            return (0, 0, PAGE_FIELDS.index(self.field))
        return (1, self.item_number, SCORED_ITEM_FIELDS.index(self.field))


@dataclass(frozen=True)
class EvalCase:
    case_id: str
    case_type: str
    image_png: bytes
    expected: dict[ValueKey, str | None]
    notes: str

    @property
    def item_numbers(self) -> list[int]:
        return sorted({key.item_number for key in self.expected if key.item_number is not None})


@dataclass(frozen=True)
class CaseLibrary:
    cases: list[EvalCase]
    unlabeled: list[str]


def validate_case_id(case_id: str) -> str:
    if not CASE_ID_PATTERN.match(case_id):
        raise CaseError(f"case id '{case_id}' must use lowercase letters, digits, '-' and '_' only, starting with a letter or digit")
    return case_id


def empty_label(item_number: int) -> dict:
    return {"item_number": item_number} | {name: None for name in SCORED_ITEM_FIELDS}


def empty_page_label(page_number: int) -> dict:
    item_numbers = item_numbers_for_page_type(page_type_for_page_number(page_number))
    return {"page_type": None, "sid": None, "items": [empty_label(number) for number in item_numbers]}


def item_label_is_empty(data: dict) -> bool:
    return all(data.get(name) is None for name in SCORED_ITEM_FIELDS)


def label_is_empty(case_type: str, data: object) -> bool:
    if not isinstance(data, dict):
        return False
    if case_type == CASE_TYPE_CROP:
        return item_label_is_empty(data)
    items = data.get("items")
    return (
        data.get("page_type") is None
        and data.get("sid") is None
        and isinstance(items, list)
        and all(isinstance(item, dict) and item_label_is_empty(item) for item in items)
    )


def validation_problems(error: ValidationError, default_location: str) -> str:
    return "; ".join(f"{'.'.join(str(part) for part in problem['loc']) or default_location}: {problem['msg']}" for problem in error.errors())


def parse_label(data: object, source: str) -> CaseLabel:
    if not isinstance(data, dict):
        raise CaseError(f"{source} must hold a JSON object")
    try:
        return CaseLabel.model_validate(data)
    except ValidationError as error:
        raise CaseError(f"{source} is not a valid label: {validation_problems(error, 'label')}") from error


def parse_page_label(data: object, source: str) -> PageLabel:
    if not isinstance(data, dict):
        raise CaseError(f"{source} must hold a JSON object")
    try:
        label_type = PartialPageLabel if data.get("partial") is True else PageLabel
        return label_type.model_validate(data)
    except ValidationError as error:
        raise CaseError(f"{source} is not a valid page label: {validation_problems(error, 'label')}") from error


def read_json_file(path: Path) -> object:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise CaseError(f"{path} is not valid JSON: {error}") from error


def read_case_info(directory: Path) -> CaseInfo:
    info_path = directory / CASE_FILE
    if not info_path.is_file():
        raise CaseError(f"case '{directory.name}' is missing {CASE_FILE}")
    data = read_json_file(info_path)
    if not isinstance(data, dict):
        raise CaseError(f"{info_path} must hold a JSON object")
    try:
        return CaseInfo.model_validate(data)
    except ValidationError as error:
        raise CaseError(f"{info_path} is not valid: {validation_problems(error, 'case')}") from error


def item_expectations(label: CaseLabel) -> dict[ValueKey, str]:
    return {
        ValueKey(label.item_number, name): getattr(label, name) for name in SCORED_ITEM_FIELDS if getattr(label, name) is not None
    }


def page_expectations(label: PageLabel) -> dict[ValueKey, str | None]:
    expected: dict[ValueKey, str | None] = {ValueKey(None, "page_type"): label.page_type, ValueKey(None, "sid"): label.sid}
    for item in sorted(label.items, key=lambda item: item.item_number):
        expected.update(item_expectations(item))
    return expected


def load_case(directory: Path) -> EvalCase:
    case_id = validate_case_id(directory.name)
    info = read_case_info(directory)
    image_path, label_path = directory / IMAGE_FILES[info.case_type], directory / LABEL_FILE
    for required in (image_path, label_path):
        if not required.is_file():
            raise CaseError(f"case '{case_id}' is missing {required.name}")
    label_data = read_json_file(label_path)
    if info.case_type == CASE_TYPE_CROP:
        expected = item_expectations(parse_label(label_data, str(label_path)))
    else:
        expected = page_expectations(parse_page_label(label_data, str(label_path)))
    notes_path = directory / NOTES_FILE
    notes = notes_path.read_text(encoding="utf-8").strip() if notes_path.is_file() else ""
    return EvalCase(case_id=case_id, case_type=info.case_type, image_png=image_path.read_bytes(), expected=expected, notes=notes)


def load_library(cases_directory: Path) -> CaseLibrary:
    if not cases_directory.is_dir():
        return CaseLibrary(cases=[], unlabeled=[])
    cases, unlabeled, problems = [], [], []
    for directory in sorted(path for path in cases_directory.iterdir() if path.is_dir()):
        label_path = directory / LABEL_FILE
        try:
            info = read_case_info(directory)
            if label_path.is_file() and label_is_empty(info.case_type, read_json_file(label_path)):
                unlabeled.append(directory.name)
                continue
            cases.append(load_case(directory))
        except CaseError as error:
            problems.append(str(error))
    if problems:
        raise CaseError("; ".join(problems))
    return CaseLibrary(cases=cases, unlabeled=unlabeled)


def select_item(extraction: PageExtraction, item_number: int) -> tuple[ItemExtraction | None, str]:
    if len(extraction.items) == 1:
        return extraction.items[0], ""
    matching = [item for item in extraction.items if item.item_number == item_number]
    if len(matching) == 1:
        return matching[0], f"model returned {len(extraction.items)} items, scored the one numbered {item_number}"
    return None, f"model returned {len(extraction.items)} items, expected exactly one"


@dataclass(frozen=True)
class RunScore:
    expected: dict[ValueKey, str | None]
    actual: dict[ValueKey, str | None] | None
    item_numbers_match: bool
    error: str = ""
    model_notes: dict[ValueKey, str] = field(default_factory=dict)

    @property
    def matches(self) -> dict[ValueKey, bool]:
        return {key: self.actual is not None and key in self.actual and self.actual[key] == value for key, value in self.expected.items()}

    def answer(self, key: ValueKey) -> str | None:
        return None if self.actual is None else self.actual.get(key)

    def all_match(self, wanted) -> bool:
        return self.actual is not None and self.item_numbers_match and all(
            matched for key, matched in self.matches.items() if wanted(key)
        )

    @property
    def status(self) -> str:
        if self.actual is None and self.error.startswith("not run"):
            return STATUS_NOT_RUN
        if self.actual is not None and self.item_numbers_match and all(self.matches.values()):
            return STATUS_PASS
        return STATUS_FAIL


def failed_run_score(case: EvalCase, error: str) -> RunScore:
    return RunScore(case.expected, None, False, error)


def item_answers(item: ItemExtraction, item_number: int) -> tuple[dict[ValueKey, str], dict[ValueKey, str]]:
    answers = {ValueKey(item_number, name): getattr(item, name) for name in SCORED_ITEM_FIELDS}
    notes = {ValueKey(item_number, name): getattr(item, NOTE_FIELDS[name]) for name in DIMENSION_FIELDS}
    return answers, notes


def score_crop_extraction(case: EvalCase, extraction: PageExtraction) -> RunScore:
    item_number = case.item_numbers[0]
    item, note = select_item(extraction, item_number)
    if item is None:
        return failed_run_score(case, note)
    answers, notes = item_answers(item, item_number)
    return RunScore(case.expected, answers, item.item_number == item_number, note, notes)


def score_page_extraction(case: EvalCase, extraction: PageExtraction) -> RunScore:
    answers: dict[ValueKey, str | None] = {ValueKey(None, "page_type"): extraction.page_type, ValueKey(None, "sid"): extraction.sid}
    notes: dict[ValueKey, str] = {}
    counts = Counter(item.item_number for item in extraction.items)
    for item_number in case.item_numbers:
        if counts[item_number] == 1:
            item = next(item for item in extraction.items if item.item_number == item_number)
            item_values, item_notes = item_answers(item, item_number)
            answers.update(item_values)
            notes.update(item_notes)
    expected_numbers = item_numbers_for_page_type(case.expected[ValueKey(None, "page_type")])
    item_numbers_match = sorted(counts.elements()) == expected_numbers
    return RunScore(case.expected, answers, item_numbers_match, "; ".join(response_problems(extraction)), notes)


def score_extraction(case: EvalCase, extraction: PageExtraction) -> RunScore:
    if case.case_type == CASE_TYPE_PAGE:
        return score_page_extraction(case, extraction)
    return score_crop_extraction(case, extraction)


@dataclass(frozen=True)
class Accuracy:
    correct: int
    total: int

    @property
    def fraction(self) -> float | None:
        return self.correct / self.total if self.total else None

    def as_dict(self) -> dict:
        return {"correct": self.correct, "total": self.total, "fraction": self.fraction}


def eval_cache_key(image_png: bytes, model: str, prompt_version: str = PROMPT_VERSION, repeat_index: int = 0) -> str:
    version = prompt_version if repeat_index == 0 else f"{prompt_version}|repeat{repeat_index}"
    return cache_key(image_png, model, version, SCHEMA_VERSION)


@dataclass
class CaseRun:
    case: EvalCase
    repeat_index: int
    score: RunScore
    from_cache: bool = False
    cost_usd: float = 0.0
    latency_seconds: float | None = None
    response_id: str | None = None


@dataclass
class CaseResult:
    case: EvalCase
    runs: list[CaseRun]

    @property
    def scored_runs(self) -> list[CaseRun]:
        return [run for run in self.runs if run.score.status != STATUS_NOT_RUN]

    @property
    def keys(self) -> list[ValueKey]:
        return sorted(self.case.expected, key=ValueKey.sort_key)

    def answers(self, key: ValueKey) -> list[str | None]:
        return [run.score.answer(key) for run in self.scored_runs]

    def correct_runs(self, key: ValueKey) -> int:
        return sum(run.score.matches[key] for run in self.scored_runs)

    def is_unstable(self, key: ValueKey) -> bool:
        return len(set(self.answers(key))) > 1

    @property
    def unstable_keys(self) -> list[ValueKey]:
        return [key for key in self.keys if self.is_unstable(key)]

    @property
    def runs_passed(self) -> int:
        return sum(run.score.status == STATUS_PASS for run in self.scored_runs)

    def status_where(self, run_passed) -> str:
        scored = self.scored_runs
        if not scored:
            return STATUS_NOT_RUN
        passed = sum(run_passed(run) for run in scored)
        if passed == len(scored):
            return STATUS_PASS
        if passed == 0:
            return STATUS_FAIL
        return STATUS_UNSTABLE

    @property
    def status(self) -> str:
        return self.status_where(lambda run: run.score.status == STATUS_PASS)

    @property
    def scores_mark_confidence(self) -> bool:
        return any(key.is_mark_confidence for key in self.case.expected)

    @property
    def values_status(self) -> str:
        return self.status_where(lambda run: run.score.all_match(lambda key: not key.is_mark_confidence))

    @property
    def mark_confidence_status(self) -> str | None:
        if not self.scores_mark_confidence:
            return None
        return self.status_where(lambda run: run.score.all_match(lambda key: key.is_mark_confidence))


@dataclass(frozen=True)
class ScoreSummary:
    repeat: int
    cases_passed: Accuracy
    runs_passed: Accuracy
    overall: Accuracy
    per_dimension: dict[str, Accuracy]
    mark_confidence: Accuracy
    page_fields: Accuracy
    unstable_values: int
    not_run: int

    def as_dict(self) -> dict:
        return {
            "repeat": self.repeat,
            "cases_passed": self.cases_passed.as_dict(),
            "runs_passed": self.runs_passed.as_dict(),
            "overall": self.overall.as_dict(),
            "per_dimension": {name: accuracy.as_dict() for name, accuracy in self.per_dimension.items()},
            "mark_confidence": self.mark_confidence.as_dict(),
            "page_fields": self.page_fields.as_dict(),
            "unstable_values": self.unstable_values,
            "not_run": self.not_run,
        }


def value_accuracy(results: list[CaseResult], wanted) -> Accuracy:
    correct = total = 0
    for result in results:
        for key in result.keys:
            if wanted(key):
                correct += result.correct_runs(key)
                total += len(result.scored_runs)
    return Accuracy(correct, total)


def summarize(results: list[CaseResult], repeat: int = 1) -> ScoreSummary:
    run_case_results = [result for result in results if result.scored_runs]
    per_dimension = {
        name: value_accuracy(results, lambda key, name=name: key.field == name and not key.is_page_field) for name in DIMENSION_FIELDS
    }
    return ScoreSummary(
        repeat=repeat,
        cases_passed=Accuracy(sum(result.status == STATUS_PASS for result in run_case_results), len(run_case_results)),
        runs_passed=Accuracy(sum(result.runs_passed for result in results), sum(len(result.scored_runs) for result in results)),
        overall=Accuracy(sum(accuracy.correct for accuracy in per_dimension.values()), sum(accuracy.total for accuracy in per_dimension.values())),
        per_dimension=per_dimension,
        mark_confidence=value_accuracy(results, lambda key: key.is_mark_confidence),
        page_fields=value_accuracy(results, lambda key: key.is_page_field),
        unstable_values=sum(len(result.unstable_keys) for result in results),
        not_run=sum(len(result.runs) - len(result.scored_runs) for result in results),
    )


def run_case(case: EvalCase, repeat_index: int, position: int, extractor: PageExtractor, cache: ResponseCache, spend: SpendTracker, model: str) -> CaseRun:
    key = eval_cache_key(case.image_png, model, repeat_index=repeat_index)
    cached = cache.load(key)
    if cached is not None:
        extraction = PageExtraction.model_validate(cached["extraction"])
        return CaseRun(
            case=case,
            repeat_index=repeat_index,
            score=score_extraction(case, extraction),
            from_cache=True,
            latency_seconds=cached.get("latency_seconds"),
            response_id=cached.get("response_id"),
        )

    reservation = spend.try_reserve_call()
    if reservation is None:
        return CaseRun(case=case, repeat_index=repeat_index, score=failed_run_score(case, "not run: --max-spend reached"))

    try:
        call = extractor.request_page(case.image_png, position, user_instruction=USER_INSTRUCTIONS[case.case_type])
    except Exception as error:
        spend.release_reservation(reservation)
        reason = error.reason if isinstance(error, PageFailed) else f"{type(error).__name__}: {error}"
        return CaseRun(case=case, repeat_index=repeat_index, score=failed_run_score(case, f"api call failed: {reason}"))

    response = call.response
    call_cost, _ = spend.record_call(reservation, usage_from_response(response))
    run = CaseRun(
        case=case,
        repeat_index=repeat_index,
        score=failed_run_score(case, ""),
        cost_usd=call_cost,
        latency_seconds=call.latency_seconds,
        response_id=response.id,
    )
    if response.status != "completed":
        run.score = failed_run_score(case, f"response status '{response.status}'")
        return run
    try:
        extraction = PageExtraction.model_validate_json(response.output_text)
    except ValidationError as error:
        run.score = failed_run_score(case, f"model output did not match schema: {error}")
        return run

    cache.store(
        key,
        {
            "model": model,
            "prompt_version": PROMPT_VERSION,
            "schema_version": SCHEMA_VERSION,
            "case_id": case.case_id,
            "case_type": case.case_type,
            "repeat_index": repeat_index,
            "reasoning_effort": call.effort,
            "response_id": response.id,
            "usage": usage_from_response(response).as_dict(),
            "cost_usd": call_cost,
            "latency_seconds": call.latency_seconds,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "extraction": extraction.model_dump(),
        },
    )
    run.score = score_extraction(case, extraction)
    return run


def case_run_plan(cases: list[EvalCase], repeat: int) -> list[tuple[EvalCase, int]]:
    return [(case, repeat_index) for case in cases for repeat_index in range(repeat)]


def run_cases(
    cases: list[EvalCase],
    extractor: PageExtractor,
    cache: ResponseCache,
    spend: SpendTracker,
    model: str,
    workers: int,
    repeat: int = 1,
) -> list[CaseResult]:
    plan = case_run_plan(cases, repeat)
    with ThreadPoolExecutor(max_workers=max(1, workers)) as executor:
        futures = [
            executor.submit(run_case, case, repeat_index, position, extractor, cache, spend, model)
            for position, (case, repeat_index) in enumerate(plan, start=1)
        ]
        runs = [future.result() for future in futures]
    return [CaseResult(case, [run for run in runs if run.case is case]) for case in cases]


@dataclass(frozen=True)
class StatusChange:
    case_id: str
    before: str
    after: str


def previous_results_path(results_directory: Path) -> Path | None:
    if not results_directory.is_dir():
        return None
    candidates = sorted(path for path in results_directory.iterdir() if path.is_file() and RESULTS_FILE_PATTERN.match(path.name))
    return candidates[-1] if candidates else None


def previous_statuses(results_path: Path | None) -> dict[str, str] | None:
    if results_path is None:
        return None
    data = json.loads(results_path.read_text(encoding="utf-8"))
    return {case["case_id"]: case["status"] for case in data.get("cases", [])}


def status_changes(before: dict[str, str] | None, after: dict[str, str]) -> list[StatusChange]:
    if before is None:
        return []
    changes = []
    for case_id in sorted(set(before) | set(after)):
        old, new = before.get(case_id, "new"), after.get(case_id, "removed")
        if old != new:
            changes.append(StatusChange(case_id, old, new))
    return changes


def case_statuses(results: list[CaseResult]) -> dict[str, str]:
    return {result.case.case_id: result.status for result in results}


def value_record(result: CaseResult, key: ValueKey) -> dict:
    return {
        "value": key.label,
        "expected": result.case.expected[key],
        "correct_runs": result.correct_runs(key),
        "scored_runs": len(result.scored_runs),
        "unstable": result.is_unstable(key),
        "answers": dict(Counter("null" if answer is None else answer for answer in result.answers(key))),
    }


def run_record(run: CaseRun) -> dict:
    score = run.score
    return {
        "repeat_index": run.repeat_index,
        "status": score.status,
        "actual": None if score.actual is None else {key.label: value for key, value in sorted(score.actual.items(), key=lambda pair: pair[0].sort_key())},
        "model_notes": {key.label: note for key, note in sorted(score.model_notes.items(), key=lambda pair: pair[0].sort_key())},
        "item_numbers_match": score.item_numbers_match,
        "error": score.error,
        "from_cache": run.from_cache,
        "cost_usd": run.cost_usd,
        "latency_seconds": run.latency_seconds,
        "response_id": run.response_id,
    }


@dataclass
class EvalReport:
    timestamp: str
    model: str
    results: list[CaseResult]
    summary: ScoreSummary
    changes: list[StatusChange]
    previous_results: str | None
    unlabeled: list[str] = field(default_factory=list)

    @property
    def runs(self) -> list[CaseRun]:
        return [run for result in self.results for run in result.runs]

    @property
    def total_cost_usd(self) -> float:
        return sum(run.cost_usd for run in self.runs)

    @property
    def live_latencies(self) -> list[float]:
        return [run.latency_seconds for run in self.runs if not run.from_cache and run.latency_seconds is not None]

    @property
    def all_latencies(self) -> list[float]:
        return [run.latency_seconds for run in self.runs if run.latency_seconds is not None]

    def as_dict(self) -> dict:
        live, recorded = self.live_latencies, self.all_latencies
        return {
            "timestamp": self.timestamp,
            "model": self.model,
            "prompt_version": PROMPT_VERSION,
            "schema_version": SCHEMA_VERSION,
            "summary": self.summary.as_dict(),
            "total_cost_usd": self.total_cost_usd,
            "api_calls": sum(1 for run in self.runs if not run.from_cache and run.response_id is not None),
            "cache_hits": sum(1 for run in self.runs if run.from_cache),
            "mean_latency_seconds_this_run": sum(live) / len(live) if live else None,
            "mean_latency_seconds_recorded": sum(recorded) / len(recorded) if recorded else None,
            "previous_results": self.previous_results,
            "status_changes": [{"case_id": change.case_id, "before": change.before, "after": change.after} for change in self.changes],
            "unlabeled_cases": self.unlabeled,
            "cases": [
                {
                    "case_id": result.case.case_id,
                    "case_type": result.case.case_type,
                    "status": result.status,
                    "values_status": result.values_status,
                    "mark_confidence_status": result.mark_confidence_status,
                    "runs_passed": result.runs_passed,
                    "scored_runs": len(result.scored_runs),
                    "unstable_values": [key.label for key in result.unstable_keys],
                    "values": [value_record(result, key) for key in result.keys],
                    "runs": [run_record(run) for run in result.runs],
                    "notes": result.case.notes,
                }
                for result in self.results
            ],
        }


def results_timestamp(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def write_report(report: EvalReport, results_directory: Path) -> Path:
    results_directory.mkdir(parents=True, exist_ok=True)
    path = results_directory / f"{report.timestamp}_{PROMPT_VERSION}.json"
    path.write_text(json.dumps(report.as_dict(), indent=2), encoding="utf-8")
    return path
