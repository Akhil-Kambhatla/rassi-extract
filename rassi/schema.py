from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

SCHEMA_VERSION = "2"
SCHEMA_NAME = "rassi_page_extraction"

ScaleValue = Literal["1", "2", "3", "4", "5", "6", "blank", "ambiguous", "multiple"]
ReasonValue = Literal["a", "b", "c", "d", "blank", "ambiguous", "multiple"]


class ItemExtraction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    item_number: int = Field(description="The item number printed above this item's table.")
    agreement_notes: str = Field(description="What is physically drawn in row A only, written before deciding agreement.")
    agreement: ScaleValue = Field(description="Row A. Agreement, 1 Strongly Disagree to 6 Strongly Agree.")
    reason_notes: str = Field(description="What is physically drawn in row B only, written before deciding reason.")
    reason: ReasonValue = Field(description="Row B. Reason, a to d only.")
    confidence_notes: str = Field(description="What is physically drawn in row C only, written before deciding confidence.")
    confidence: ScaleValue = Field(description="Row C. Confidence, 1 Not At All Confident to 6 Very Confident.")


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
