from itertools import combinations
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from rassi.config import FLAG_VALUES, MARK_CONFIDENCE_VALUES, MULTIPLE_MARK_SEPARATOR, REASON_OPTIONS, SCALE_OPTIONS

SCHEMA_VERSION = "4"
SCHEMA_NAME = "rassi_page_extraction"


def multiple_mark_values(options: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(
        MULTIPLE_MARK_SEPARATOR.join(marked)
        for count in range(2, len(options) + 1)
        for marked in combinations(options, count)
    )


SCALE_VALUES = SCALE_OPTIONS + multiple_mark_values(SCALE_OPTIONS) + FLAG_VALUES
REASON_VALUES = REASON_OPTIONS + multiple_mark_values(REASON_OPTIONS) + FLAG_VALUES

ScaleValue = Literal[SCALE_VALUES]
ReasonValue = Literal[REASON_VALUES]
MarkConfidence = Literal[MARK_CONFIDENCE_VALUES]

MARK_CONFIDENCE_DESCRIPTION = (
    "clear if every marked option in this row was made by its own separate mark and each can be read with certainty as "
    "belonging to one specific cell, and always clear for blank. unsure if one mark covers more than one cell, if any "
    "mark cannot be read cleanly, and always unsure for ambiguous."
)


class ItemExtraction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    item_number: int = Field(description="The item number printed above this item's table.")
    agreement_notes: str = Field(description="What is physically drawn in row A only, written before deciding agreement.")
    agreement: ScaleValue = Field(description="Row A. Agreement, 1 Strongly Disagree to 6 Strongly Agree. Two or more marked cells are joined by commas in left to right order, for example 1,4.")
    agreement_mark_confidence: MarkConfidence = Field(description="Row A. " + MARK_CONFIDENCE_DESCRIPTION)
    reason_notes: str = Field(description="What is physically drawn in row B only, written before deciding reason.")
    reason: ReasonValue = Field(description="Row B. Reason, a to d only. Two or more marked cells are joined by commas in left to right order, for example a,c.")
    reason_mark_confidence: MarkConfidence = Field(description="Row B. " + MARK_CONFIDENCE_DESCRIPTION)
    confidence_notes: str = Field(description="What is physically drawn in row C only, written before deciding confidence.")
    confidence: ScaleValue = Field(description="Row C. Confidence, 1 Not At All Confident to 6 Very Confident. Two or more marked cells are joined by commas in left to right order, for example 2,5.")
    confidence_mark_confidence: MarkConfidence = Field(description="Row C. " + MARK_CONFIDENCE_DESCRIPTION)


class PageExtraction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    page_type: Literal["header", "continuation"] = Field(description="header if the black box stamp with SID and PD is present, otherwise continuation.")
    sid: str | None = Field(description="Text after 'SID:' in the stamp, exactly as printed, or null if no stamp.")
    stamp_period: str | None = Field(description="Text after 'PD:' in the stamp, exactly as printed, or null if no stamp.")
    pre_post: Literal["PRE", "POST", "unreadable"] = Field(description="The PRE or POST label printed at the top right of the page.")
    items: list[ItemExtraction] = Field(description="One entry per item table on this page, in the order they appear.")
    page_notes: str = Field(description="Anything unusual on the page: scan artifacts, shadows, stripes, unreadable areas. Empty string if nothing.")


def page_extraction_json_schema() -> dict:
    return PageExtraction.model_json_schema()


def structured_output_format() -> dict:
    return {
        "type": "json_schema",
        "name": SCHEMA_NAME,
        "schema": page_extraction_json_schema(),
        "strict": True,
    }
