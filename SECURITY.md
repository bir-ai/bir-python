# Security Policy

Bir is a local-first LLM tracing and evals SDK. Privacy is a design goal, not an
afterthought: by default Bir records the *shape* of your application's calls — names,
timings, token usage, and status — and **not** the inputs and outputs that flow through
them. This document describes what Bir does and does not capture, the guarantees and
limits of its redaction, and how to report a vulnerability.

For the full reference, see [Capture & Privacy](docs/site/capture-privacy.md); this page
is the security-focused summary.

## What is captured

- **Input/output capture is opt-in and disabled by default.** No function arguments,
  return values, prompts, or completions are written to disk unless you explicitly turn
  capture on — globally via `configure(capture_inputs=True, capture_outputs=True)`, per
  observation via `@observe(capture_inputs=True, ...)` or a per-event argument, or via
  the `BIR_*` environment variables. With capture off, Bir records only metadata about
  the call (names, IDs, timing, token counts, status), never the payloads.
- **A master kill switch disables tracing entirely.** `configure(enabled=False)` or
  `BIR_DISABLED=1` makes Bir a no-op — nothing is written at all — while your code
  continues to run.
- **Storage is local-first.** Traces are appended to a JSONL file on your own machine
  (default `.bir/traces.jsonl`). Bir does not transmit anything on its own; sending
  events to a server is a separate, explicit action you take.

## Redaction

When capture is enabled, every captured value passes through redaction **before** it is
written to disk and **before** any optional size truncation, so a secret is always
replaced before a value can be cut. This applies on every persistence path — captured
inputs, outputs, metadata, fallback object representations, and error messages.

**Built-in redaction rules always apply and cannot be disabled, replaced, or
reordered.** You can only *widen* coverage (see below).

Bir redacts two ways:

- **Secret-like field names.** A mapping key whose normalized name is or contains a
  known secret term has its value replaced — including `access_key`, `api_key`,
  `apikey`, `authorization`, `auth_header`, `client_secret`, `password`, `private_key`,
  `secret`, `token`, and the standalone names `auth`, `credential`, `credentials`, and
  `creds`.
- **Secret-like text patterns.** Captured strings (and fallback reprs and error text)
  are scanned for, and the matches replaced with `[redacted]`:
  - Labeled secrets (`api_key=...`, `password: ...`, `Authorization: ...`) and
    `Bearer ...` tokens.
  - `sk-...` tokens and JWTs (`eyJ....`).
  - AWS access key IDs (`AKIA...`, `ASIA...`).
  - Google API keys (`AIza...`).
  - Slack tokens (`xox[baprs]-...`).
  - GitHub tokens (`ghp_`, `gho_`, `ghs_`, `ghu_`, `ghr_`).
  - Stripe secret/restricted keys (`sk_live_`, `sk_test_`, `rk_live_`, `rk_test_`).
  - Azure storage-style account keys (88-character base64 ending in `==`).
  - PEM private-key blocks (`-----BEGIN ... PRIVATE KEY----- ... -----END ... PRIVATE
    KEY-----`).
  - Credit-card / PAN numbers: 13–19 digit runs (optionally space- or hyphen-grouped)
    that pass the Luhn checksum. The checksum gate leaves ordinary long integers, IDs,
    and phone numbers untouched.

### Limitations — redaction is best-effort

Pattern-based redaction is a safety net, **not a guarantee**. It recognizes common,
well-known credential shapes; it can miss novel, proprietary, or unusually formatted
secrets, and it cannot understand the meaning of free-form prose. Treat it accordingly:

- For highly sensitive payloads, keep capture **off**. The strongest guarantee is the
  data Bir never collects.
- Widen redaction for your own credential names and formats:

  ```python
  import re
  from bir import configure

  configure(
      additional_secret_keys=["internal_api_token", "x_company_key"],
      additional_redaction_patterns=[re.compile(r"ACME-[A-Z0-9]{20}")],
  )
  ```

  These options are strictly additive — they extend the built-in rules and can never
  weaken or bypass them.
- Review what your application actually records before relying on it.

### Redaction contract

The exact redaction behavior is pinned by a shared fixture
(`tests/fixtures/redaction-cases.json`) and guarded by `tests/test_redaction_parity.py`,
so the same inputs redact identically across the Bir SDK and the companion server
project. Changes that would weaken redaction are caught by that parity test.

## Supported versions

Bir is pre-1.0 and follows a small-release workflow. Security fixes are released against
the latest published version on PyPI (`bir-sdk`).

| Version | Supported |
| ------- | --------- |
| Latest `0.x` release | ✅ |
| Older releases | ❌ — please upgrade |

## Reporting a vulnerability

Please report security vulnerabilities **privately** — do not open a public issue for a
suspected vulnerability.

Use GitHub's private vulnerability reporting for this repository:

- **[Report a vulnerability](https://github.com/bir-ai/bir-python/security/advisories/new)**
  (the **Security** tab → **Report a vulnerability** on
  [bir-ai/bir-python](https://github.com/bir-ai/bir-python)).

Please include enough detail to reproduce the issue — affected version, a minimal
example, and the impact you observed. We will acknowledge your report, investigate, and
coordinate a fix and disclosure timeline with you. We appreciate responsible disclosure
and will credit reporters who wish to be named.
