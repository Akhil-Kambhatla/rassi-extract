import threading
from dataclasses import dataclass, field

from rassi.config import (
    ESTIMATED_INPUT_TOKENS_PER_PAGE,
    ESTIMATED_OUTPUT_TOKENS_PER_PAGE,
    PRICES_PER_MILLION_TOKENS,
)


class UnknownModelPriceError(ValueError):
    pass


@dataclass
class TokenUsage:
    input_tokens: int = 0
    output_tokens: int = 0
    reasoning_tokens: int = 0
    cached_input_tokens: int = 0

    def as_dict(self) -> dict:
        return {
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "reasoning_tokens": self.reasoning_tokens,
            "cached_input_tokens": self.cached_input_tokens,
        }

    def minus(self, other: "TokenUsage") -> "TokenUsage":
        return TokenUsage(
            input_tokens=self.input_tokens - other.input_tokens,
            output_tokens=self.output_tokens - other.output_tokens,
            reasoning_tokens=self.reasoning_tokens - other.reasoning_tokens,
            cached_input_tokens=self.cached_input_tokens - other.cached_input_tokens,
        )

    @classmethod
    def from_dict(cls, data: dict) -> "TokenUsage":
        return cls(
            input_tokens=int(data.get("input_tokens", 0)),
            output_tokens=int(data.get("output_tokens", 0)),
            reasoning_tokens=int(data.get("reasoning_tokens", 0)),
            cached_input_tokens=int(data.get("cached_input_tokens", 0)),
        )


def prices_for(model: str) -> dict:
    if model not in PRICES_PER_MILLION_TOKENS:
        known = ", ".join(sorted(PRICES_PER_MILLION_TOKENS))
        raise UnknownModelPriceError(f"No price for model '{model}' in config.PRICES_PER_MILLION_TOKENS. Known models: {known}")
    return PRICES_PER_MILLION_TOKENS[model]


def cost_usd(model: str, usage: TokenUsage) -> float:
    prices = prices_for(model)
    return (usage.input_tokens * prices["input"] + usage.output_tokens * prices["output"]) / 1_000_000


def default_estimated_cost_per_page(model: str) -> float:
    return cost_usd(
        model,
        TokenUsage(input_tokens=ESTIMATED_INPUT_TOKENS_PER_PAGE, output_tokens=ESTIMATED_OUTPUT_TOKENS_PER_PAGE),
    )


@dataclass(frozen=True)
class SpendSnapshot:
    spent_usd: float = 0.0
    calls_made: int = 0
    usage: TokenUsage = field(default_factory=TokenUsage)

    def since(self, earlier: "SpendSnapshot") -> "SpendSnapshot":
        return SpendSnapshot(
            spent_usd=self.spent_usd - earlier.spent_usd,
            calls_made=self.calls_made - earlier.calls_made,
            usage=self.usage.minus(earlier.usage),
        )


class SpendTracker:
    def __init__(self, model: str, max_spend_usd: float, fallback_estimate_usd: float | None = None):
        self.model = model
        self.max_spend_usd = max_spend_usd
        self.fallback_estimate_usd = (
            fallback_estimate_usd if fallback_estimate_usd is not None else default_estimated_cost_per_page(model)
        )
        self.spent_usd = 0.0
        self.reserved_usd = 0.0
        self.calls_made = 0
        self.largest_call_usd = 0.0
        self.usage = TokenUsage()
        self.limit_reached = False
        self._lock = threading.Lock()

    def next_call_estimate_usd(self) -> float:
        if self.calls_made > 0:
            return self.largest_call_usd
        return self.fallback_estimate_usd

    def try_reserve_call(self) -> float | None:
        with self._lock:
            if self.limit_reached:
                return None
            estimate = self.next_call_estimate_usd()
            if self.spent_usd + self.reserved_usd + estimate > self.max_spend_usd:
                self.limit_reached = True
                return None
            self.reserved_usd += estimate
            return estimate

    def release_reservation(self, reservation_usd: float) -> None:
        with self._lock:
            self.reserved_usd = max(0.0, self.reserved_usd - reservation_usd)

    def record_call(self, reservation_usd: float, usage: TokenUsage) -> tuple[float, float]:
        call_cost = cost_usd(self.model, usage)
        with self._lock:
            self.reserved_usd = max(0.0, self.reserved_usd - reservation_usd)
            self.spent_usd += call_cost
            self.calls_made += 1
            self.largest_call_usd = max(self.largest_call_usd, call_cost)
            self.usage.input_tokens += usage.input_tokens
            self.usage.output_tokens += usage.output_tokens
            self.usage.reasoning_tokens += usage.reasoning_tokens
            self.usage.cached_input_tokens += usage.cached_input_tokens
            return call_cost, self.spent_usd

    def snapshot(self) -> SpendSnapshot:
        with self._lock:
            return SpendSnapshot(
                spent_usd=self.spent_usd,
                calls_made=self.calls_made,
                usage=TokenUsage.from_dict(self.usage.as_dict()),
            )
