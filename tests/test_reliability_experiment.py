import json
from types import SimpleNamespace

import numpy as np
import pymupdf

from eval import reliability
from eval.reliability import detail_views
from rassi.crop import encode_png
from rassi.evaluation import ValueKey


def test_detail_views_preserve_pixels_and_overlap_without_gaps():
    rgb = np.arange(120 * 60 * 3, dtype=np.uint8).reshape(120, 60, 3)
    png = encode_png(rgb)
    views = detail_views(png)
    assert len(views) == 3
    for view, top, bottom in zip(views, (0, 33, 66), (54, 87, 120)):
        pixmap = pymupdf.Pixmap(view)
        actual = np.frombuffer(pixmap.samples, dtype=np.uint8).reshape(pixmap.height, pixmap.width, 3)
        np.testing.assert_array_equal(actual, rgb[top:bottom])
    assert png == encode_png(rgb)


def test_item_experiment_scores_only_adjudicated_fields_and_preserves_source_page(tmp_path, monkeypatch):
    directory = tmp_path / "partial"
    directory.mkdir()
    (directory / "case.json").write_text(json.dumps({"case_type": "page", "page_number": 33}))
    (directory / "label.json").write_text(json.dumps({
        "partial": True,
        "page_type": "header",
        "sid": "REDACTED",
        "items": [{"item_number": number, **({"confidence": "3"} if number == 3 else {})} for number in range(1, 7)],
    }))
    rgb = np.full((120, 60, 3), 255, dtype=np.uint8)
    (directory / "page.png").write_bytes(encode_png(rgb))
    calls = []

    def crop(rendered, page_number, item_number, dpi):
        calls.append((page_number, item_number))
        np.testing.assert_array_equal(rendered, rgb)
        return SimpleNamespace(png_bytes=b"complete-item-image")

    monkeypatch.setattr(reliability, "crop_rendered_item", crop)
    library = reliability.item_library(tmp_path)
    assert calls == [(33, 3)]
    assert len(library.cases) == 1
    assert library.cases[0].expected == {ValueKey(3, "confidence"): "3"}
    assert library.cases[0].image_png == b"complete-item-image"
    assert library.cases[0].case_type == "crop"
