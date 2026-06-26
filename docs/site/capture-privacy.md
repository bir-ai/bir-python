# Capture & Privacy

Input and output capture is disabled by default. Enable it globally with
`configure()` or for a single observed function:

```python
from bir import configure, observe

configure(capture_inputs=True, capture_outputs=True)

@observe(capture_inputs=True, capture_outputs=True)
def answer(question: str) -> str:
    return question
```

Capture stays disabled unless an environment variable, a `configure()` call,
or a per-observation/per-event argument explicitly enables it.

## Redaction

Before captured events are written, Bir redacts common secret-like fields such
as `api_key`, `authorization`, `password`, `secret`, and `token`.

Captured strings, fallback object representations, and captured error messages
are also scanned for common secret-like text patterns, including:

- Labeled and Bearer secrets.
- `sk-...` tokens and JWTs.
- AWS access key IDs (`AKIA...` and `ASIA...`).
- Google API keys (`AIza...`).
- Slack `xox*` tokens.
- GitHub `ghp_`, `gho_`, `ghs_`, `ghu_`, and `ghr_` tokens.
- Stripe secret and restricted keys (`sk_live_`, `sk_test_`, `rk_live_`, `rk_test_`).
- Azure storage-style account keys (88-character base64 ending in `==`).
- PEM private-key blocks (`-----BEGIN ... PRIVATE KEY-----` ... `-----END ... PRIVATE KEY-----`).

!!! warning

    Redaction is best-effort, not a guarantee that every credential or
    sensitive value will be recognized. Keep capture opt-in for sensitive
    payloads and review what your application records.

### Adding custom rules

Organizations often have domain-specific credential names or text formats Bir
cannot know in advance. `configure()` accepts two additive options for them:

```python
import re
from bir import configure

configure(
    additional_secret_keys=["ssn", "badge-id"],
    additional_redaction_patterns=[r"CUST-\d+", re.compile(r"acct-\w+", re.IGNORECASE)],
)
```

- `additional_secret_keys` redacts extra mapping keys by whole-name,
  case-insensitive match (`-` and `_` are treated as equivalent), so `"ssn"`
  redacts a `SSN` field but not an unrelated `session_id`.
- `additional_redaction_patterns` accepts regex strings and/or compiled
  `re.Pattern` objects, replacing every match with `[redacted]` in captured
  strings, repr fallbacks, error text, prompt and score metadata, integration
  inputs/outputs, and dataset/experiment files.

These options are **purely additive**: the built-in rules and the `[redacted]`
marker always apply and can never be disabled, replaced, or reordered. Entries
are validated and compiled once during `configure()`, so an empty key, empty
pattern, invalid regex, non-string entry, bytes pattern, or an over-large list
raises immediately. Passing either argument replaces the previously configured
additional rules of that kind (an empty iterable clears them); omitting it leaves
them unchanged.

Captured values are normalized to JSON-compatible data. Non-finite floats such
as `NaN` and `Infinity` are stored as strings, and deeply nested values are
truncated.

## Environment capture settings

```bash
export BIR_CAPTURE_INPUTS=true
export BIR_CAPTURE_OUTPUTS=true
export BIR_TRACE_PATH=/var/log/bir/traces.jsonl
```

Boolean settings accept `1`, `true`, `yes`, or `on` and `0`, `false`, `no`, or
`off`, case-insensitively. Variables are read once at import time and explicit
`configure()` arguments take precedence.

## Prompt and dataset payloads

Prompt template text, variables, and rendered values require their own explicit
capture flags. Dataset export redacts by default; use
`dataset.to_jsonl(..., redact=False)` only when raw export is intentional.

Score metadata is always passed through the same redaction rules before it is
written.
