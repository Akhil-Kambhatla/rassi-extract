import argparse
import threading
import time

import numpy as np
import pymupdf

from rassi import eval_cli, evaluation, extract
from rassi.config import DEFAULT_MODEL, IMAGE_DETAIL, RENDER_DPI
from rassi.crop import crop_rendered_item, encode_png
from rassi.paths import DEFAULT_LAYOUT
from rassi.prompt import PROMPT_VERSION, USER_INSTRUCTION

DETAIL_INSTRUCTION = (
    "The first image is the full page. The remaining images are overlapping detail views of the same page, "
    "not additional pages. Use them to inspect small or broken strokes. Return each printed item once. "
    "Read item numbers from the printed text and keep all row interpretation rules."
)

AUDIT_INSTRUCTION = (
    "\n\nFINAL VISUAL AUDIT\n"
    "Before finalizing each row, inspect every valid option, including the printed numeral or letter itself, "
    "for small, faint, broken, or overtraced marks. Do not stop after finding the most prominent mark. "
    "For each candidate mark, distinguish selection strokes from cancellation strokes, stray strokes, "
    "and vertical bleed using the existing rules. Describe the visible evidence for including or excluding "
    "each candidate in that row's note. A stroke through printed words is not by itself evidence that "
    "an existing selection mark was crossed out. Check whether a selection was actually cancelled. "
    "Before returning blank, verify that there is no selection mark, including over the printed words. "
    "Compare the final value against the note: include every separately resolved selection described there, "
    "and exclude every mark that the note discounts. If the note and value disagree, inspect the row again "
    "and correct the interpretation from the image. "
    "Preserve ambiguous for marks that cannot be resolved and never infer an answer from an expected pattern."
)


def detail_views(png_bytes: bytes) -> list[bytes]:
    pixmap = pymupdf.Pixmap(png_bytes)
    rgb = np.frombuffer(pixmap.samples, dtype=np.uint8).reshape(pixmap.height, pixmap.width, pixmap.n)
    if pixmap.n != 3:
        raise ValueError("detail views require an RGB PNG")
    return [encode_png(rgb[round(top * pixmap.height):round(bottom * pixmap.height)]) for top, bottom in ((0, .45), (.275, .725), (.55, 1))]


def item_library(cases_directory):
    library = evaluation.load_library(cases_directory)
    cases = []
    for case in library.cases:
        if case.case_type == evaluation.CASE_TYPE_CROP:
            cases.append(case)
            continue
        info = evaluation.read_case_info(cases_directory / case.case_id)
        pixmap = pymupdf.Pixmap(case.image_png)
        rgb = np.frombuffer(pixmap.samples, dtype=np.uint8).reshape(pixmap.height, pixmap.width, pixmap.n)
        for item_number in case.item_numbers:
            crop = crop_rendered_item(rgb, info.page_number, item_number, RENDER_DPI)
            expected = {key: value for key, value in case.expected.items() if key.item_number == item_number}
            cases.append(evaluation.EvalCase(
                f"{case.case_id}-q{item_number:02}", evaluation.CASE_TYPE_CROP, crop.png_bytes, expected, case.notes
            ))
    return evaluation.CaseLibrary(cases, library.unlabeled)


def configure_variant(variant: str) -> None:
    version = f"{PROMPT_VERSION}-reliability-{variant}-v1"
    original_key = evaluation.eval_cache_key
    evaluation.eval_cache_key = lambda image_png, model, repeat_index=0: original_key(
        image_png, model, prompt_version=version, repeat_index=repeat_index
    )
    eval_cli.eval_cache_key = evaluation.eval_cache_key
    evaluation.PROMPT_VERSION = version
    eval_cli.PROMPT_VERSION = version
    original_input = extract.build_input
    if variant == "details":
        def build_input(png_bytes: bytes, user_instruction: str = USER_INSTRUCTION) -> list[dict]:
            request = original_input(png_bytes, user_instruction)
            if user_instruction == USER_INSTRUCTION:
                request[0]["content"].append({"type": "input_text", "text": DETAIL_INSTRUCTION})
                request[0]["content"].extend(
                    {"type": "input_image", "image_url": extract.png_data_url(view), "detail": IMAGE_DETAIL}
                    for view in detail_views(png_bytes)
                )
            return request

        extract.build_input = build_input
    if variant == "audit":
        extract.SYSTEM_INSTRUCTIONS += AUDIT_INSTRUCTION
    if variant == "reasoning-low":
        extract.REASONING_EFFORT_PREFERENCE = ["low"]
    if variant == "items":
        eval_cli.load_library = item_library


def pace_requests(interval: float) -> None:
    original_create = extract.PageExtractor.create_with_retries
    lock = threading.Lock()
    next_start = 0.0

    def create_with_retries(self, request: dict, page_number: int):
        nonlocal next_start
        with lock:
            time.sleep(max(0, next_start - time.monotonic()))
            next_start = time.monotonic() + interval
        return original_create(self, request, page_number)

    extract.PageExtractor.create_with_retries = create_with_retries


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--variant", choices=("details", "audit", "reasoning-low", "items"), required=True)
    parser.add_argument("--repeat", type=int, default=10)
    parser.add_argument("--max-spend", type=float, default=6)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--interval", type=float)
    arguments = parser.parse_args()
    configure_variant(arguments.variant)
    interval = arguments.interval if arguments.interval is not None else (6 if arguments.variant.startswith("details") else 2)
    if interval > 0:
        pace_requests(interval)
    return eval_cli.run_eval_run(arguments, DEFAULT_LAYOUT)


if __name__ == "__main__":
    raise SystemExit(main())
