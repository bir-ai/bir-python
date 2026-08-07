"""Immutable SDK configuration, environment parsing, and validation.

Dependency direction: this module depends only on the Python standard library.
Runtime, capture, persistence, and CLI modules may depend on it; it never imports
those higher-level modules or owns their mutable active-configuration state.
"""

from __future__ import annotations

import math
import os
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

_DEFAULT_TRACE_PATH = Path(".bir/traces.jsonl")

# Upper bounds on the user-supplied additive redaction rules accepted by
# ``configure``. They reject unboundedly large configuration that would slow
# every capture or grow memory without limit. Built-in rules are never counted
# against these caps and can never be disabled, replaced, or reordered.
_MAX_ADDITIONAL_SECRET_KEYS = 100
_MAX_ADDITIONAL_REDACTION_PATTERNS = 100
_MAX_ADDITIONAL_SECRET_KEY_LENGTH = 200
_MAX_ADDITIONAL_REDACTION_PATTERN_LENGTH = 1000

# Upper bound on the opt-in ``sample_rules`` table accepted by ``configure``.
# Rules are checked only at trace-root creation, but a cap still avoids carrying
# an unbounded process-global configuration. Exact name matching keeps lookups
# predictable and leaves the global ``sample_rate`` as the default.
_MAX_SAMPLE_RULES = 1000

# Upper bound on the opt-in ``model_prices`` table accepted by ``configure``. It
# rejects an unboundedly large price table while leaving ample room for a
# user-curated list. Bir bundles no prices; this only caps what a caller supplies.
_MAX_MODEL_PRICES = 1000

# The only keys accepted inside a single model's rate mapping: the per-token
# ``input``/``output`` rates and an optional ``currency`` (default "USD").
_MODEL_PRICE_RATE_KEYS = frozenset({"input", "output", "currency"})


@dataclass(frozen=True)
class _ModelPrice:
    """Validated per-token rates and currency for one ``model_prices`` entry.

    ``input``/``output`` are non-negative, finite per-token rates and either may
    be ``None`` when only one side is priced; ``currency`` defaults to ``"USD"``.
    Frozen so the whole price table stays hashable on the immutable ``_Config``.
    """

    input: int | float | None
    output: int | float | None
    currency: str


@dataclass(frozen=True)
class _Config:
    """Immutable, validated settings consumed by the SDK's internal modules."""

    trace_path: Path = _DEFAULT_TRACE_PATH
    capture_inputs: bool = False
    capture_outputs: bool = False
    service_name: str | None = None
    environment: str | None = None
    # Optional trace-source tag recorded on trace roots under ``metadata.source``.
    # It mirrors the ``source`` field the product's Playground writes and that the
    # server/dashboard filter on by exact match, giving SDK callers a first-class
    # way to tag where a trace came from. ``None`` records nothing.
    source: str | None = None
    # Master on/off switch for all recording. When False, every primitive still
    # runs the user's body and still propagates exceptions, but nothing is ever
    # written -- an explicit, intent-revealing kill switch (feature flag, incident
    # toggle, tests) enforced through the same "trace dropped" path as sampling.
    enabled: bool = True
    sample_rate: float = 1.0
    # Optional exact trace-root-name sampling overrides. Stored as a validated,
    # name-sorted tuple so the frozen config stays hashable.
    sample_rules: tuple[tuple[str, float], ...] = ()
    # ``max_bytes is None`` keeps the historical unbounded single trace file.
    max_bytes: int | None = None
    backup_count: int = 3
    # Additive redaction rules are normalized/compiled at configuration time.
    additional_secret_keys: frozenset[str] = frozenset()
    additional_redaction_patterns: tuple[re.Pattern[str], ...] = ()
    # Opt-in, local-only price table, kept deterministic and hashable.
    model_prices: tuple[tuple[str, _ModelPrice], ...] = ()
    # Opt-in capture-size limits; ``None`` preserves historical behavior.
    max_value_length: int | None = None
    max_collection_items: int | None = None


def _validate_additional_secret_keys(value: Any) -> frozenset[str]:
    """Validate and normalize the user-supplied extra secret-key names.

    Accepts any non-string iterable of non-empty strings and returns them
    normalized to the same form the built-in name rule uses (lower-cased with
    ``-`` treated as ``_``), so matching is exact and case-insensitive. A bare
    ``str``/``bytes`` is rejected so a single key is never silently iterated
    character by character. The entry count and per-key length are bounded so a
    pathologically large configuration fails fast with a clear error.
    """

    if isinstance(value, (str, bytes)) or not isinstance(value, Iterable):
        raise TypeError("bir additional_secret_keys must be an iterable of strings")
    keys = list(value)
    if len(keys) > _MAX_ADDITIONAL_SECRET_KEYS:
        raise ValueError(f"bir additional_secret_keys must not exceed {_MAX_ADDITIONAL_SECRET_KEYS} entries")
    normalized: set[str] = set()
    for key in keys:
        if not isinstance(key, str):
            raise TypeError("bir additional_secret_keys entries must be strings")
        if not key:
            raise ValueError("bir additional_secret_keys entries must not be empty")
        if len(key) > _MAX_ADDITIONAL_SECRET_KEY_LENGTH:
            raise ValueError(
                f"bir additional_secret_keys entries must not exceed {_MAX_ADDITIONAL_SECRET_KEY_LENGTH} characters"
            )
        normalized.add(key.lower().replace("-", "_"))
    return frozenset(normalized)


def _validate_additional_redaction_patterns(value: Any) -> tuple[re.Pattern[str], ...]:
    """Validate and compile the user-supplied extra text-redaction patterns.

    Accepts any non-string iterable of regex strings and/or already-compiled
    ``re.Pattern`` objects, compiling and validating each exactly once here so a
    bad pattern fails at ``configure`` time rather than on a later capture. The
    entry count is bounded to reject unboundedly large configuration. The
    returned patterns run in order, after every built-in rule.
    """

    if isinstance(value, (str, bytes, re.Pattern)) or not isinstance(value, Iterable):
        raise TypeError("bir additional_redaction_patterns must be an iterable of regex strings or compiled patterns")
    raw_patterns = list(value)
    if len(raw_patterns) > _MAX_ADDITIONAL_REDACTION_PATTERNS:
        raise ValueError(
            f"bir additional_redaction_patterns must not exceed {_MAX_ADDITIONAL_REDACTION_PATTERNS} entries"
        )
    return tuple(_compile_additional_redaction_pattern(pattern) for pattern in raw_patterns)


def _compile_additional_redaction_pattern(pattern: Any) -> re.Pattern[str]:
    """Return a compiled ``str`` regex for one additional pattern entry."""

    if isinstance(pattern, re.Pattern):
        if isinstance(pattern.pattern, bytes):
            raise TypeError("bir additional_redaction_patterns compiled patterns must be str patterns, not bytes")
        return cast("re.Pattern[str]", pattern)
    if not isinstance(pattern, str):
        raise TypeError(
            "bir additional_redaction_patterns entries must be regex strings or compiled re.Pattern objects"
        )
    if not pattern:
        raise ValueError("bir additional_redaction_patterns entries must not be empty")
    if len(pattern) > _MAX_ADDITIONAL_REDACTION_PATTERN_LENGTH:
        raise ValueError(
            f"bir additional_redaction_patterns entries must not exceed "
            f"{_MAX_ADDITIONAL_REDACTION_PATTERN_LENGTH} characters"
        )
    try:
        return re.compile(pattern)
    except re.error as exc:
        raise ValueError(f"bir additional_redaction_patterns entry is not a valid regex: {exc}") from exc


def _validate_event_name(value: Any, field: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"bir {field} must be a string")
    if not value:
        raise ValueError(f"bir {field} must not be empty")
    return value


def _validate_bool(value: Any, field: str) -> bool:
    if not isinstance(value, bool):
        raise TypeError(f"bir {field} must be a bool")
    return value


def _validate_number(value: Any, field: str) -> int | float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"bir {field} must be an int or float")
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError(f"bir {field} must be finite")
    return value


def _is_finite_number(value: int | float) -> bool:
    """Whether :func:`_validate_number` would accept ``value`` as finite.

    Mirrors the check inside it so a caller can ask the question without having
    to catch the exception. Only a float can be ``inf`` or ``nan``; a Python int
    is unbounded and always finite, which is also why this cannot be
    ``math.isfinite`` -- that raises ``OverflowError`` on an int too large to
    convert to a float, and such an int is a perfectly recordable number.
    """

    return not (isinstance(value, float) and not math.isfinite(value))


def _validate_non_negative_number(value: Any, field: str) -> int | float:
    numeric_value = _validate_number(value, field)
    if numeric_value < 0:
        raise ValueError(f"bir {field} must be non-negative")
    return numeric_value


def _validate_sample_rate(value: Any) -> float:
    numeric_value = _validate_number(value, "sample_rate")
    if numeric_value < 0.0 or numeric_value > 1.0:
        raise ValueError("bir sample_rate must be between 0.0 and 1.0")
    return float(numeric_value)


def _validate_sample_rules(value: Any) -> tuple[tuple[str, float], ...]:
    """Validate exact trace-root-name sampling overrides."""

    if not isinstance(value, Mapping):
        raise TypeError("bir sample_rules must be a mapping of trace name to sample rate")
    if len(value) > _MAX_SAMPLE_RULES:
        raise ValueError(f"bir sample_rules must not exceed {_MAX_SAMPLE_RULES} entries")
    normalized: list[tuple[str, float]] = []
    for trace_name, sample_rate in value.items():
        name = _validate_event_name(trace_name, "sample_rules keys")
        try:
            rate = _validate_sample_rate(sample_rate)
        except TypeError as exc:
            raise TypeError(f"bir sample_rules[{name!r}] rate must be an int or float") from exc
        except ValueError as exc:
            message = str(exc).replace("sample_rate", f"sample_rules[{name!r}] rate")
            raise ValueError(message) from exc
        normalized.append((name, rate))
    normalized.sort(key=lambda item: item[0])
    return tuple(normalized)


def _sample_rate_for_trace(trace_name: str, config: _Config) -> float:
    """Return the exact-name sampling override or the configured default."""

    for name, sample_rate in config.sample_rules:
        if name == trace_name:
            return sample_rate
    return config.sample_rate


def _validate_non_negative_int(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"bir {field} must be an int")
    if value < 0:
        raise ValueError(f"bir {field} must be non-negative")
    return value


def _validate_positive_int(value: Any, field: str) -> int:
    value = _validate_non_negative_int(value, field)
    if value == 0:
        raise ValueError(f"bir {field} must be positive")
    return value


def _retrieval_document_from_mapping(document: Mapping[str, Any]) -> dict[str, Any]:
    normalized = dict(document)
    if "rank" in normalized and normalized["rank"] is not None:
        normalized["rank"] = _validate_non_negative_int(normalized["rank"], "retrieval document rank")
    if "score" in normalized and normalized["score"] is not None:
        normalized["score"] = _validate_non_negative_number(normalized["score"], "retrieval document score")
    return normalized


def _validate_currency(value: Any) -> str:
    if not isinstance(value, str):
        raise TypeError("bir currency must be a string")
    if not value:
        raise ValueError("bir currency must not be empty")
    return value


def _validate_model_prices(value: Any) -> tuple[tuple[str, _ModelPrice], ...]:
    """Validate and normalize the opt-in ``model_prices`` table."""

    if not isinstance(value, Mapping):
        raise TypeError("bir model_prices must be a mapping of model name to rates")
    if len(value) > _MAX_MODEL_PRICES:
        raise ValueError(f"bir model_prices must not exceed {_MAX_MODEL_PRICES} entries")
    normalized: list[tuple[str, _ModelPrice]] = []
    for model, rates in value.items():
        if not isinstance(model, str):
            raise TypeError("bir model_prices keys must be model-name strings")
        if not model:
            raise ValueError("bir model_prices keys must not be empty")
        normalized.append((model, _validate_model_price(rates, model)))
    normalized.sort(key=lambda item: item[0])
    return tuple(normalized)


def _validate_model_price(value: Any, model: str) -> _ModelPrice:
    """Validate one model's rate mapping into a frozen ``_ModelPrice``."""

    if not isinstance(value, Mapping):
        raise TypeError(f"bir model_prices[{model!r}] must be a mapping of rates")
    unknown = [key for key in value if key not in _MODEL_PRICE_RATE_KEYS]
    if unknown:
        listed = ", ".join(sorted(repr(key) for key in unknown))
        raise ValueError(f"bir model_prices[{model!r}] has unknown rate keys: {listed}")
    input_rate = value.get("input")
    output_rate = value.get("output")
    if input_rate is None and output_rate is None:
        raise ValueError(f"bir model_prices[{model!r}] must set at least one of 'input' or 'output'")
    validated_input = (
        _validate_non_negative_number(input_rate, f"model_prices[{model!r}].input") if input_rate is not None else None
    )
    validated_output = (
        _validate_non_negative_number(output_rate, f"model_prices[{model!r}].output")
        if output_rate is not None
        else None
    )
    currency = value.get("currency", "USD")
    return _ModelPrice(input=validated_input, output=validated_output, currency=_validate_currency(currency))


def _price_for_model(model: str, config: _Config) -> _ModelPrice | None:
    """Return the configured exact-name price entry, or ``None``."""

    for name, price in config.model_prices:
        if name == model:
            return price
    return None


_ENV_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})
_ENV_FALSE_VALUES = frozenset({"0", "false", "no", "off"})


def _env_value(name: str) -> str | None:
    """Return the stripped value of ``name``, or ``None`` when unset or blank."""

    raw = os.environ.get(name)
    if raw is None:
        return None
    stripped = raw.strip()
    return stripped or None


def _parse_env_bool(value: str, name: str) -> bool:
    """Parse a boolean-like environment value, rejecting ambiguous input."""

    normalized = value.strip().lower()
    if normalized in _ENV_TRUE_VALUES:
        return True
    if normalized in _ENV_FALSE_VALUES:
        return False
    allowed = ", ".join(sorted(_ENV_TRUE_VALUES | _ENV_FALSE_VALUES))
    raise ValueError(f"bir {name} must be a boolean-like value (one of: {allowed}), got {value!r}")


def _parse_env_sample_rate(value: str) -> float:
    """Parse a float sample rate from the environment and range-check it."""

    try:
        numeric = float(value.strip())
    except ValueError as exc:
        raise ValueError(f"bir BIR_SAMPLE_RATE must be a number between 0.0 and 1.0, got {value!r}") from exc
    return _validate_sample_rate(numeric)


def _parse_env_int(value: str, name: str) -> int:
    """Parse a non-negative integer capture-size limit from the environment."""

    try:
        numeric = int(value.strip())
    except ValueError as exc:
        raise ValueError(f"bir {name} must be a non-negative integer, got {value!r}") from exc
    return _validate_non_negative_int(numeric, name)


def _config_from_env() -> _Config:
    """Build the starting config from the ``BIR_*`` environment variables."""

    defaults = _Config()
    trace_path = _env_value("BIR_TRACE_PATH")
    capture_inputs = _env_value("BIR_CAPTURE_INPUTS")
    capture_outputs = _env_value("BIR_CAPTURE_OUTPUTS")
    service_name = _env_value("BIR_SERVICE_NAME")
    environment = _env_value("BIR_ENVIRONMENT")
    source = _env_value("BIR_SOURCE")
    disabled = _env_value("BIR_DISABLED")
    sample_rate = _env_value("BIR_SAMPLE_RATE")
    max_value_length = _env_value("BIR_MAX_VALUE_LENGTH")
    max_collection_items = _env_value("BIR_MAX_COLLECTION_ITEMS")
    return _Config(
        trace_path=Path(trace_path) if trace_path is not None else defaults.trace_path,
        capture_inputs=(
            _parse_env_bool(capture_inputs, "BIR_CAPTURE_INPUTS")
            if capture_inputs is not None
            else defaults.capture_inputs
        ),
        capture_outputs=(
            _parse_env_bool(capture_outputs, "BIR_CAPTURE_OUTPUTS")
            if capture_outputs is not None
            else defaults.capture_outputs
        ),
        service_name=(
            _validate_event_name(service_name, "BIR_SERVICE_NAME")
            if service_name is not None
            else defaults.service_name
        ),
        environment=(
            _validate_event_name(environment, "BIR_ENVIRONMENT") if environment is not None else defaults.environment
        ),
        source=(_validate_event_name(source, "BIR_SOURCE") if source is not None else defaults.source),
        enabled=(not _parse_env_bool(disabled, "BIR_DISABLED") if disabled is not None else defaults.enabled),
        sample_rate=(_parse_env_sample_rate(sample_rate) if sample_rate is not None else defaults.sample_rate),
        max_value_length=(
            _parse_env_int(max_value_length, "BIR_MAX_VALUE_LENGTH")
            if max_value_length is not None
            else defaults.max_value_length
        ),
        max_collection_items=(
            _parse_env_int(max_collection_items, "BIR_MAX_COLLECTION_ITEMS")
            if max_collection_items is not None
            else defaults.max_collection_items
        ),
    )
