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
DEFAULT_MAX_SPEND_USD = 15.00
DEFAULT_WORKERS = 4

RENDER_DPI = 300
IMAGE_DETAIL = "high"
PROMPT_CACHE_KEY = "rassi-page"
API_MAX_ATTEMPTS = 3
API_BACKOFF_BASE_SECONDS = 2.0
API_TIMEOUT_SECONDS = 180

REASONING_EFFORT_PREFERENCE = ["none", "minimal", "low", "medium"]

PROJECTION_PAGES_PER_PDF = 50
PROJECTION_PDF_COUNT = 100

PRICES_PER_MILLION_TOKENS = {
    "gpt-5.6-terra": {"input": 2.00, "cached_input": 0.20, "output": 12.00},
    "gpt-5.6-luna": {"input": 0.20, "cached_input": 0.02, "output": 1.20},
}
CACHED_INPUT_TOKENS_SOURCE = (
    "OpenAI reports cached input tokens in usage.input_tokens_details.cached_tokens. They are part of input_tokens "
    "and are billed at the cached_input rate instead of the input rate."
)
LONG_CONTEXT_SURCHARGE_NOTE = (
    "OpenAI adds a long context surcharge to requests above its long context threshold. This tool sends one page "
    "per call, a few thousand input tokens, which stays well below that threshold, so the surcharge never applies."
)

ESTIMATED_INPUT_TOKENS_PER_PAGE = 3000
ESTIMATED_OUTPUT_TOKENS_PER_PAGE = 1500

MISSING_VALUE_CODE = ""

ITEMS_PER_PAGE = 6
HEADER_ITEM_NUMBERS = list(range(1, 7))
CONTINUATION_ITEM_NUMBERS = list(range(7, 13))
ALL_ITEM_NUMBERS = list(range(1, 13))

SCALE_OPTIONS = ("1", "2", "3", "4", "5", "6")
REASON_OPTIONS = ("a", "b", "c", "d")
FLAG_VALUES = ("blank", "ambiguous")
MULTIPLE_MARK_SEPARATOR = ","
MULTIPLE_MARK_FLAG = "multiple"
REDACTED_SID = "REDACTED"

MARK_CONFIDENCE_CLEAR = "clear"
MARK_CONFIDENCE_UNSURE = "unsure"
MARK_CONFIDENCE_VALUES = (MARK_CONFIDENCE_CLEAR, MARK_CONFIDENCE_UNSURE)
UNSURE_VALUE_MARKER = "?"
REVIEW_CHECK_WORD = "check"
COMBINED_DIRECTORY_NAME = "_combined"
