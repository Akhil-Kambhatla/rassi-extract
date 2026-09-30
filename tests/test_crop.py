from pathlib import Path

import numpy as np
import pymupdf
import pytest

from rassi.crop import (
    TableDetectionError,
    analyse_page,
    close_along_axis,
    crop_item,
    crop_rendered_item,
    dark_mask,
    detect_layout,
    estimate_skew_slope,
    render_page_rgb,
    shear_rows,
    validate_layout,
    verify_crop_holds_table,
)

EXISTING_FIXTURE = Path(__file__).parent / "fixtures" / "synthetic_two_page.pdf"
PAGE_WIDTH, PAGE_HEIGHT = 612, 792
TABLE_LEFT, TABLE_RIGHT = 54, 558
ROW_HEIGHT = 24
FIRST_TABLE_TOP = 150
TABLE_PITCH = 104
COLUMN_EDGES = [54, 120, 193, 266, 339, 412, 485, 558]
DPI = 300


def table_tops() -> list[int]:
    return [FIRST_TABLE_TOP + index * TABLE_PITCH for index in range(6)]


def draw_form_page(page: pymupdf.Page, first_item: int, header: bool) -> None:
    if header:
        page.draw_rect(pymupdf.Rect(36, 36, 400, 110), color=(0, 0, 0), fill=(0, 0, 0))
        page.insert_text((44, 60), "SID: TEST01", color=(1, 1, 1), fontsize=12)
    for index, top in enumerate(table_tops()):
        page.insert_text((TABLE_LEFT - 18, top - 8), f"{first_item + index}. Synthetic question text for this item.", fontsize=10)
        bottom = top + 3 * ROW_HEIGHT
        page.draw_rect(pymupdf.Rect(COLUMN_EDGES[-3], top + ROW_HEIGHT, TABLE_RIGHT, top + 2 * ROW_HEIGHT), color=None, fill=(0.2, 0.2, 0.2))
        for row in range(4):
            y = top + row * ROW_HEIGHT
            page.draw_line((TABLE_LEFT, y), (TABLE_RIGHT, y), color=(0, 0, 0), width=0.8)
        for x in COLUMN_EDGES:
            page.draw_line((x, top), (x, bottom), color=(0, 0, 0), width=0.8)
        for column, label in enumerate(["1", "2", "3", "4", "5", "6"]):
            page.insert_text((COLUMN_EDGES[column + 1] + 20, top + 16), label, fontsize=10)


@pytest.fixture(scope="module")
def synthetic_form(tmp_path_factory) -> Path:
    path = tmp_path_factory.mktemp("forms") / "synthetic_form.pdf"
    document = pymupdf.open()
    draw_form_page(document.new_page(width=PAGE_WIDTH, height=PAGE_HEIGHT), 1, header=True)
    draw_form_page(document.new_page(width=PAGE_WIDTH, height=PAGE_HEIGHT), 7, header=False)
    document.save(path)
    document.close()
    return path


def points_to_pixels(points: float) -> float:
    return points * DPI / 72


def test_detects_six_tables_on_both_pages(synthetic_form):
    for page_number, is_header in ((1, True), (2, False)):
        rgb = render_page_rgb(synthetic_form, page_number, DPI)
        layout = analyse_page(dark_mask(rgb, DPI), DPI)
        assert len(layout.tables) == 6
        assert layout.is_header_page is is_header
        for table, top in zip(layout.tables, table_tops()):
            assert abs(table.top - points_to_pixels(top)) < 12
            assert abs(table.bottom - points_to_pixels(top + 3 * ROW_HEIGHT)) < 12
            assert abs(table.left - points_to_pixels(TABLE_LEFT)) < 12
            assert abs(table.right - points_to_pixels(TABLE_RIGHT)) < 12


@pytest.mark.parametrize("page_number,item_number,position", [(1, 1, 0), (1, 4, 3), (2, 7, 0), (2, 12, 5)])
def test_crop_covers_the_requested_table_and_its_question(synthetic_form, page_number, item_number, position):
    crop = crop_item(synthetic_form, page_number, item_number)
    top = table_tops()[position]
    box = crop.box
    assert box.top < points_to_pixels(top - 12)
    assert box.bottom > points_to_pixels(top + 3 * ROW_HEIGHT)
    assert box.left < points_to_pixels(TABLE_LEFT - 18)
    assert box.right > points_to_pixels(TABLE_RIGHT)
    if position > 0:
        assert box.top > points_to_pixels(table_tops()[position - 1] + 3 * ROW_HEIGHT)
    if page_number == 1 and position == 0:
        assert box.top > points_to_pixels(110)
    with pymupdf.open(stream=crop.png_bytes, filetype="png") as image:
        assert image[0].rect.width > 0


def test_crop_rejects_item_from_the_other_page_of_the_pair(synthetic_form):
    with pytest.raises(TableDetectionError, match="items 1 to 6"):
        crop_item(synthetic_form, 1, 7)
    with pytest.raises(TableDetectionError, match="items 7 to 12"):
        crop_item(synthetic_form, 2, 3)


def test_crop_fails_clearly_on_a_page_without_tables():
    with pytest.raises(TableDetectionError, match="found 0 item tables on page 1, expected 6"):
        crop_item(EXISTING_FIXTURE, 1, 1)


def test_crop_rejects_pages_and_items_out_of_range(synthetic_form):
    with pytest.raises(TableDetectionError, match="outside the document"):
        crop_item(synthetic_form, 3, 1)
    with pytest.raises(TableDetectionError, match="item 13"):
        crop_item(synthetic_form, 1, 13)


def test_skew_estimate_straightens_sloped_lines():
    height, width = 800, 1600
    slope = np.tan(np.radians(0.8))
    dark = np.zeros((height, width), dtype=bool)
    columns = np.arange(width)
    for base in (200, 400, 600):
        rows = np.round(base + (columns - width / 2) * slope).astype(int)
        dark[rows, columns] = True
        dark[rows + 1, columns] = True
    estimated = estimate_skew_slope(dark)
    assert estimated == pytest.approx(slope, abs=np.tan(np.radians(0.06)))
    straightened = shear_rows(dark, estimated)
    assert straightened.sum(axis=1).max() > 0.9 * width


def points_to_pixel_index(points: float) -> int:
    return round(points_to_pixels(points))


@pytest.fixture(scope="module")
def clean_continuation_page(synthetic_form) -> np.ndarray:
    return render_page_rgb(synthetic_form, 2, DPI)


def warp_with_skew_drifting_down_the_page(rgb: np.ndarray, top_degrees: float, bottom_degrees: float) -> np.ndarray:
    height, width = rgb.shape[:2]
    rows = np.arange(height)[:, None]
    columns = np.arange(width)[None, :]
    slopes = np.tan(np.radians(top_degrees + (bottom_degrees - top_degrees) * rows / height))
    source_rows = np.clip(np.round(rows + (columns - width / 2) * slopes).astype(int), 0, height - 1)
    return rgb[source_rows, columns]


def rotate(rgb: np.ndarray, degrees: float) -> np.ndarray:
    height, width = rgb.shape[:2]
    angle = np.radians(degrees)
    rows, columns = np.mgrid[0:height, 0:width]
    centre_row, centre_column = height / 2, width / 2
    source_columns = np.round(centre_column + (columns - centre_column) * np.cos(angle) - (rows - centre_row) * np.sin(angle)).astype(int)
    source_rows = np.round(centre_row + (columns - centre_column) * np.sin(angle) + (rows - centre_row) * np.cos(angle)).astype(int)
    inside = (source_columns >= 0) & (source_columns < width) & (source_rows >= 0) & (source_rows < height)
    rotated = np.full_like(rgb, 255)
    rotated[inside] = rgb[source_rows[inside], source_columns[inside]]
    return rotated


def break_borders(rgb: np.ndarray, gap: int = 20, period: int = 90) -> np.ndarray:
    broken = rgb.copy()
    reach = 6
    for top in table_tops():
        for row in range(4):
            y = points_to_pixel_index(top + row * ROW_HEIGHT)
            for x in range(points_to_pixel_index(TABLE_LEFT) + period // 2, points_to_pixel_index(TABLE_RIGHT), period):
                broken[y - reach : y + reach, x : x + gap] = 255
        for edge in COLUMN_EDGES:
            x = points_to_pixel_index(edge)
            for y in range(points_to_pixel_index(top) + period // 3, points_to_pixel_index(top + 3 * ROW_HEIGHT), period // 2):
                broken[y : y + gap, x - reach : x + reach] = 255
    return broken


def add_speckle(rgb: np.ndarray, density: float = 0.004, dot_size: int = 3) -> np.ndarray:
    speckled = rgb.copy()
    height, width = rgb.shape[:2]
    generator = np.random.default_rng(34)
    count = int(height * width * density)
    rows = generator.integers(0, height - dot_size, count)
    columns = generator.integers(0, width - dot_size, count)
    for row_offset in range(dot_size):
        for column_offset in range(dot_size):
            speckled[rows + row_offset, columns + column_offset] = 0
    return speckled


def add_shadow_band(rgb: np.ndarray, top_fraction: float = 0.3, bottom_fraction: float = 0.55, brightness: float = 0.55) -> np.ndarray:
    height = rgb.shape[0]
    factors = np.ones(height)
    factors[int(height * top_fraction) : int(height * bottom_fraction)] = brightness
    return (rgb * factors[:, None, None]).astype(np.uint8)


def erase_table(rgb: np.ndarray, position: int) -> np.ndarray:
    erased = rgb.copy()
    top = table_tops()[position]
    erased[points_to_pixel_index(top - 20) : points_to_pixel_index(top + 3 * ROW_HEIGHT + 4)] = 255
    return erased


def assert_six_tables_with_complete_crops(rgb: np.ndarray) -> None:
    layout = detect_layout(rgb, DPI)
    assert len(layout.tables) == 6
    assert layout.is_header_page is False
    validate_layout(layout, 2, DPI)
    for item_number in (7, 9, 12):
        crop = crop_rendered_item(rgb, 2, item_number, DPI)
        assert crop.box.bottom > crop.box.top


def test_primary_pass_misses_a_table_when_skew_drifts_down_the_page(clean_continuation_page):
    warped = warp_with_skew_drifting_down_the_page(clean_continuation_page, 0.0, 1.8)
    slope = estimate_skew_slope(dark_mask(warped, DPI))
    primary = analyse_page(dark_mask(shear_rows(warped, slope), DPI), DPI)
    assert len(primary.tables) == 5
    layout = detect_layout(warped, DPI)
    assert len(layout.recovered) == 1
    assert layout.unaccounted
    assert_six_tables_with_complete_crops(warped)


def test_recovered_table_crop_explains_the_recovery(clean_continuation_page):
    warped = warp_with_skew_drifting_down_the_page(clean_continuation_page, 0.0, 1.8)
    recovered_position = [table.top for table in detect_layout(warped, DPI).tables].index(detect_layout(warped, DPI).recovered[0].top)
    crop = crop_rendered_item(warped, 2, 7 + recovered_position, DPI)
    assert any("unaccounted for" in note for note in crop.notes)
    assert any("recovered the table" in note for note in crop.notes)


def test_six_tables_despite_broken_border_segments(clean_continuation_page):
    assert_six_tables_with_complete_crops(break_borders(clean_continuation_page))


def test_six_tables_despite_heavy_speckle(clean_continuation_page):
    assert_six_tables_with_complete_crops(add_speckle(clean_continuation_page))


def test_six_tables_despite_a_shadow_band(clean_continuation_page):
    assert_six_tables_with_complete_crops(add_shadow_band(clean_continuation_page))


def test_six_tables_despite_stronger_skew(clean_continuation_page):
    assert_six_tables_with_complete_crops(rotate(clean_continuation_page, 1.8))


def test_header_block_is_still_found_under_a_shadow_band(synthetic_form):
    shadowed = add_shadow_band(render_page_rgb(synthetic_form, 1, DPI), top_fraction=0.02, bottom_fraction=0.3)
    layout = detect_layout(shadowed, DPI)
    assert len(layout.tables) == 6
    assert layout.is_header_page is True


def test_missing_table_is_refused_with_the_unaccounted_region_named(clean_continuation_page):
    erased = erase_table(clean_continuation_page, 2)
    with pytest.raises(TableDetectionError) as refusal:
        crop_rendered_item(erased, 2, 7, DPI)
    message = str(refusal.value)
    assert "found 5 item tables on page 2, expected 6" in message
    gap_top = points_to_pixels(table_tops()[1] + 3 * ROW_HEIGHT)
    gap_bottom = points_to_pixels(table_tops()[3])
    unaccounted = message.split("Unaccounted for: ")[1].split(" of ")[0]
    regions = [tuple(int(value) for value in region.removeprefix("rows ").split(" to ")) for region in unaccounted.split(", ")]
    assert any(abs(top - gap_top) < 12 and abs(bottom - gap_bottom) < 12 for top, bottom in regions)
    assert "Recovery searched" in message


def test_crop_without_a_complete_table_is_refused(clean_continuation_page):
    layout = detect_layout(clean_continuation_page, DPI)
    table = layout.tables[0]
    half_table = clean_continuation_page[table.top - 100 : table.top + table.height // 2, table.left - 100 : table.right + 20]
    with pytest.raises(TableDetectionError, match="holds 0 complete item tables instead of 1"):
        verify_crop_holds_table(half_table, table, 7, DPI)


def test_border_closing_bridges_short_gaps_only():
    row = np.zeros((1, 200), dtype=bool)
    row[0, 20:80] = True
    row[0, 90:150] = True
    row[0, 190:200] = True
    closed = close_along_axis(row, 24, axis=1)
    assert closed[0, 20:150].all()
    assert not closed[0, 160:180].any()
