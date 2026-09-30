from rassi.schema import ItemExtraction, PageExtraction


def make_item(
    item_number: int,
    agreement: str = "4",
    reason: str = "b",
    confidence: str = "5",
    agreement_notes: str = "single circle",
    reason_notes: str = "single circle",
    confidence_notes: str = "single circle",
) -> ItemExtraction:
    return ItemExtraction(
        item_number=item_number,
        agreement_notes=agreement_notes,
        agreement=agreement,
        reason_notes=reason_notes,
        reason=reason,
        confidence_notes=confidence_notes,
        confidence=confidence,
    )


def header_page(sid: str | None = "123456", stamp_period: str | None = "1", pre_post: str = "PRE", items: list[ItemExtraction] | None = None) -> PageExtraction:
    return PageExtraction(
        page_type="header",
        sid=sid,
        stamp_period=stamp_period,
        pre_post=pre_post,
        items=items if items is not None else [make_item(number) for number in range(1, 7)],
        page_notes="",
    )


def continuation_page(pre_post: str = "PRE", items: list[ItemExtraction] | None = None) -> PageExtraction:
    return PageExtraction(
        page_type="continuation",
        sid=None,
        stamp_period=None,
        pre_post=pre_post,
        items=items if items is not None else [make_item(number) for number in range(7, 13)],
        page_notes="",
    )


def numbered(pages: list[PageExtraction], start: int = 1) -> list[tuple[int, PageExtraction]]:
    return [(start + offset, page) for offset, page in enumerate(pages)]
