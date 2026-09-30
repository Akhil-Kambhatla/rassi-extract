KNOWN_TEACHER_CODES = frozenset(
    {
        "010401",
        "010406",
        "010407",
        "020415",
        "020416",
        "020417",
        "020418",
        "020419",
        "020420",
        "020421",
    }
)

DEFAULT_MODEL = "gpt-5.6-terra"
DEFAULT_MAX_SPEND_USD = 2.00
DEFAULT_WORKERS = 4

RENDER_DPI = 300
IMAGE_DETAIL = "high"
API_MAX_ATTEMPTS = 3
API_BACKOFF_BASE_SECONDS = 2.0
API_TIMEOUT_SECONDS = 180

REASONING_EFFORT_PREFERENCE = ["none", "minimal", "low", "medium"]

PROJECTION_PAGES_PER_PDF = 50
PROJECTION_PDF_COUNT = 100

PRICES_PER_MILLION_TOKENS = {
    "gpt-5.6-terra": {"input": 2.00, "output": 12.00},
    "gpt-5.6-luna": {"input": 0.20, "output": 1.20},
}

ESTIMATED_INPUT_TOKENS_PER_PAGE = 3000
ESTIMATED_OUTPUT_TOKENS_PER_PAGE = 1500

MISSING_VALUE_CODE = ""

ITEMS_PER_PAGE = 6
HEADER_ITEM_NUMBERS = list(range(1, 7))
CONTINUATION_ITEM_NUMBERS = list(range(7, 13))
ALL_ITEM_NUMBERS = list(range(1, 13))

NON_NUMERIC_VALUES = ("blank", "ambiguous", "multiple")
REASON_LETTER_TO_NUMBER = {"a": 1, "b": 2, "c": 3, "d": 4}
REDACTED_SID = "REDACTED"
