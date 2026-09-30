import json
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from pydantic import BaseModel, ConfigDict, ValidationError, field_validator

from rassi.cache import ResponseCache, cache_key
from rassi.config import ALL_ITEM_NUMBERS
from rassi.extract import PageExtractor, PageFailed, usage_from_response
from rassi.prompt import PROMPT_VERSION
from rassi.schema import SCHEMA_VERSION, ItemExtraction, PageExtraction, ReasonValue, ScaleValue
from rassi.spend import SpendTracker

CROP_FILE = "crop.png"
LABEL_FILE = "label.json"
NOTES_FILE = "notes.md"
DIMENSION_FIELDS = ("agreement", "reason", "confidence")
CASE_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
RESULTS_FILE_PATTERN = re.compile(r"^\d{8}T\d{6}Z_.+\.json$")

STATUS_PASS = "pass"
STATUS_FAIL = "fail"
STATUS_NOT_RUN = "not_run"


class CaseError(ValueError):
    pass


class CaseLabel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    item_number: int
    agreement: ScaleValue
    reason: ReasonValue
    confidence: ScaleValue

    @field_validator("item_number")
    @classmethod
    def item_number_in_range(cls, value: int) -> int:
        if value not in ALL_ITEM_NUMBERS:
            raise ValueError(f"must be {ALL_ITEM_NUMBERS[0]} to {ALL_ITEM_NUMBERS[-1]}")
        return value


@dataclass(frozen=True)
class EvalCase:
    case_id: str
    label: CaseLabel
    crop_png: bytes
    notes: str


@dataclass(frozen=True)
class CaseLibrary:
    cases: list[EvalCase]
    unlabeled: list[str]


def validate_case_id(case_id: str) -> str:
    if not CASE_ID_PATTERN.match(case_id):
        raise CaseError(f"case id '{case_id}' must use lowercase letters, digits, '-' and '_' only, starting with a letter or digit")
    return case_id


def empty_label(item_number: int) -> dict:
    return {"item_number": item_number, "agreement": None, "reason": None, "confidence": None}


def label_is_empty(data: dict) -> bool:
    return all(data.get(name) is None for name in DIMENSION_FIELDS)


def parse_label(data: object, source: str) -> CaseLabel:
    if not isinstance(data, dict):
        raise CaseError(f"{source} must hold a JSON object")
    try:
        return CaseLabel.model_validate(data)
    except ValidationError as error:
        problems = "; ".join(f"{'.'.join(str(part) for part in problem['loc']) or 'label'}: {problem['msg']}" for problem in error.errors())
        raise CaseError(f"{source} is not a valid label: {problems}") from error


def read_label_data(label_path: Path) -> object:
    try:
        return json.loads(label_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise CaseError(f"{label_path} is not valid JSON: {error}") from error


def load_case(directory: Path) -> EvalCase:
    case_id = validate_case_id(directory.name)
    crop_path, label_path = directory / CROP_FILE, directory / LABEL_FILE
    for required in (crop_path, label_path):
        if not required.is_file():
            raise CaseError(f"case '{case_id}' is missing {required.name}")
    label = parse_label(read_label_data(label_path), str(label_path))
    notes_path = directory / NOTES_FILE
    notes = notes_path.read_text(encoding="utf-8").strip() if notes_path.is_file() else ""
    return EvalCase(case_id=case_id, label=label, crop_png=crop_path.read_bytes(), notes=notes)


def load_library(cases_directory: Path) -> CaseLibrary:
    if not cases_directory.is_dir():
        return CaseLibrary(cases=[], unlabeled=[])
    cases, unlabeled, problems = [], [], []
    for directory in sorted(path for path in cases_directory.iterdir() if path.is_dir()):
        label_path = directory / LABEL_FILE
        try:
            if label_path.is_file():
                data = read_label_data(label_path)
                if isinstance(data, dict) and label_is_empty(data):
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
class CaseScore:
    case_id: str
    expected: dict
    actual: dict | None
    matches: dict[str, bool]
    item_number_matches: bool
    error: str = ""

    @property
    def status(self) -> str:
        if self.actual is None and self.error.startswith("not run"):
            return STATUS_NOT_RUN
        if self.actual is not None and self.item_number_matches and all(self.matches.values()):
            return STATUS_PASS
        return STATUS_FAIL


def item_values(item: ItemExtraction | CaseLabel) -> dict:
    return {"item_number": item.item_number, **{name: getattr(item, name) for name in DIMENSION_FIELDS}}


def score_case(case_id: str, label: CaseLabel, item: ItemExtraction | None, error: str = "") -> CaseScore:
    expected = item_values(label)
    if item is None:
        return CaseScore(case_id, expected, None, {name: False for name in DIMENSION_FIELDS}, False, error)
    actual = item_values(item)
    matches = {name: actual[name] == expected[name] for name in DIMENSION_FIELDS}
    return CaseScore(case_id, expected, actual, matches, actual["item_number"] == expected["item_number"], error)


@dataclass(frozen=True)
class Accuracy:
    correct: int
    total: int

    @property
    def fraction(self) -> float | None:
        return self.correct / self.total if self.total else None

    def as_dict(self) -> dict:
        return {"correct": self.correct, "total": self.total, "fraction": self.fraction}


@dataclass(frozen=True)
class ScoreSummary:
    cases_passed: Accuracy
    overall: Accuracy
    per_dimension: dict[str, Accuracy]
    not_run: int

    def as_dict(self) -> dict:
        return {
            "cases_passed": self.cases_passed.as_dict(),
            "overall": self.overall.as_dict(),
            "per_dimension": {name: accuracy.as_dict() for name, accuracy in self.per_dimension.items()},
            "not_run": self.not_run,
        }


def summarize(scores: list[CaseScore]) -> ScoreSummary:
    scored = [score for score in scores if score.status != STATUS_NOT_RUN]
    per_dimension = {
        name: Accuracy(sum(score.matches[name] for score in scored), len(scored)) for name in DIMENSION_FIELDS
    }
    return ScoreSummary(
        cases_passed=Accuracy(sum(score.status == STATUS_PASS for score in scored), len(scored)),
        overall=Accuracy(sum(accuracy.correct for accuracy in per_dimension.values()), len(scored) * len(DIMENSION_FIELDS)),
        per_dimension=per_dimension,
        not_run=len(scores) - len(scored),
    )


def eval_cache_key(crop_png: bytes, model: str, prompt_version: str = PROMPT_VERSION) -> str:
    return cache_key(crop_png, model, prompt_version, SCHEMA_VERSION)


@dataclass
class CaseRun:
    case: EvalCase
    score: CaseScore
    from_cache: bool = False
    cost_usd: float = 0.0
    latency_seconds: float | None = None
    response_id: str | None = None


def run_case(case: EvalCase, position: int, extractor: PageExtractor, cache: ResponseCache, spend: SpendTracker, model: str) -> CaseRun:
    key = eval_cache_key(case.crop_png, model)
    cached = cache.load(key)
    if cached is not None:
        extraction = PageExtraction.model_validate(cached["extraction"])
        item, note = select_item(extraction, case.label.item_number)
        return CaseRun(
            case=case,
            score=score_case(case.case_id, case.label, item, note),
            from_cache=True,
            latency_seconds=cached.get("latency_seconds"),
            response_id=cached.get("response_id"),
        )

    reservation = spend.try_reserve_call()
    if reservation is None:
        return CaseRun(case=case, score=score_case(case.case_id, case.label, None, "not run: --max-spend reached"))

    try:
        call = extractor.request_page(case.crop_png, position)
    except Exception as error:
        spend.release_reservation(reservation)
        reason = error.reason if isinstance(error, PageFailed) else f"{type(error).__name__}: {error}"
        return CaseRun(case=case, score=score_case(case.case_id, case.label, None, f"api call failed: {reason}"))

    response = call.response
    call_cost, _ = spend.record_call(reservation, usage_from_response(response))
    run = CaseRun(case=case, score=score_case(case.case_id, case.label, None, ""), cost_usd=call_cost, latency_seconds=call.latency_seconds, response_id=response.id)
    if response.status != "completed":
        run.score = score_case(case.case_id, case.label, None, f"response status '{response.status}'")
        return run
    try:
        extraction = PageExtraction.model_validate_json(response.output_text)
    except ValidationError as error:
        run.score = score_case(case.case_id, case.label, None, f"model output did not match schema: {error}")
        return run

    cache.store(
        key,
        {
            "model": model,
            "prompt_version": PROMPT_VERSION,
            "schema_version": SCHEMA_VERSION,
            "case_id": case.case_id,
            "reasoning_effort": call.effort,
            "response_id": response.id,
            "usage": usage_from_response(response).as_dict(),
            "cost_usd": call_cost,
            "latency_seconds": call.latency_seconds,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "extraction": extraction.model_dump(),
        },
    )
    item, note = select_item(extraction, case.label.item_number)
    run.score = score_case(case.case_id, case.label, item, note)
    return run


def run_cases(cases: list[EvalCase], extractor: PageExtractor, cache: ResponseCache, spend: SpendTracker, model: str, workers: int) -> list[CaseRun]:
    with ThreadPoolExecutor(max_workers=max(1, workers)) as executor:
        futures = [
            executor.submit(run_case, case, position, extractor, cache, spend, model)
            for position, case in enumerate(cases, start=1)
        ]
        return [future.result() for future in futures]


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


def status_changes(before: dict[str, str] | None, scores: list[CaseScore]) -> list[StatusChange]:
    if before is None:
        return []
    after = {score.case_id: score.status for score in scores}
    changes = []
    for case_id in sorted(set(before) | set(after)):
        old, new = before.get(case_id, "new"), after.get(case_id, "removed")
        if old != new:
            changes.append(StatusChange(case_id, old, new))
    return changes


@dataclass
class EvalReport:
    timestamp: str
    model: str
    runs: list[CaseRun]
    summary: ScoreSummary
    changes: list[StatusChange]
    previous_results: str | None
    unlabeled: list[str] = field(default_factory=list)

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
                    "case_id": run.case.case_id,
                    "status": run.score.status,
                    "expected": run.score.expected,
                    "actual": run.score.actual,
                    "matches": run.score.matches,
                    "item_number_matches": run.score.item_number_matches,
                    "error": run.score.error,
                    "from_cache": run.from_cache,
                    "cost_usd": run.cost_usd,
                    "latency_seconds": run.latency_seconds,
                    "response_id": run.response_id,
                    "notes": run.case.notes,
                }
                for run in self.runs
            ],
        }


def results_timestamp(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def write_report(report: EvalReport, results_directory: Path) -> Path:
    results_directory.mkdir(parents=True, exist_ok=True)
    path = results_directory / f"{report.timestamp}_{PROMPT_VERSION}.json"
    path.write_text(json.dumps(report.as_dict(), indent=2), encoding="utf-8")
    return path
