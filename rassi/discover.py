from dataclasses import dataclass, field
from pathlib import Path

from rassi.config import KNOWN_TEACHER_CODES
from rassi.filename import FilenameError, SourceFileInfo, parse_source_filename
from rassi.paths import DataLayout, display_path, output_paths


class InputError(Exception):
    pass


@dataclass(frozen=True)
class InputFile:
    path: Path
    source: SourceFileInfo


@dataclass(frozen=True)
class InputProblem:
    path: Path
    message: str

    def describe(self) -> str:
        return f"{display_path(self.path)}: {self.message}"


@dataclass
class Discovery:
    files: list[InputFile] = field(default_factory=list)
    problems: list[InputProblem] = field(default_factory=list)
    warnings: list[InputProblem] = field(default_factory=list)

    def extend(self, other: "Discovery") -> None:
        self.files.extend(other.files)
        self.problems.extend(other.problems)
        self.warnings.extend(other.warnings)


def is_hidden(path: Path) -> bool:
    return path.name.startswith(".")


def visible_entries(directory: Path) -> list[Path]:
    return sorted((entry for entry in directory.iterdir() if not is_hidden(entry)), key=lambda entry: entry.name)


def input_sort_key(input_file: InputFile) -> tuple:
    return (input_file.source.teacher_code, int(input_file.source.period), input_file.path.name)


def validate_pdf(path: Path) -> InputFile | InputProblem:
    try:
        source = parse_source_filename(path)
    except FilenameError as error:
        return InputProblem(path, f"skipped, {error}")
    folder_name = path.parent.name
    if source.teacher_code != folder_name:
        return InputProblem(
            path,
            f"skipped, teacher code '{source.teacher_code}' in the filename does not match the folder '{folder_name}' it sits in",
        )
    return InputFile(path, source)


def unknown_teacher_warnings(files: list[InputFile]) -> list[InputProblem]:
    return [
        InputProblem(
            input_file.path,
            f"teacher code '{input_file.source.teacher_code}' is not in config.KNOWN_TEACHER_CODES; processing anyway, check it is not a typo",
        )
        for input_file in files
        if input_file.source.teacher_code not in KNOWN_TEACHER_CODES
    ]


def scan_teacher_folder(folder: Path, empty_is_problem: bool) -> Discovery:
    discovery = Discovery()
    entries = visible_entries(folder)
    pdf_entries = [entry for entry in entries if entry.is_file() and entry.suffix.lower() == ".pdf"]
    if not pdf_entries:
        empty_notice = InputProblem(folder, "skipped, teacher folder contains no PDFs")
        if empty_is_problem:
            discovery.problems.append(empty_notice)
        else:
            discovery.warnings.append(empty_notice)
    for entry in entries:
        if entry.is_dir():
            discovery.problems.append(InputProblem(entry, "skipped, nested folders inside a teacher folder are not read"))
            continue
        outcome = validate_pdf(entry)
        if isinstance(outcome, InputFile):
            discovery.files.append(outcome)
        else:
            discovery.problems.append(outcome)
    discovery.files.sort(key=input_sort_key)
    discovery.warnings.extend(unknown_teacher_warnings(discovery.files))
    return discovery


def require_input_root(layout: DataLayout) -> list[Path]:
    root = layout.input_root
    if not root.is_dir():
        raise InputError(f"Input folder {display_path(root)} does not exist. Create it and add one folder per teacher code.")
    entries = visible_entries(root)
    if not entries:
        raise InputError(f"Input folder {display_path(root)} is empty. Add one folder per teacher code, for example {display_path(root / '020421')}.")
    return entries


def discover_all(layout: DataLayout) -> Discovery:
    discovery = Discovery()
    for entry in require_input_root(layout):
        if entry.is_dir():
            discovery.extend(scan_teacher_folder(entry, empty_is_problem=False))
        else:
            discovery.problems.append(InputProblem(entry, "skipped, files must sit inside a teacher folder, not directly in the input folder"))
    discovery.files.sort(key=input_sort_key)
    return discovery


def discover_teacher(layout: DataLayout, teacher_code: str) -> Discovery:
    if not teacher_code or Path(teacher_code).name != teacher_code or is_hidden(Path(teacher_code)):
        raise InputError(f"'{teacher_code}' is not a valid teacher code; pass the folder name only, for example 020421")
    require_input_root(layout)
    folder = layout.teacher_input_directory(teacher_code)
    if not folder.is_dir():
        raise InputError(f"Teacher folder {display_path(folder)} does not exist")
    return scan_teacher_folder(folder, empty_is_problem=True)


def discover_file(layout: DataLayout, pdf_path: Path) -> Discovery:
    if not pdf_path.is_file():
        raise InputError(f"{display_path(pdf_path)} does not exist")
    input_root = layout.input_root.resolve()
    resolved = pdf_path.resolve()
    if resolved.parent.parent != input_root:
        raise InputError(
            f"{display_path(pdf_path)} is not inside {display_path(layout.input_root)}. "
            f"--file must point at {display_path(layout.input_root)}/<teacher_code>/<teacher_code>_PD<period>_RASSI.pdf"
        )
    outcome = validate_pdf(resolved)
    if isinstance(outcome, InputFile):
        return Discovery(files=[outcome], warnings=unknown_teacher_warnings([outcome]))
    return Discovery(problems=[outcome])


def output_collisions(files: list[InputFile], layout: DataLayout) -> list[str]:
    claimed: dict[str, InputFile] = {}
    collisions = []
    for input_file in files:
        paths = output_paths(input_file.source, layout)
        key = str(paths.directory / paths.stem).casefold()
        if key in claimed:
            collisions.append(
                f"{display_path(claimed[key].path)} and {display_path(input_file.path)} would both write outputs "
                f"named {paths.stem} in {display_path(paths.directory)}"
            )
        else:
            claimed[key] = input_file
    return collisions
