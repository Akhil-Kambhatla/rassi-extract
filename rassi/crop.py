from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import numpy as np
import pymupdf

from rassi.config import ALL_ITEM_NUMBERS, CONTINUATION_ITEM_NUMBERS, HEADER_ITEM_NUMBERS, ITEMS_PER_PAGE, RENDER_DPI

DARK_PIXEL_THRESHOLD = 225
SOLID_PIXEL_THRESHOLD = 100
BLUR_RADIUS_INCHES = 0.007
BACKGROUND_RADIUS_INCHES = 0.25
ADAPTIVE_DARK_RATIO = 0.75
BORDER_CLOSING_INCHES = 0.08
RULE_MIN_WIDTH_FRACTION = 0.5
RULE_MAX_GAP_INCHES = 0.03
RULE_MAX_THICKNESS_INCHES = 0.08
RULE_SKEW_TOLERANCE_INCHES = 0.01
BLOCK_MIN_THICKNESS_INCHES = 0.2
WALL_MIN_COVERAGE = 0.85
WALL_MIN_SPAN_FRACTION = 0.5
WALL_SEARCH_MARGIN_INCHES = 0.05
ROW_INSET_INCHES = 0.02
RULES_PER_TABLE = 4
QUESTION_ALLOWANCE_ROWS = 1.6
CROP_PADDING_INCHES = 0.04
QUESTION_NUMBER_INDENT_INCHES = 0.4
MAX_SKEW_DEGREES = 2.0
SKEW_STEP_DEGREES = 0.05
SKEW_ESTIMATE_DOWNSCALE = 4
UNACCOUNTED_MIN_HEIGHT_INCHES = 0.9
REGION_CLEARANCE_INCHES = 0.1
TABLE_HEIGHT_TOLERANCE = 0.12
TABLE_WIDTH_TOLERANCE = 0.08


class TableDetectionError(RuntimeError):
    pass


@dataclass(frozen=True)
class TracingStrategy:
    name: str
    mask: Callable[[np.ndarray, int], np.ndarray]


@dataclass(frozen=True)
class HorizontalBand:
    top: int
    bottom: int
    left: int
    right: int

    @property
    def thickness(self) -> int:
        return self.bottom - self.top + 1


@dataclass(frozen=True)
class VerticalRegion:
    top: int
    bottom: int

    def describe(self) -> str:
        return f"rows {self.top} to {self.bottom}"


@dataclass(frozen=True)
class ItemTable:
    rules: tuple[HorizontalBand, ...]
    left: int
    right: int
    slope: float = 0.0
    strategy_name: str = "global threshold"

    @property
    def top(self) -> int:
        return self.rules[0].top

    @property
    def bottom(self) -> int:
        return self.rules[-1].bottom

    @property
    def height(self) -> int:
        return self.bottom - self.top + 1

    @property
    def width(self) -> int:
        return self.right - self.left + 1

    @property
    def row_height(self) -> float:
        return (self.rules[-1].top - self.rules[0].top) / (len(self.rules) - 1)

    @property
    def skew_degrees(self) -> float:
        return float(np.degrees(np.arctan(self.slope)))

    def describe(self) -> str:
        return f"rows {self.top} to {self.bottom}"


@dataclass(frozen=True)
class PageLayout:
    tables: list[ItemTable]
    blocks: list[HorizontalBand]
    width: int
    height: int
    unaccounted: tuple[VerticalRegion, ...] = ()
    recovered: tuple[ItemTable, ...] = ()

    @property
    def is_header_page(self) -> bool:
        return bool(self.tables) and any(block.bottom < self.tables[0].top for block in self.blocks)


@dataclass(frozen=True)
class CropBox:
    left: int
    top: int
    right: int
    bottom: int


@dataclass(frozen=True)
class ItemCrop:
    png_bytes: bytes
    box: CropBox
    page_is_header: bool
    notes: tuple[str, ...] = ()


def pixels(inches: float, dpi: int) -> int:
    return max(1, round(inches * dpi))


def render_page_rgb(pdf_path: Path, page_number: int, dpi: int) -> np.ndarray:
    with pymupdf.open(pdf_path) as document:
        if not 1 <= page_number <= document.page_count:
            raise TableDetectionError(f"page {page_number} is outside the document, which has {document.page_count} pages")
        pixmap = document[page_number - 1].get_pixmap(dpi=dpi, alpha=False, colorspace=pymupdf.csRGB)
    samples = np.frombuffer(pixmap.samples, dtype=np.uint8).reshape(pixmap.height, pixmap.stride)
    return samples[:, : pixmap.width * 3].reshape(pixmap.height, pixmap.width, 3).copy()


def box_blur(gray: np.ndarray, radius: int) -> np.ndarray:
    size = 2 * radius + 1
    padded = np.pad(gray, radius, mode="edge")
    summed = np.zeros((padded.shape[0] + 1, padded.shape[1] + 1))
    summed[1:, 1:] = padded.cumsum(axis=0).cumsum(axis=1)
    window_sums = summed[size:, size:] - summed[:-size, size:] - summed[size:, :-size] + summed[:-size, :-size]
    return window_sums / (size * size)


def dark_mask(rgb: np.ndarray, dpi: int) -> np.ndarray:
    return box_blur(rgb.mean(axis=2), pixels(BLUR_RADIUS_INCHES, dpi)) < DARK_PIXEL_THRESHOLD


def solid_mask(rgb: np.ndarray, dpi: int) -> np.ndarray:
    return box_blur(rgb.mean(axis=2), pixels(BLUR_RADIUS_INCHES, dpi)) < SOLID_PIXEL_THRESHOLD


def adaptive_dark_mask(rgb: np.ndarray, dpi: int) -> np.ndarray:
    gray = rgb.mean(axis=2)
    smoothed = box_blur(gray, pixels(BLUR_RADIUS_INCHES, dpi))
    background = box_blur(gray, pixels(BACKGROUND_RADIUS_INCHES, dpi))
    return smoothed < background * ADAPTIVE_DARK_RATIO


def centered_window_count(mask: np.ndarray, radius: int, axis: int) -> np.ndarray:
    counts = np.moveaxis(mask, axis, 0).astype(np.int32)
    padding = [(radius + 1, radius)] + [(0, 0)] * (counts.ndim - 1)
    cumulative = np.pad(counts, padding).cumsum(axis=0)
    window = 2 * radius + 1
    return np.moveaxis(cumulative[window:] - cumulative[:-window], 0, axis)


def close_along_axis(mask: np.ndarray, length: int, axis: int) -> np.ndarray:
    radius = max(1, length // 2)
    dilated = centered_window_count(mask, radius, axis) > 0
    return centered_window_count(dilated, radius, axis) == 2 * radius + 1


def close_borders(mask: np.ndarray, dpi: int) -> np.ndarray:
    length = pixels(BORDER_CLOSING_INCHES, dpi)
    return close_along_axis(close_along_axis(mask, length, axis=1), length, axis=0)


def closed_adaptive_mask(rgb: np.ndarray, dpi: int) -> np.ndarray:
    return close_borders(adaptive_dark_mask(rgb, dpi), dpi)


GLOBAL_THRESHOLD_TRACING = TracingStrategy("global threshold", dark_mask)
ADAPTIVE_TRACING = TracingStrategy("adaptive threshold", adaptive_dark_mask)
ADAPTIVE_CLOSED_TRACING = TracingStrategy("adaptive threshold with border closing", closed_adaptive_mask)
RECOVERY_STRATEGIES = (GLOBAL_THRESHOLD_TRACING, ADAPTIVE_TRACING, ADAPTIVE_CLOSED_TRACING)
STRATEGIES_BY_NAME = {strategy.name: strategy for strategy in RECOVERY_STRATEGIES}


def longest_run(row: np.ndarray, max_gap: int) -> tuple[int, int] | None:
    padded = np.concatenate(([False], row, [False]))
    changes = np.flatnonzero(np.diff(padded.astype(np.int8)))
    if changes.size == 0:
        return None
    starts, ends = changes[0::2], changes[1::2] - 1
    best_start, best_end = starts[0], ends[0]
    run_start, run_end = starts[0], ends[0]
    for start, end in zip(starts[1:], ends[1:]):
        if start - run_end - 1 <= max_gap:
            run_end = end
        else:
            run_start, run_end = start, end
        if run_end - run_start > best_end - best_start:
            best_start, best_end = run_start, run_end
    return int(best_start), int(best_end)


def spread_vertically(dark: np.ndarray, reach: int) -> np.ndarray:
    spread = dark.copy()
    for offset in range(1, reach + 1):
        spread[offset:] |= dark[:-offset]
        spread[:-offset] |= dark[offset:]
    return spread


def shear_rows(image: np.ndarray, slope: float) -> np.ndarray:
    height, width = image.shape[:2]
    shifts = np.round((np.arange(width) - width / 2) * slope).astype(np.int32)
    source_rows = np.clip(np.arange(height, dtype=np.int32)[:, None] + shifts[None, :], 0, height - 1)
    return image[source_rows, np.arange(width, dtype=np.int32)[None, :]]


def estimate_skew_slope(dark: np.ndarray) -> float:
    factor = SKEW_ESTIMATE_DOWNSCALE
    height, width = dark.shape
    small = dark[: height - height % factor, : width - width % factor]
    small = small.reshape(height // factor, factor, width // factor, factor).mean(axis=(1, 3))
    best_slope, best_score = 0.0, -1.0
    steps = round(MAX_SKEW_DEGREES / SKEW_STEP_DEGREES)
    for step in range(-steps, steps + 1):
        slope = float(np.tan(np.radians(step * SKEW_STEP_DEGREES)))
        profile = shear_rows(small, slope).sum(axis=1)
        score = float(np.square(profile).sum())
        if score > best_score:
            best_slope, best_score = slope, score
    return best_slope


def horizontal_bands(dark: np.ndarray, dpi: int) -> list[HorizontalBand]:
    height, width = dark.shape
    min_width = width * RULE_MIN_WIDTH_FRACTION
    max_gap = pixels(RULE_MAX_GAP_INCHES, dpi)
    tolerant = spread_vertically(dark, pixels(RULE_SKEW_TOLERANCE_INCHES, dpi))
    candidate_rows = np.flatnonzero(tolerant.sum(axis=1) >= min_width)
    bands: list[HorizontalBand] = []
    for y in candidate_rows:
        run = longest_run(tolerant[y], max_gap)
        if run is None or run[1] - run[0] + 1 < min_width:
            continue
        left, right = run
        if bands and y - bands[-1].bottom <= 2:
            previous = bands[-1]
            bands[-1] = HorizontalBand(previous.top, int(y), min(previous.left, left), max(previous.right, right))
        else:
            bands.append(HorizontalBand(int(y), int(y), left, right))
    return bands


def thick_blocks(bands: list[HorizontalBand], dpi: int) -> list[HorizontalBand]:
    return [band for band in bands if band.thickness >= pixels(BLOCK_MIN_THICKNESS_INCHES, dpi)]


def wall_columns(dark: np.ndarray, upper: HorizontalBand, lower: HorizontalBand, dpi: int) -> np.ndarray:
    inset = pixels(ROW_INSET_INCHES, dpi)
    top, bottom = upper.bottom + 1 + inset, lower.top - inset
    margin = pixels(WALL_SEARCH_MARGIN_INCHES, dpi)
    left = max(0, min(upper.left, lower.left) - margin)
    right = min(dark.shape[1], max(upper.right, lower.right) + margin + 1)
    if bottom - top < inset:
        return np.array([], dtype=int)
    coverage = dark[top:bottom, left:right].mean(axis=0)
    return np.flatnonzero(coverage >= WALL_MIN_COVERAGE) + left


def walls_span_row(columns: np.ndarray, page_width: int) -> bool:
    return columns.size > 0 and columns.max() - columns.min() >= page_width * WALL_MIN_SPAN_FRACTION


def build_table(dark: np.ndarray, rules: list[HorizontalBand], dpi: int, slope: float, strategy_name: str) -> ItemTable:
    walls = np.concatenate([wall_columns(dark, upper, lower, dpi) for upper, lower in zip(rules, rules[1:])])
    return ItemTable(rules=tuple(rules), left=int(walls.min()), right=int(walls.max()), slope=slope, strategy_name=strategy_name)


def analyse_page(dark: np.ndarray, dpi: int, slope: float = 0.0, strategy_name: str = GLOBAL_THRESHOLD_TRACING.name) -> PageLayout:
    height, width = dark.shape
    bands = horizontal_bands(dark, dpi)
    rules = [band for band in bands if band.thickness <= pixels(RULE_MAX_THICKNESS_INCHES, dpi)]
    chains: list[list[HorizontalBand]] = []
    for rule in rules:
        if chains and walls_span_row(wall_columns(dark, chains[-1][-1], rule, dpi), width):
            chains[-1].append(rule)
        else:
            chains.append([rule])
    tables = [build_table(dark, chain, dpi, slope, strategy_name) for chain in chains if len(chain) == RULES_PER_TABLE]
    return PageLayout(tables=tables, blocks=thick_blocks(bands, dpi), width=width, height=height)


def unaccounted_regions(tables: list[ItemTable], page_height: int, dpi: int) -> list[VerticalRegion]:
    heights = [table.height for table in tables]
    min_height = round(0.9 * float(np.median(heights))) if heights else pixels(UNACCOUNTED_MIN_HEIGHT_INCHES, dpi)
    regions: list[VerticalRegion] = []
    upper = 0
    for table in sorted(tables, key=lambda table: table.top):
        if table.top - upper >= min_height:
            regions.append(VerticalRegion(upper, table.top))
        upper = max(upper, table.bottom)
    if page_height - upper >= min_height:
        regions.append(VerticalRegion(upper, page_height))
    return regions


def candidate_slopes(strip: np.ndarray, page_slope: float) -> list[float]:
    local_slope = estimate_skew_slope(strip)
    return [local_slope] if np.isclose(local_slope, page_slope) else [local_slope, page_slope]


def tables_overlap(first: ItemTable, second: ItemTable) -> bool:
    return first.top <= second.bottom and second.top <= first.bottom


def recover_tables_in_region(
    rendered: np.ndarray, region: VerticalRegion, page_slope: float, dpi: int, page_masks: dict[str, np.ndarray], needed: int
) -> list[ItemTable]:
    clearance = pixels(REGION_CLEARANCE_INCHES, dpi)
    accepted: list[ItemTable] = []
    for strategy in RECOVERY_STRATEGIES:
        if strategy.name not in page_masks:
            page_masks[strategy.name] = strategy.mask(rendered, dpi)
        strip = page_masks[strategy.name][region.top + clearance : max(region.top + clearance + SKEW_ESTIMATE_DOWNSCALE, region.bottom - clearance)]
        for slope in candidate_slopes(strip, page_slope):
            layout = analyse_page(strategy.mask(shear_rows(rendered, slope), dpi), dpi, slope, strategy.name)
            for table in layout.tables:
                inside = region.top < table.top and table.bottom < region.bottom
                if inside and not any(tables_overlap(table, other) for other in accepted):
                    accepted.append(table)
            if len(accepted) >= needed:
                return accepted
    return accepted


def detect_layout(rendered: np.ndarray, dpi: int) -> PageLayout:
    height, width = rendered.shape[:2]
    page_slope = estimate_skew_slope(dark_mask(rendered, dpi))
    straightened = shear_rows(rendered, page_slope)
    primary = analyse_page(dark_mask(straightened, dpi), dpi, page_slope)
    blocks = thick_blocks(horizontal_bands(solid_mask(straightened, dpi), dpi), dpi)
    tables = list(primary.tables)
    unaccounted = unaccounted_regions(tables, height, dpi) if len(tables) < ITEMS_PER_PAGE else []
    recovered: list[ItemTable] = []
    page_masks = {GLOBAL_THRESHOLD_TRACING.name: dark_mask(rendered, dpi)} if unaccounted else {}
    for region in unaccounted:
        needed = ITEMS_PER_PAGE - len(tables) - len(recovered)
        if needed > 0:
            recovered += recover_tables_in_region(rendered, region, page_slope, dpi, page_masks, needed)
    return PageLayout(
        tables=sorted(tables + recovered, key=lambda table: table.top),
        blocks=blocks,
        width=width,
        height=height,
        unaccounted=tuple(unaccounted),
        recovered=tuple(recovered),
    )


def item_position(item_number: int) -> int:
    return (item_number - 1) % ITEMS_PER_PAGE


def crop_box(layout: PageLayout, position: int, dpi: int) -> CropBox:
    table = layout.tables[position]
    padding = pixels(CROP_PADDING_INCHES, dpi)
    obstacles_above = [other.bottom for other in layout.tables[:position]]
    obstacles_above += [block.bottom for block in layout.blocks if block.bottom < table.top]
    question_top = table.top - round(QUESTION_ALLOWANCE_ROWS * table.row_height)
    top = max([question_top] + [bottom + padding for bottom in obstacles_above])
    return CropBox(
        left=max(0, table.left - pixels(QUESTION_NUMBER_INDENT_INCHES, dpi)),
        top=max(0, top),
        right=min(layout.width, table.right + padding + 1),
        bottom=min(layout.height, table.bottom + padding + 1),
    )


def describe_tables(tables: list[ItemTable]) -> str:
    return ", ".join(table.describe() for table in tables) if tables else "none"


def describe_regions(regions: list[VerticalRegion]) -> str:
    return ", ".join(region.describe() for region in regions) if regions else "none"


def overlapping_pairs(tables: list[ItemTable]) -> list[tuple[ItemTable, ItemTable]]:
    return [(upper, lower) for upper, lower in zip(tables, tables[1:]) if lower.top <= upper.bottom]


def inconsistent_tables(tables: list[ItemTable]) -> list[ItemTable]:
    median_height = float(np.median([table.height for table in tables]))
    median_width = float(np.median([table.width for table in tables]))
    return [
        table
        for table in tables
        if abs(table.height - median_height) > TABLE_HEIGHT_TOLERANCE * median_height
        or abs(table.width - median_width) > TABLE_WIDTH_TOLERANCE * median_width
    ]


def validate_layout(layout: PageLayout, page_number: int, dpi: int) -> None:
    tables = layout.tables
    if len(tables) != ITEMS_PER_PAGE:
        remaining = unaccounted_regions(tables, layout.height, dpi) if len(tables) < ITEMS_PER_PAGE else []
        searched = f" Recovery searched {describe_regions(list(layout.unaccounted))} and found nothing more." if layout.unaccounted else ""
        raise TableDetectionError(
            f"found {len(tables)} item tables on page {page_number}, expected {ITEMS_PER_PAGE}. "
            f"Tables were traced at {describe_tables(tables)}. "
            f"Unaccounted for: {describe_regions(remaining)} of {layout.height}.{searched} "
            "The ruled table borders could not be traced reliably, so no crop was written"
        )
    overlaps = overlapping_pairs(tables)
    if overlaps:
        upper, lower = overlaps[0]
        raise TableDetectionError(
            f"item tables on page {page_number} overlap ({upper.describe()} and {lower.describe()}), so no crop was written"
        )
    misfits = inconsistent_tables(tables)
    if misfits:
        raise TableDetectionError(
            f"item tables on page {page_number} differ in size ({describe_tables(misfits)} do not match the others), "
            "so no crop was written"
        )


def validate_item_for_page(layout: PageLayout, item_number: int, page_number: int, dpi: int) -> None:
    validate_layout(layout, page_number, dpi)
    expected_items = HEADER_ITEM_NUMBERS if layout.is_header_page else CONTINUATION_ITEM_NUMBERS
    if item_number not in expected_items:
        kind = "a page 1 (header)" if layout.is_header_page else "a page 2 (continuation)"
        raise TableDetectionError(
            f"page {page_number} looks like {kind} page, which holds items {expected_items[0]} to {expected_items[-1]}, "
            f"but item {item_number} was requested"
        )


def verify_crop_holds_table(cropped: np.ndarray, table: ItemTable, item_number: int, dpi: int) -> None:
    strategy = STRATEGIES_BY_NAME[table.strategy_name]
    found = analyse_page(strategy.mask(cropped, dpi), dpi).tables
    complete = [candidate for candidate in found if abs(candidate.height - table.height) <= TABLE_HEIGHT_TOLERANCE * table.height]
    if len(complete) != 1:
        raise TableDetectionError(
            f"the crop for item {item_number} holds {len(complete)} complete item tables instead of 1, so no crop was written"
        )


def recovery_notes(layout: PageLayout) -> tuple[str, ...]:
    if not layout.recovered:
        return ()
    primary_count = len(layout.tables) - len(layout.recovered)
    notes = [f"the page wide pass traced {primary_count} of {ITEMS_PER_PAGE} tables and left {describe_regions(list(layout.unaccounted))} unaccounted for"]
    notes += [
        f"recovered the table at {table.describe()} using {table.strategy_name} with a local skew of {table.skew_degrees:.2f} degrees"
        for table in layout.recovered
    ]
    return tuple(notes)


def encode_png(rgb: np.ndarray) -> bytes:
    height, width, _ = rgb.shape
    pixmap = pymupdf.Pixmap(pymupdf.csRGB, width, height, np.ascontiguousarray(rgb).tobytes(), 0)
    return pixmap.tobytes("png")


def crop_rendered_item(rendered: np.ndarray, page_number: int, item_number: int, dpi: int) -> ItemCrop:
    layout = detect_layout(rendered, dpi)
    validate_item_for_page(layout, item_number, page_number, dpi)
    position = item_position(item_number)
    table = layout.tables[position]
    box = crop_box(layout, position, dpi)
    cropped = shear_rows(rendered, table.slope)[box.top : box.bottom, box.left : box.right]
    verify_crop_holds_table(cropped, table, item_number, dpi)
    return ItemCrop(png_bytes=encode_png(cropped), box=box, page_is_header=layout.is_header_page, notes=recovery_notes(layout))


def crop_item(pdf_path: Path, page_number: int, item_number: int, dpi: int = RENDER_DPI) -> ItemCrop:
    if item_number not in ALL_ITEM_NUMBERS:
        raise TableDetectionError(f"item {item_number} is outside {ALL_ITEM_NUMBERS[0]} to {ALL_ITEM_NUMBERS[-1]}")
    return crop_rendered_item(render_page_rgb(pdf_path, page_number, dpi), page_number, item_number, dpi)
