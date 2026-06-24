# Shared contract fixtures

These four files are the **wire contract** shared by the `bir` product repo
(FastAPI server + Next.js dashboard) and the `bir-python` SDK (`bir-sdk`). Each
repo keeps its own copy under `tests/fixtures/`, and they **must stay
byte-for-byte identical**:

| Fixture | Canonical source | What it pins |
| --- | --- | --- |
| `event-schema-v1.json` | `bir-python` (SDK) | the `1.0` event schema the SDK produces and the server/dashboard consume |
| `valid-events.jsonl` | `bir-python` (SDK) | a representative trace (trace / span / tool-call / generation / score events) |
| `redaction-cases.json` | `bir-python` (SDK) | secret-redaction parity cases for the SDK's and server's independent redactors |
| `valid-experiment.json` | `bir` (product) | the canonical `/v1/experiments` upload shape the server locks |

"Canonical source" is the repo whose copy wins when re-syncing; the other repo
holds a copy. It is **not** where the file is verified — each repo verifies its
own copy in CI.

## Drift guard

Nothing kept these in sync before, so they drifted. Now:

- Each repo commits a checksum manifest, [`CHECKSUMS.sha256`](CHECKSUMS.sha256),
  listing the sha256 of every shared fixture. The two repos' manifests are
  identical.
- Each repo's CI runs `python scripts/fixtures.py check`, which fails if any
  local fixture no longer matches the manifest. This needs only the one repo
  checked out — CI never assumes the sibling repo is present.

## Changing a fixture

1. Edit it in its **canonical** repo (see the table above).
2. With both repos checked out side by side, run **from either repo**:

   ```bash
   python scripts/fixtures.py sync
   ```

   This copies each fixture from its canonical repo into the other and rewrites
   the identical `CHECKSUMS.sha256` in both. Commit the result in **both** repos
   (ideally in paired PRs). If the sibling isn't auto-detected, pass
   `--sibling /path/to/repo` or set `$BIR_SIBLING`.

Never hand-edit `CHECKSUMS.sha256`.

## Tradeoff

`check` verifies a repo against *its own* committed manifest and deliberately
does **not** check out the sibling repo (CI can't assume both are present). So
the guarantee is "fixtures match this repo's manifest," and cross-repo identity
holds because `sync` writes the *same* manifest into both repos at once.

- **Caught automatically:** editing a fixture without re-running `sync` — its
  checksum stops matching and that repo's CI goes red.
- **Not caught by CI alone:** deliberately hand-editing *both* a fixture and its
  manifest entry in one repo only. That bypasses `sync`, leaves the sibling
  unchanged, and shows up as an unpaired manifest diff in review.

The alternative — having CI clone the sibling and diff directly — was rejected
because it couples each repo's CI to the other's location and breaks the "no
assumption both are checked out" constraint. A committed manifest keeps each
repo's CI self-contained.
