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

!!! warning

    Redaction is best-effort, not a guarantee that every credential or
    sensitive value will be recognized. Keep capture opt-in for sensitive
    payloads and review what your application records.

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
