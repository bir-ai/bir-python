# Changelog

All notable changes to the Bir Python SDK are documented here.

This project follows a small-release workflow while the SDK is early-stage.
Before publishing, verify the release with the SDK release checklist in
`docs/SDK_RELEASE_CHECKLIST.md`.

## Unreleased

### Documentation

- The privacy page now says which *fields* redaction scans and which it does
  not. It enumerated what the recognizer catches in exhaustive detail and warned
  that recognition is best-effort, but never said where the recognizer is
  pointed. Sweeping one `sk-live-…` credential through every public surface that
  accepts a string and writes it to the store, 9 of 21 recorded it verbatim, and
  the split is coherent rather than accidental:

  ```
  scanned                                   recorded as given
  metadata, keys and values                 the event name, on every event type
  captured input and output                 model on a generation
  a retrieval's query and documents         service_name / environment / source
  a prompt's template, variables, rendered
  the message of an exception a call raised
  ```

  What the application passed as content is scanned; what identifies the record
  is written as given. No behaviour changed, which is the decision: a name and a
  model are how a record is found and read back — `bir traces --name`, the tree
  `bir show` prints, and the `model_prices` table that fills in a generation's
  cost all key on them — so replacing one with `[redacted]` would destroy the
  record without un-leaking anything, since the credential is already wherever
  it came from.

  `model` was weighed separately, being the one identity field a third party
  supplies wholesale: a generation's `model` is whatever the provider echoed
  back, as an event `name` from a framework bridge is whatever the framework
  announced. It is not scanned either. A credential arriving in either one was
  already sent to that third party, so the place to fix it is the call rather
  than the trace, and scanning `model` alone would leave a boundary that is
  harder to state than to defend. The page says this outright and tells a reader
  who needs an identity value scanned to put it in `metadata` as well.

  A new `tests/test_redaction_boundary.py` pins the whole table, both columns, by
  running the real primitives against a real store and reading the JSONL back.
  Previous redaction tests all ask which patterns are recognized; nothing asked
  which fields the recognizer is aimed at, so nothing would have noticed the
  boundary moving.

### Changed

- `bir eval-gate` now fails a candidate run whose examples failed. An aggregate
  score is a mean over the examples an evaluator actually *scored*, and a failed
  example carries no scores at all, so failures leave that denominator rather
  than lowering the mean. A candidate that broke therefore reported a *higher*
  mean than the baseline that worked, and the gate — whose whole job is failing
  a build on a regression — read only those means. Driven with a 20-example
  dataset the baseline answers correctly on the 10 easy examples and wrongly on
  the 10 hard ones, against a candidate that raises on the hard ones instead of
  answering them:

  ```
                                              before        after
  10 of 20 examples crashed                   exit 0        exit 1
  18 of 20 examples crashed                   exit 0        exit 1
  20 of 20 examples crashed                   exit 0        exit 1
  both runs failed the same 5 of 20           exit 0        exit 0
  candidate repaired the baseline's failures  exit 0        exit 0
  ```

  The first row is the sharpest: the gate did not merely pass it, it reported
  `"deltas": {"exact_match": 0.5}` and `"improved": ["exact_match"]` — half the
  run crashed and the diff called it an improvement.

  The documented escape hatch did not reach any of this. `--missing-score
  regress` fires on `baseline_only`, which is empty whenever the evaluator ran
  in both runs, so it covered only the total-failure case and only when asked
  for. Partial failure, the realistic shape, was invisible at every setting.

  The new rule is that the candidate failed a larger **share** of its examples
  than the baseline did. A share rather than a count, so two runs over datasets
  of different sizes stay comparable — 3 of 10 regresses against 10 of 100 even
  though it is fewer examples — and compared by cross-multiplication rather than
  division, so it is exact and a run of zero examples needs no special case: it
  has no share that can be larger than another's. Equal shares do not regress,
  which is what keeps a suite that fails the same examples on both sides from
  failing the gate every time.

  `compare_experiments(failed_examples=...)` and `--failed-examples
  {ignore,regress}` select the policy, in the shape `missing_score` already
  established. It defaults to `"regress"`. That is a breaking change to an exit
  code CI reads, which `0.x` permits in a minor release with the migration step
  named: pass `--failed-examples ignore` (or `failed_examples="ignore"`) to
  decide the gate on aggregate means alone, exactly as before.

  Strictness rather than a numeric allowance was chosen deliberately. The gate
  already fails on *any* score drop past `--tolerance`, whose default is `0.0`,
  so an escape hatch that turns a rule off is the house style; an error is not a
  score and has no natural tolerance unit to spend. A numeric allowance can be
  added later without moving anything, because it would narrow a rule that
  already exists.

  The diff carries the counts whatever the policy decides, because a build could
  not previously see them at all — the whole `to_dict()` payload had no key
  mentioning examples, errors, or counts, and `compare_experiments` loads the
  result file rather than the summary that records them. It now emits
  `baseline_example_count`, `baseline_error_count`, `candidate_example_count`,
  `candidate_error_count`, `failed_examples`, and `failed_example_regression`;
  the last reports the comparison itself, filled in under either policy, the way
  `baseline_only` is reported whatever `missing_score` says. `ExperimentResult`
  gained `example_count` and `error_count` properties for the same numbers, and
  the persisted summary is now built from them, so the two cannot drift.

  No test pinned the old behavior, which is why the whole suite passed against
  the new default before a line of it was written: every existing
  `compare_experiments` case in `tests/test_evals.py` and every `eval-gate` case
  in `tests/test_cli.py` builds both sides from examples that all succeeded, so
  both error counts were zero and the new term never fired. The new cases drive
  the shape above, the equal-share and fewer-failures cases that must not fire,
  a share compared across datasets of different sizes in both directions, the
  total-failure case, the empty-candidate boundary the `missing_score` policy
  owns instead, the counts under both policies, and a diff built by hand without
  the new fields, which decides exactly as it did before they existed.

- The OTLP exporter emits the current spelling of two attribute names alongside
  the ones it already wrote. `otel.py` and `README.md` claimed the exported
  attributes "follow the GenAI semantic conventions where they exist", and two
  of them had been renamed underneath that claim. Measured against
  `opentelemetry-semantic-conventions` 0.65b0, the release installed alongside
  `opentelemetry-sdk` 1.44.0:

  ```
  superseded                  current                         where
  deployment.environment  ->  deployment.environment.name     Resource
  gen_ai.system           ->  gen_ai.provider.name            generation spans
  ```

  The consequence was quiet: a backend keying its environment facet on
  `deployment.environment.name` or its provider facet on `gen_ai.provider.name`
  saw no value, and the traces looked like they lacked the metadata rather than
  like they spelled it differently.

  Both spellings are now written with the same value, rather than one replacing
  the other. The `otel` extra accepts `opentelemetry-sdk>=1.20`, and a backend
  anywhere in that range may key on either name; emitting only the current
  spelling would leave the facet empty for anyone who had not migrated, which is
  the same silence pointing the other way. The cost is two attributes — one on
  the `Resource`, one on a generation span — and dual emission is what the
  conventions prescribe for a rename in progress. The superseded names go when
  the extra's floor rises past the release that carries only the replacements.

  `gen_ai.request.model`, `gen_ai.usage.input_tokens`, and
  `gen_ai.usage.output_tokens` were re-checked against the same release and are
  unchanged. Every GenAI constant in 0.65b0 carries a deprecation note, but for
  those three it records the move to the GenAI conventions repository rather
  than a new spelling — which is why the presence of a deprecation note is not
  what this was measured on.

  The version is now named wherever the claim is made — the module docstring,
  `README.md`, and a new "Which semantic conventions" section in
  `docs/site/cli-env.md` with the table above — so the next audit can re-check
  it against a release rather than against "current". New tests read the
  constants out of the installed conventions package and compare them to what
  the exporter writes, so a future rename fails rather than passing quietly; the
  case pinning the superseded spellings says in its assertion message that the
  transition can end once those constants are gone.

### Added

- `run_experiment()` and `run_experiment_async()` take `total_timeout`, a limit
  on the run. `timeout` bounds an example and never bounded the run: a task that
  outran it keeps its worker until it returns, so a run against a backend that
  stopped answering takes as long as the tasks do however small `timeout` is.
  Measured on 60 examples against a task that never returns, `max_workers=4`,
  5 ms per-example timeout:

  ```
                                    run time   rows
  timeout only                      280.07 s   60 of 60
  timeout and total_timeout=2         2.01 s    4 of 60
  ```

  The previous release bounded the *threads* a serial run abandons; this bounds
  the run. They are different limits and neither implies the other, which the
  rejected attempt to stretch the per-example limit over the run made plain.

  Three decisions, recorded because each had a defensible alternative.

  **The examples it does not reach are absent, not failures.** They did not fail,
  and recording them as errors would make the experiment look worse than the code
  it measured — it would also move the gate's error rate for a reason that has
  nothing to do with the code. Absent is the shape `raise_on_error` already
  produces when it ends a run early, so `example_count` already means "the rows
  this run produced" rather than the dataset size, and the rows are the leading
  examples in dataset order. A third status would have said it best and is ruled
  out: `tests/fixtures/valid-experiment.json` is a wire contract whose canonical
  copy lives in the product repo, so it cannot change here alone.

  **Stopping honors `raise_on_error`.** With the default `True` it raises
  `TimeoutError: experiment stopped after 600s with 143 of 500 example(s) run`,
  because a run that quietly returned a third of its dataset would compare
  against a full baseline as though nothing were wrong. With `False` it returns
  what ran and says so on the `bir` logger.

  **The async runner gets it too.** It cancels cleanly so it has no stuck
  workers, but a run against a backend that stopped answering is just as
  unbounded there, and the two runners are documented as matching.

  One limit is documented rather than hidden: a run can be stopped between
  examples, not inside one, because Python cannot interrupt a running task. With
  `timeout` also set, that granularity is one example's timeout.

### Fixed

- A concurrent run that is waiting on stuck workers now says so instead of
  looking hung. `max_workers > 1` bounds its threads by construction, but a task
  that outran its timeout holds its pool slot until it returns, so a queued
  example waits for one and the run takes as long as the stuck tasks do: 60
  examples against a 20 s task with `max_workers=4` and a 5 ms timeout take
  about 280 s. It now reports once, on the first example that waits longer than
  a whole timeout, naming the workers it is waiting on.

  Bounding that wait — the obvious fix, and the one the roadmap asked for — was
  implemented, measured, and rejected. Two slow examples saturating a two-worker
  pool made **four of ten** queued fast examples be recorded as failures they
  would not have had, because a worker frees a moment after the bound expires:

  ```
  2 slow (0.30 s) + 10 fast, max_workers=2, timeout=0.05 s
                          succeeded   recorded as never run
  bounded wait            6           4
  waiting as before       10          0
  ```

  Refusing an example that would have passed is worse than a slow run, and there
  is no bound that separates "returns in 0.30 s" from "never returns" without
  waiting 0.30 s to find out. The serial path can bound its wait because it has
  sixteen slots of headroom before it refuses anything; a pool's bound *is*
  `max_workers`, so there is none. Bounding the run rather than the example would
  need a budget for the run, which `run_experiment()` does not take — that is a
  feature, not a fix, and it is recorded on the roadmap as one.

  `tests/test_evals.py::test_threaded_timeout_excludes_worker_queue_time` is what
  caught it. It was written for the property that a queued example is not charged
  for its wait, and the bound broke it by refusing the example outright.

- A serial run with a timeout no longer holds one thread per timed-out example.
  Python cannot stop a thread, so a task that outran its timeout keeps running
  until it returns; the serial path gave every example its own worker and
  abandoned it, with nothing bounding how many piled up and `max_workers=1` the
  default. A hung backend is exactly what the timeout is for, so the dataset was
  the only limit. Measured on 400 examples against a task that never returns,
  with a 5 ms timeout:

  ```
                                              run time   peak threads
  before: one worker per example              2.70 s     402
  bounded, waiting open-endedly for a slot    ~750 s      18
  after:  bounded, waiting the example's own  2.76 s      18
  ```

  The middle row is the decision. Bounding the workers means an example can find
  none free, and waiting open-endedly for one hands back exactly what `timeout`
  exists to prevent — sixteen tasks have to return before the seventeenth example
  can start, which took that run from 2.70 s to roughly 750 s. So the wait is
  bounded by the example's own timeout: it was allowed that much time, and it
  spends it waiting for a worker instead of running on one.

  What that costs is that an example can be recorded as failed without having
  run, and it says so rather than claiming its task overran: `no worker was free
  within Ns; N task(s) from earlier timed-out examples are still running and
  cannot be stopped`. Sixteen is the bound because it has two jobs — high enough
  that a run whose examples occasionally overrun never queues behind them, low
  enough that a run whose examples all hang holds a fixed handful. It is not a
  knob; a caller who wants more concurrency has `max_workers`, whose pool bounds
  itself the same way.

  A run that returns with tasks still going says so once on the `bir` logger,
  with the count, the way a paused store and a rotation gap already report.
  Nothing changes for a run that meets its timeout: measured on 40 examples that
  all finish, no extra threads, no warning, and a run with `timeout=None`
  abandons nothing at all.

  Nothing pinned this. The timeout cases assert the recorded error, the preserved
  order and the excluded queue time; none counted threads, and none used a
  dataset large enough to notice. The new cases run four times the bound against
  a task that hangs and check the thread count, the distinct message for an
  example that never started, that the run stays inside its own time budget, the
  single report, and that a healthy run and a mostly-healthy one are untouched.

- Three tests that only Windows could fail now pass there. `os.fsdecode` uses
  `surrogateescape` on POSIX and `surrogatepass` on Windows, and the latter
  refuses an invalid start byte outright, so `os.fsdecode(b"doc-\xff.pdf")`
  raised `UnicodeDecodeError` on the Windows legs of two report tests. They want
  the string a filesystem walk leaves, not the decoding, so they now write
  `"doc-\udcff.pdf"` directly — the same value, on every platform.

  The third asserted that a repaired store no longer contains the truncated
  fragment, as a *substring*. The fragment is the head of an event line, and
  every event written in the same clock tick shares that head, so on a platform
  whose clock is coarse enough for three events to share a timestamp the check
  failed on the clock rather than on the repair. It now asserts the fragment is
  not present as a whole line, which is exact, and that the store ends in a
  terminator.

- `scripts/benchmarks.py --smoke` runs again. Its `send_batched` case stubs the
  transport so the measurement is Bir's batching rather than a server, and it
  stubbed `urllib.request.urlopen` — which sends no longer call. The stub was
  bypassed, the benchmark tried to reach `http://127.0.0.1:9` for real, and the
  CI step failed with `ConnectionRefusedError`. Its fake response also needed the
  optional byte count `read` now receives. Both match what the test suite's fakes
  already do.

  The guard that should have caught it is the interesting part.
  `tests/test_benchmarks.py` ran the harness end to end on `--only
  trace_disabled`, which touches no transport, so the one case that stubs an SDK
  internal was never executed — exactly the rot its own docstring warns about
  ("nothing fails when a case stops measuring what it claims to"). It now runs
  every case in the smoke subset, which costs about a second at `--repeat 1`,
  plus the cases held out of that subset when their optional extra is installed.
  Reverting the stub target makes the new test fail with the CI error.

- The two report-mode tests added with the staged report write now skip on
  Windows, where they could not pass. `os.chmod` there sets only the read-only
  flag, so a file asked for `0o600` still reports `0o666` and the mode a rename
  carries cannot be observed at all — `assertEqual(S_IMODE(...), 0o600)` failed
  on the Windows leg of CI and nowhere else. They carry the same
  `skipIf(sys.platform == "win32", "POSIX permission bits")` guard, and the same
  reason, that `tests/test_store_permissions.py` has always used. The behaviour
  they pin is unchanged and still covered on Linux and macOS.

- A batch response is now checked against the request it answers, rather than
  only for being the right shape. `accepted` is printed by `bir send` and gates
  pipelines, `skipped` is computed from it, and `event_ids` is what
  `--mark-sent` writes to the sidecar, so a number or an id a server invented
  was not cosmetic. Driven against a local server answering a three-event send
  with a body of its choosing:

  ```
  server said                                  before                      after
  {"accepted": 99, "event_ids": ["a"]}         accepted=99 attempted=3  exit 0   refused
  {"accepted": -5, "event_ids": []}            accepted=-5 skipped=8    exit 0   refused
  {"accepted": 3, "event_ids": ["x","y","z"]}  accepted=3  exit 0                refused
  ```

  The second line is arithmetic no store can produce: more events skipped than
  were attempted, from a negative acceptance. The third recorded three ids that
  name no event in the store, which `--mark-sent` would have remembered as
  delivered for good. A reply is now refused when `accepted` is negative or
  exceeds what was sent, when `event_ids` is longer than the batch, or when it
  names an id that was not posted; the message says which, and quotes the body.

  The success body is also bounded now, by what the request justifies rather
  than by a constant. `response.read()` took the whole thing, deliberately — a
  batch's accepted ids are legitimately longer than any message would carry —
  but nothing tied "legitimately long" to the batch, so one reply could be any
  size at all. It was the only value in the SDK that could: the loaders stream,
  `prune` is disk-backed, and the upload spool is disk-backed. The limit allows
  an id's worth per event sent plus an envelope, so a proportionate reply is
  still parsed whole and one that cannot be the ids of what was sent is not read.
  Measured against a server answering a **one-event** send with a 200 MB body,
  with the server in a process of its own so the client's cost is its own:

  ```
                                            peak RSS before   peak RSS after
  response.read() whole, as before          23 MB             755 MB
  bir send, bounded by the request          33 MB              34 MB
  ```

  The per-event fallback got the same treatment, so a 404 on the batch endpoint
  is not a way round either check: a reply claiming a count outside `0..1` for a
  single posted event is refused, and its body is bounded the same way.

  A refusal raises, so nothing is reported as accepted. With `batch_size` set,
  batches are posted in sequence and each one's ids are recorded as it
  completes, so a refusal part-way leaves the batches that already succeeded
  marked and raises for the rest, exactly as any other mid-run failure does.

  Two existing tests changed rather than being deleted. The one pinning that a
  large accepted response is parsed whole pinned the decision this reverses; it
  now pins both halves of the new boundary — a 500-event reply read whole, and a
  200 KB reply to a one-event send refused. Two fakes that answered with invented
  ids (`["a", "b"]`) now echo the ids their store actually holds, which is what a
  server does. Every fake response's `read()` also takes the optional byte count
  `http.client.HTTPResponse.read` has always taken, since the transport now
  passes one.

- A redirect no longer turns `bir send` into a report of a delivery that never
  happened. `urlopen` uses an opener carrying `HTTPRedirectHandler`, which
  answers a 301, 302, or 303 on a POST by reissuing the request as a **GET with
  no body** at whatever host the `Location` names. The events reached nobody,
  and the unconfigured host's reply was parsed as the batch result. Driven with
  two loopback servers, the configured one answering every POST with a 302:

  ```
                                          before                   after
  bir send --mark-sent                    exit 0                   exit 1
                                          accepted=99 attempted=3  bir: … HTTP 302 …
  the redirect target received            GET /v1/events/batch     nothing
  event bytes delivered anywhere          0                        0 (and it says so)
  traces.jsonl.sent recorded              ["not-a-real-id"]        unchanged
  ```

  A second `bir send` printed the same success, and nothing on stdout, stderr,
  or the exit code separated it from a real upload of every event in the store.
  307 and 308 already raised, because urllib's handler refuses to convert those
  — that was the handler's rule rather than a decision Bir had made, and all
  five now behave the same way.

  Sends go through an opener built with that handler replaced by one that
  follows nothing, so a 3xx falls through to the default error handler and
  arrives as an `HTTPError` with its status and headers intact. The message
  names the status and the `Location` it declined, bounded to 500 characters
  like every other server-chosen string an error carries, and says what to fix:

  ```
  bir: bir server at http://127.0.0.1:9000/v1/events/batch answered HTTP 302 with
  a redirect to http://elsewhere/v1/events; bir does not follow redirects, so
  nothing was sent. Point the server URL at the address that serves the API.
  ```

  A redirect is not retried: it is a configuration problem, not a transient one,
  so it raises immediately like a 4xx. `send_experiment()` shares the opener and
  the refusal; it was previously saved only by a stricter response check that
  happened to reject the redirect target's reply.

  `build_opener` keeps every other default handler, so proxies configured through
  the usual environment variables still apply. One consequence is documented
  rather than hidden: an opener installed globally with
  `urllib.request.install_opener()` no longer steers where Bir sends, because
  Bir now uses its own rather than urllib's process-wide one.

  No test could have caught this. The suite stubbed `urllib.request.urlopen` in
  all 67 places it exercised sending, and `grep -rn "HTTPServer" tests/` found
  nothing — the real opener had never run against a real server. Those stubs now
  patch Bir's own transport seam (the opener's `open`) rather than a standard
  library function the code no longer calls, and a new
  `tests/test_send_over_http.py` stands up loopback servers and drives the real
  thing: every redirect status refused with nothing reaching the target, a
  redirect with no `Location` header, an over-long `Location` bounded in the
  message, `send_experiment` refusing the same way, a successful batch whose
  bytes really cross a socket, a 4xx raised once against a 5xx retried three
  times, and a 404 batch endpoint falling back to one request per event.

- A run no longer accepts two evaluators with the same name. Every score is
  filed under its evaluator's name and nothing else, so two sharing one were
  averaged together into a number no example was given by anything — and the
  gate reported a different one beside it in the same diff:

  ```
  evaluators passed in:  [regex_match(r"^alpha"), regex_match(r"gamma$")]
  per-example scores:    [('regex_match', 1.0), ('regex_match', 0.0)]
  aggregate_scores:      {'regex_match': 0.5}
  the report's rows:     | regex_match | 0.50 |
                         | q0 | success | regex_match=1.00 regex_match=0.00 | - |
  compare deltas         {'regex_match': 0.5}   improved: ['regex_match']
  example_deltas         {'regex_match': {'q0': 1.0, 'q1': 1.0, ...}}
  ```

  The aggregate moved 0.5 and every example moved 1.0, for the same evaluator
  name, in one diff: `_example_scores_by_evaluator` keys by name into a dict, so
  the last score written wins there while the mean takes both.

  Nothing chose this. Thirteen of the fourteen evaluator factories default
  `name` to the factory's own, so the collision arrives from the most ordinary
  pairing there is — `field_equals("answer")` beside `field_equals("citation")`,
  or two `regex_match` patterns — and no error, warning, or documentation
  mentioned it.

  `run_experiment` and `run_experiment_async` now raise before the run touches
  its output file, naming the repeated name and the keyword-only `name=` that
  every factory already takes. Before the file, because the result writer opens
  its output for truncating write: a rejected run would otherwise have emptied
  the previous experiment at that path.

  Rejected where the list is built rather than reported later, because that is
  where the fix is: by the time a merged mean exists it has lost which
  evaluators it came from. The check is on writing only — an experiment recorded
  before it existed still loads and still compares, its two evaluators sharing
  one aggregate as they always did, since refusing to read a file already on
  disk would be worse than reporting what it holds.

  No test pinned the old behaviour and no documentation mentioned the rule. The
  new cases cover both factories that collide, both runners, a rejected run
  leaving an earlier one at that path byte-identical, `name=` producing two
  aggregates and two per-example delta keys, four different factories not being
  mistaken for a repeat, and a run recorded before the check still loading.

- `bir experiment-report --output` no longer destroys the report it cannot
  finish writing. It wrote with `Path.write_text`, which opens the destination
  for truncating write before a byte is encoded or reaches the disk, so any
  failure left a zero-byte file where a report was. Driven on a 1 MB HFS+ volume
  padded to 98 KB free, re-rendering a 451 KB report over a 1,540-byte one:

  ```
                                    before                   after
  report.html after the failure     0 bytes, exit 1          1,540 bytes, exit 1
                                    bir: [Errno 28] No space left on device
  ```

  It now writes through a sibling temporary file and renames it into place, the
  way the experiment summary, the trace store's sent-ID sidecar, and `prune`
  already do. It was the only file-producing path in the SDK that did not.

  The staged file is created with the umask's mode rather than the owner-only
  one those three use: `docs/site/capture-privacy.md` promises that
  `experiment-report --output` "writes to a path you named and keeps the umask's
  mode, because those are deliberate handoffs rather than Bir's own store". A
  rename carries the staged file's mode to the destination, so an existing
  report's mode is copied onto the staged file first — a plain write left it
  alone, and a narrowed report must not silently widen on the next render.

- A report can no longer hold text that nothing can write out. `os.fsdecode`
  returns surrogate-escaped text for a filename that is not valid UTF-8, so an
  `example_id` taken from a filesystem walk — the ordinary shape for a
  document-ingestion dataset — carried code points no encoder accepts.
  `render_experiment_report()` returned a string that neither `--output` nor
  stdout could write, in both formats:

  ```
                                    before                         after
  render_experiment_report(html)    4,835 chars, UnicodeEncodeError  4,840 chars, encodes
  render_experiment_report(md)      2,434 chars, UnicodeEncodeError  2,439 chars, encodes
  bir experiment-report --output    exit 1, report destroyed         exit 0
  ```

  Surrogates are now escaped as `\udcff` where every other experiment-derived
  string is escaped for its format. Escaped rather than dropped or replaced by a
  placeholder, for the reason the CLI escapes a control character instead of
  stripping it: a reader wants to see that something odd was recorded, and the
  escape names which code point it was. Ordinary non-ASCII text is untouched —
  `günlük` and `日本語` render as themselves — and the fast path returns an ASCII
  string without scanning it.

  Fixed in the renderer rather than at the write, so the public
  `render_experiment_report()` returns something its caller can write, and so
  the stdout path is covered too: it survives here only because this machine's
  stdout uses `surrogateescape`, and would have failed on one that does not.

- An append onto a store whose last write never finished no longer destroys the
  event it is recording. An append lands at the byte after whatever is already
  there, so a file ending mid-line ran the fragment and the incoming event
  together into one line that parsed as neither. The event being recorded had
  been written whole and was lost anyway, and the damage stopped being a
  trailing fragment — which `bir prune` can drop — and became a permanent
  unreadable line in the middle of the store, which every writing command
  refuses by design:

  ```
                                before                        after
  after the interrupted write   6 lines, 1 unreadable         6 lines, 1 unreadable
  after 1 more append           6 lines, 1 unreadable         6 lines, 0 unreadable
                                t0 t1 t2 t3 t4 <unreadable>   t0 t1 t2 t3 t4 first-after-recovery
  after 3 more appends          9 lines, 1 unreadable         9 lines, 0 unreadable
  bir prune afterwards          exit 1, Invalid JSON          exit 0
  ```

  This is the ordinary sequence after a full disk, not a corner: recording is
  paused and retried per event, so the first append to succeed after any space
  frees is the one that gets eaten. Driven on a 1 MB HFS+ volume filled by an
  actual workload — 2,710 lines with one unreadable — then 60 KB freed and
  recording resumed: 2,532 lines, **none** unreadable, and `load_events()`
  returning all 2,532. Before, that store held an unreadable line for good.

  The unfinished bytes are now removed before the append rather than written
  onto. That is the same judgement `bir prune` already makes about the same
  bytes — they were never a whole event and no reader could ever read them —
  made earlier and for the same reason. Truncating rather than starting a new
  line was the choice: a new line keeps the fragment, and once a later append
  terminates it, it is no longer the shape prune can drop, so the store would
  carry it forever.

  It is not silent. The repair is reported once on the `bir` logger at
  `WARNING`, naming the byte count, alongside the outage and recovery messages
  that already exist. Once per occurrence is once per outage: the repair leaves
  the file ending in a terminator, so the next append finds nothing to drop.

  The file's last byte is read rather than a flag remembered, because the write
  that left the fragment need not have been this process's — an OOM kill ends the
  process that tore the line, and several processes may share one store. Reading
  it on every append cost 20–24% of the recording path, which is too much for a
  guard against something rare:

  ```
                       baseline    every append     guarded by size
  trace_recorded       70.4 µs     86.9 µs (+23%)   74.8 µs
  generation_recorded  145.1 µs    176.7 µs (+22%)  152.5 µs
  store_rotation       171.1 µs    206.0 µs (+20%)  191.2 µs
  ```

  The guarded column is best-of-four runs at `--repeat 9`; the same case varied
  74.5–80.5 µs across consecutive runs on this machine, so read it as "a few
  percent" rather than as a figure to compare against a later one. The
  every-append column is far enough outside that band to be the real difference.
  The size is read from the path rather than the descriptor, so the check does
  not depend on ``os.fstat``, which a caller may be substituting for reasons of
  its own.

  So the byte is read only when the file is not exactly where this process left
  it: the first append to a store, an append after one that did not finish,
  another process's write, a rotation, an edit. An append that finds the size it
  wrote skips it. That is not a heuristic with a hole — anything that can put a
  fragment there also changes the size — except an in-place edit that replaces
  the final terminator without changing the length, which is noted where the
  guard is defined.

  The repair runs before rotation rather than after, so a fragment is fixed
  instead of being renamed into a sibling: nothing appends to a rotated file, so
  one carried in there would stay unreadable for good.

  No test pinned any of this. `tests/test_unwritable_store.py` drove failing
  writes but never let one succeed afterwards on a file left unterminated, and
  `tests/test_damaged_store.py` truncates a store and only reads it. The new
  cases interrupt a real append part-way through and then record again, cover a
  fragment left by an earlier run, a file that is nothing but an unfinished
  write, the rotation ordering, the traced call being unaffected throughout, and
  a healthy store that must be touched by none of it.

- `bir prune` can now reclaim space on the store a full disk produces. The
  full-disk path is documented and works — the append fails, the traced call is
  unaffected, one `ERROR bir:` line says recording is paused — but what it leaves
  behind is a half-written final line, and prune refused to read a store with a
  line it could not parse. The one command that reclaims space was unusable on
  the one store that most needs it, and `--dry-run` refused too, so there was not
  even a preview. Driven on a 1 MB HFS+ volume, recording with the default
  unbounded configuration until the volume filled:

  ```
  4,000 traced calls returned normally, 2,709 traces written
  store: 901,120 bytes, 2,710 lines, 2,709 parse, 1 does not, ends with newline: False

                                     before                       after
  bir prune --keep-last 5 --dry-run  exit 1, Invalid JSON …       exit 0, removed=2704 kept=5
  bir prune --keep-last 5 --yes      exit 1, Invalid JSON …       exit 0, 876 KB reclaimed
  bir traces (afterwards)            exit 1, Invalid JSON …       exit 0, 5 rows
  ```

  The opening is one line wide and is decided by the file's own bytes rather than
  by a flag. An event is appended as one whole line ending in a newline, so a
  file whose last line has no newline ends in a write that never finished: those
  bytes were never a complete event, no reader could ever read them, and dropping
  them loses nothing that was recorded. Iterating a text file yields that
  fragment as the only line without a terminator, so recognizing it costs no
  extra read and cannot misfire on a line further up.

  Everything else still refuses, and is pinned that way. A line that was written
  whole and cannot be parsed refuses whether it is last or in the middle — only
  the missing terminator proves nothing was recorded there. A fragment in a
  rotated sibling refuses, because nothing appends to a rotated file. And `bir
  send` refuses either one: sending is not the repair path, and dropping or
  re-sending a record is not a decision a transport makes on its own. Prune
  first, then send.

  Automatic rather than a `--repair` flag, because the recovery has to work for
  someone who does not already know a flag exists — a full disk is not when you
  read the manual. It is not silent, which is the other half of that choice:
  every run that finds the line says so on stderr (`would drop …` under
  `--dry-run`), and `--json` carries `incomplete_tail_bytes`. That count is
  separate from `removed_events` because the line is not an event — no selection
  filter named it — and it is already inside `bytes_reclaimed`, which measures
  the file. The line goes even when the selection removes no traces, so a repair
  never depends on `--keep-last` happening to match.

  One limit is documented rather than fixed. Prune stages the surviving lines in
  a sibling file and renames it over the original, which is what makes an
  interrupted prune safe, so it needs room for what survives. On a volume with
  literally no bytes left it fails at the staging file with a clear `[Errno 28]`
  naming it, and `--dry-run` still reports what is reclaimable. Free anything at
  all and the real run goes through: on the volume above, 57 KB of headroom was
  enough to reclaim 876 KB.

  This reverses a decision rather than fixing an oversight, so the tests that
  pinned it were rewritten rather than deleted: `tests/test_damaged_store.py`
  now pins the repair, the dry-run preview, the drop that happens even when the
  selection is empty, the whole-line refusal in both positions, the rotated
  sibling, `send` staying strict, and the reported byte count.

- A server's response body can no longer repaint the terminal running `bir send`.
  The previous release established that recorded text must not be able to steer
  the terminal reading it and escaped every table cell and header. The CLI's
  error channel did not go through that: `main` printed an exception message
  raw, and one of the strings that reaches it is a remote host's response body,
  embedded verbatim. Driving `bir send` against a server that chooses its own
  body:

  ```
                                          before                after
  400 whose body is an ANSI repaint       105 bytes, 4 ESC      117 bytes, 0 ESC
  200 whose body is an ANSI repaint       105 bytes, 4 ESC      117 bytes, 0 ESC
  400 whose body is 2 MB                  2,000,053 bytes       567 bytes
  ```

  The repaint body erases its own line, moves the cursor up, erases again, and
  prints `accepted=1 attempted=1 skipped=0` — what a successful send prints. A
  failed send could be made to look like a successful one on the operator's
  screen.

  Escaping is applied where the diagnostic is printed rather than where the
  message is built. A message mixes Bir's own words with parts it does not
  control — a path, a trace or experiment id, a store's field names, a server's
  body — and the print site is the one place that covers all of them, including
  messages nobody has written yet; the sweep found `Dataset.__post_init__`
  interpolating example ids from a file without `repr`, and it is covered by
  this without being touched. Every `bir: …` line now goes through one helper
  and is a single line whatever it contains, so a body cannot forge a second
  one. The OTLP install hint, the only message that had embedded a newline
  itself, is now one line rather than an exception to the rule.

  Escaping stops at the CLI and does not move into the exceptions. A
  `RuntimeError` a program catches carries what the server actually said, so a
  caller logging or matching it sees the bytes rather than a rendering of them —
  the same split already drawn for recorded values, which are stored as written
  and escaped when printed.

  A response body is separately bounded, which belongs where the string is read
  rather than where it is shown: `_read_http_error_body` now stops one character
  past the limit instead of reading whatever arrives, and every message carries
  at most 500 characters plus `…[truncated]`. The bound is on what a message
  shows, not on what is parsed — a successful batch response is still read
  whole, since a large batch's accepted ids are legitimately longer than any
  message would carry, and cutting it before `json.loads` would refuse a good
  response.

  No test pinned the old behaviour. The existing control-character cases cover
  `traces`, `show`, `tail`, and `experiment-show` on stdout; nothing covered
  `bir: …` on stderr and nothing drove `bir send` against a hostile body. The
  new cases drive both repaint bodies and the oversized one through the CLI,
  pin the bound and the untouched read at the transport, and assert that a
  legitimately large accepted response still parses.

- `bir tail` follows the store across a rotation. It is documented as printing
  new events "as they are written", and with `configure(max_bytes=...)` set it
  did not: rotation renames the active file away and starts a new one, and the
  follow held only a byte offset into a path. Same workload every run — 200
  traces, 400 events, 5 ms apart, followed by a `bir tail` subprocess for the
  whole run — changing only the rotation limit:

  ```
                        before                          after
  no rotation           400 lines, 200/200 traces       400 lines, 200/200 traces
  rotation at 64 KB     316 lines, 158/200 traces       400 lines, 200/200 traces
  ```

  Nothing was reported and nothing was duplicated; a quarter of the events
  simply never appeared. Two things were lost at once. The events appended
  between the last 0.5 s poll and the rename went with the renamed file, which
  nothing ever re-read — `--include-rotated` taught every other read command
  about rotated siblings, but a follow has no such flag. And the replacement was
  usually already longer than the stale offset by the next poll, so the one
  rotation check there was — `if size < offset` — read it as ordinary growth and
  seeked past the beginning of a file it had never read.

  The follow now identifies the file its offset belongs to and re-checks that
  every poll, so a replaced file is recognized however large it has grown. On a
  rotation it locates that file among the siblings, drains what it still owed,
  and reads any files that rotated in between, in write order, before continuing
  with the new active file — several rotations can land inside one poll
  interval. Only files newer than the one it was reading are touched, so a
  follow still never prints events that predate it.

  Device and inode do not identify a file well enough to do this on Linux.
  Rotation frees an inode every time it drops a file past `backup_count`, and
  ext4 hands that number to the new active file created a moment later, so the
  replacement can arrive wearing the number of the file the follow was reading
  and be waved through. APFS and NTFS allocate strictly increasing ids and never
  reproduce it, which is why it showed up only on some of CI. The identity is
  therefore device, inode, and the file's recorded first line — an event
  carrying its own id, which no other file has. A rename changes none of the
  three, which is what still lets a rotated file be recognized under its new
  name, and an empty file has no first line to compare, so a store's first event
  is not mistaken for a replacement.

  One gap cannot be closed and is now reported instead of silent. Rotating more
  times than `backup_count` keeps deletes the file the follow was reading before
  it can be read, and nothing can print what is no longer on disk. The same
  workload at a 2 KB limit rotates roughly twenty times per poll interval and
  the store itself ends up holding 15 of the 200 traces; that run now prints
  `bir: … was replaced; the events it still held were not shown` on stderr — off
  the event stream on stdout — where before it printed nothing and said nothing.
  A `bir prune` that rewrites a followed store, and any other replacement of the
  active file, report the same way: from inside a follow they leave the same
  absence, and the notice says what was observed rather than guessing a cause.

  No test pinned the old behaviour. The existing cases drive `_follow_trace`
  against a single appended file and the `tail` command against an un-rotated
  one, so nothing rotated the store while a follow was running. The new cases
  rotate through the real writer rather than renaming by hand, and cover a
  rotation per poll, a file drained after being rotated away mid-interval, two
  whole files rotating inside one interval, the reported gap, and a rewrite in
  place. The inode-reuse case is pinned by forcing every file to report the same
  device and inode, so it reproduces on a filesystem that would never produce it
  naturally rather than only on the CI legs that do. A store that does not exist
  yet and one whose first event fills an empty file are pinned alongside them.

- A framework object that raises while Bir reads it no longer fails the call it
  was recording. The previous release established that rule for the seven direct
  provider wrappers and guarded every read in `_common.py`. The framework event
  bridges were left out, and they read framework objects through their own
  private copies of the same helper — with the `try`/`except` missing. Measured
  by driving every event-bridge callback with an object whose reads raise:

  ```
  -- langchain
     on_llm_end(Hostile)                               RAISED RuntimeError: llm_output exploded
     on_retriever_end([Hostile])                       RAISED RuntimeError: page_content exploded
  -- llamaindex
     on_event_end('llm', response=Hostile)             RAISED RuntimeError: text exploded
     on_event_end('retrieve', nodes=[Hostile])         RAISED RuntimeError: node exploded
     on_event_end('retrieve', nodes=[HostileMethods])  RAISED ValueError: Node must be a TextNode to get text.
  -- pydantic_ai
     on_start/on_end(Hostile)                          RAISED RuntimeError: get_span_context exploded

  6 of 36 callback sequences raised
  ```

  CrewAI, AG2, Haystack, and the OpenAI Agents processor were clean across the
  same sweep; they read through the guarded `_value` throughout.

  The raising accessor is not a synthetic shape. It raises exactly what
  LlamaIndex's own `NodeWithScore.get_text()` raises for a node that is not a
  `TextNode`, which is the ordinary case for a multi-modal retriever. And
  LlamaIndex does not absorb it: `CallbackManager.on_event_end` calls each
  handler with no `try`/`except`, so the exception lands in the application's own
  `query()`. LangChain does absorb it — its callback manager logs and re-raises
  only when `handler.raise_error` is true, and this handler declares `False` — so
  there the cost was silence rather than a failure.

  Both costs were real. Measured on the store, with an ordinary `@observe()`
  function running afterwards in the same context:

  ```
  llamaindex: retrieve event, node text accessor raises
                        before                            after
    events written      1  ['span/application_work']      3  [retrieval, root, application_work]
    traces loadable     0                                 2
    events with no root 1  ['application_work']           0
  ```

  The LlamaIndex case lost the retrieval event *and* the trace root, and left the
  context entered so the application's own next event joined a trace that would
  never exist — the exact state `_lifecycle.py` was written to prevent, reached
  from a direction that module cannot see. The LangChain case kept its roots and
  dropped the retrieval event silently, leaking its registry entry until the
  1,024-run bound evicted it.

  The previous fix went in one function short of the problem: it patched
  `_response_text` and left `_node_text`, the identical accessor loop four
  functions below it, untouched. Every framework-object read in both bridges now
  goes through the shared guarded `_value`, both accessor loops go through one
  `_accessor_text`, and the two shadow copies of `_common`'s helpers that had
  drifted out of `langchain.py` are deleted rather than fixed. Reading a
  framework's `__str__` is guarded the same way, by a shared `_text`.

  The case lives in the shared bridge conformance contract rather than against
  one framework, so it holds for every handler and any added later: without the
  guards it fails 13 of its 14 cases across all seven bridges. The retrieval
  paths, which the shared generation run does not reach, are pinned in the
  per-framework modules beside the payload parsing they belong to.

  A document or node that cannot be read now contributes what is still known
  rather than nothing: a LangChain document keeps its rank, and a LlamaIndex node
  keeps its id and score, while the readable ones beside them record in full.

  Measured cost, interleaved against the previous code and compared best-of:
  reading five retrieved nodes went from 34.7 µs to 28.9 µs and reading a
  response's text from 0.82 µs to 0.71 µs, because recognizing a plain string
  payload key no longer walks the attribute reads an enum member needs; token
  usage is not distinguishable; and reading five LangChain documents went from
  2.67 µs to 4.96 µs, which is 0.46 µs per document on a callback that follows a
  vector search. Over a whole bridge run the difference is not measurable at all
  — the store write dominates it.

- A provider response that raises while Bir reads it no longer fails the call
  that returned it. The call had already succeeded; reading its result to build a
  record turned it into an exception at the caller. Measured against every direct
  provider bridge:

  ```
  model_dump() raises  broke: anthropic, bedrock, cohere, google, litellm, mistral, openai
  dict() raises        broke: anthropic, bedrock, cohere, google, litellm, mistral, openai
  property raises      broke: anthropic, litellm, mistral, openai
  ```

  The asynchronous wrappers behaved the same way.

  `_response_output` called `model_dump()`, then `dict()`, then `dict(response)`
  — three pieces of code the provider owns, none of them guarded — and `_value`
  read attributes with `getattr(source, key, None)`, which absorbs a missing
  attribute but not a property that raises.

  This is the invariant the SDK had already established twice, with the guard on
  the wrong end of it. A store that cannot be written is caught and reported; a
  value whose own `__repr__` raises is caught inside capture. Both protect the
  last step. Reading the provider's response is the first step, it is the one
  recording path that runs a third party's code, and it was the one without a
  guard.

  Every read that can execute somebody else's code is now guarded and falls back
  to what is still known. When a conversion raises, the response object itself is
  recorded rather than a marker, so capture takes its `repr` under its own guard
  and a response that cannot serialize still records what it can. The two bridges
  that read responses outside the shared helpers — LangChain's own copy of the
  conversion and LlamaIndex's `get_content`/`get_text` accessors — go through the
  same guards.

  A genuinely failing provider call still raises, unchanged. The call failing is
  the provider's answer; only reading its result for a record is bookkeeping.

  The case is asserted in `tests/integration_contract.py` rather than against one
  provider, so it holds for every wrapper family at once and for any added later:
  without the guards it fails 70 contract cases across every family.

  Measured at +0.14 µs per response read (0.512 → 0.655 µs for one output
  conversion and two attribute reads), which is not distinguishable at the level
  of a traced provider call.

- `bir tail` now shows events when its output is redirected. It showed nothing at
  all. Measured by following a store while 400 events were written to it:

  ```
  bir tail --path traces.jsonl > tail.out
    t+1s   0 bytes, 0 of 400 events
    t+2s   0 bytes
    t+3s   0 bytes
    after SIGTERM   0 bytes

  PYTHONUNBUFFERED=1, same workload
    22,690 bytes, 400 of 400 events
  ```

  Not a partial delay: nothing arrived. Nothing in `cli.py` flushed, so the
  rendered lines sat in the block buffer Python uses for a stdout that is not a
  terminal — and that buffer is larger than a following session ever fills, so
  the only thing that drained it was the process exiting. `tail` is the one
  command built to run until it is interrupted, and anything automated stops it
  with `SIGTERM`, which discards the buffer without unwinding. A redirected
  `bir tail` could therefore produce nothing for its entire life, which broke the
  ordinary way to use a follow command: `bir tail | grep error`.

  Each batch is flushed before the poll loop sleeps.

  No existing test could see this. `_follow_trace` is exercised with a
  `StringIO`, which has no buffering to get wrong, so those cases passed either
  way — the seam that makes the loop testable is what hid the defect. The new
  case drives the CLI as a subprocess against a real pipe and reads the output
  while the follow is still running.

- A derived cost can no longer fail the call it was recording. `configure(model_prices=...)`
  validates every rate as finite and non-negative, and `set_usage()` validates
  every token count the same way, but the arithmetic between them was not
  bounded. Measured:

  ```
  configure(model_prices={"m": {"input": 1e308, "output": 1e308}})
  @observe def chat(): … set_usage(input_tokens=1000, output_tokens=1000)

  chat() -> ValueError: bir input_cost must be finite
  ```

  The traced function lost its return value to an exception raised by the
  bookkeeping about it. This was the last path in the generation exit sequence
  that could still raise at the caller; the store write beside it has been
  wrapped since the previous release. An unrepresentable derived cost is now left
  off the event instead, which is the rule that already applied to a store that
  cannot be written.

  Two smaller things came out of the same measurement, both of which lost a whole
  event rather than raising. `set_cost()` and `set_usage()` derive `total_cost`
  and `total_tokens` by adding the two sides they were given, and neither sum was
  validated — so two finite costs adding to infinity produced an event that could
  not be serialized, which was then reported as the *trace store* failing, since
  that is where the failure surfaced. Both sums are now validated like a supplied
  total. An explicit `set_cost()` or `set_usage()` therefore still raises, which
  is right: those values came from the caller, and a total that cannot be
  represented is a mistake worth reporting rather than absorbing. Only the
  derived path, where nobody passed anything, stays silent.

  The finiteness check deliberately is not `math.isfinite`. A Python int is
  unbounded and always finite, and `math.isfinite` raises `OverflowError` on one
  too large to convert to a float — so a price of `10**400` would have crashed
  the check that exists to prevent crashes. It is recorded normally.

- `bir export-otel` no longer reports success when it exported nothing. Measured
  against an endpoint with nothing listening, and again against a server
  answering every POST with HTTP 500:

  ```
  $ bir export-otel --endpoint http://127.0.0.1:9/v1/traces --json
  { "endpoint": "…", "spans": 8, "traces": 4 }
  exit=0
  ```

  Zero spans were delivered in either run, and the default run against the dead
  endpoint spent 58 seconds retrying before printing that. `bir send`, which
  exists to do the same job, already reported both failures correctly with a
  non-zero exit.

  Three things dropped the failure. `SimpleSpanProcessor` calls the exporter once
  per span and discards the `SpanExportResult`; `TracerProvider.force_flush`
  returns a bool nobody read; and the reported number counted spans *built*, so
  it was the same whether or not anything arrived — which also made the
  documented "number of spans exported" describe something the function did not
  measure.

  The exporter's own answers are now counted. `bir export-otel` reports the spans
  the endpoint accepted, and an export that did not deliver everything exits
  non-zero with a message naming the endpoint and how much arrived, writing
  nothing to stdout so a pipeline reading the `--json` contract never sees a span
  count for spans that are not there. A partial delivery is a failure too.

  `export_traces_to_otlp()` now returns the accepted count and raises
  `RuntimeError` on an incomplete export. Its signature is unchanged. Exporting
  is an operation invoked for its effect, so this is the rule `send_events`,
  `prune`, and the loaders already follow, and it is the only way a caller
  replaying a store into a collector can learn the data did not arrive.

  A batch counts as delivered unless the exporter *said* it failed. OpenTelemetry
  asks `export` to return a `SpanExportResult`, but an exporter that returns
  something else is not evidence of a failure, and reporting one would break a
  working pipeline over a technicality the OpenTelemetry SDK itself ignores. Only
  a stated failure — what the OTLP exporter returns once its retries are
  exhausted — is treated as one. `force_flush` is still called, but its result is
  not the signal: with `SimpleSpanProcessor` the spans are already exported
  synchronously, so it answers `True` without consulting the exporter.

  Measured cost: `export_otel` 82.4 µs/unit against 81.0 before, one delegation
  per span, with peak memory unchanged at 363 KiB.

### Security

- The files Bir writes for itself are now created readable only by the user who
  ran the process. They inherited the umask, which on the common default meant
  world-readable:

  ```
  dir  0o755  .bir
  file 0o644  .bir/traces.jsonl
  file 0o644  .bir/experiments/e.jsonl
  file 0o644  .bir/experiments/e.summary.json
  ```

  Those files hold captured inputs and outputs, and redaction is documented as
  best-effort, so on a shared CI runner or a multi-user host that was the wrong
  default — and it was inherited rather than chosen, and written down nowhere.

  The trace store, its size-rotated siblings, the `.sent` upload sidecar, and
  experiment result and summary files are created `0600`. The mode is passed to
  `os.open` rather than applied with `chmod` afterwards, so there is no moment
  when the file exists and anyone can read it.

  Three limits on that, each deliberate. The `.bir` directory keeps the umask's
  mode, so a sibling process can still list the store and simply cannot read it.
  Existing files are never changed, because the mode is applied only as a file is
  created and a user who widened one meant to. And what the user asks Bir to
  export — `dataset.to_jsonl(...)`, `bir experiment-report --output ...` — keeps
  the umask's mode, because those are deliberate handoffs to a path the caller
  named rather than Bir's own store.

  A umask can narrow this further and can never widen it. A deployment that needs
  the store readable by another uid can widen the file once after it is created
  or relax the umask of the process creating it; `docs/site/capture-privacy.md`
  now says so, which is the part that was missing.

  Two tests that simulated a failed summary write by patching `Path.write_text`
  now fail the staged write itself, since the summary is written through a handle
  so its mode can be set as it is created. What they assert is unchanged.

- Recorded text can no longer steer the terminal reading it. Control characters
  were printed exactly as stored, so a trace recorded under the name
  `\x1b[2K\x1b[31mFAKE ERROR\x1b[0m` came back out of `bir traces` intact — shown
  here through `cat -v`, which is the only reason the escapes are visible:

  ```
  START                             STATUS   DURATION  EVENTS  NAME
  2026-08-08T03:05:25.719851+00:00  success  0.0ms     1       ok
  2026-08-08T03:05:25.719254+00:00  success  0.2ms     1       ^[[2K^[[31mFAKE ERROR^[[0m
  ```

  On a real terminal `\x1b[2K` erases the line the cursor is on, so a row could
  wipe the row above it, and `\x1b[31m` repaints what follows. A name containing
  a newline split the table row in two on its own.

  Names are not always literals: a framework bridge passes the tool the model
  chose, an application passes a route from a request, and `generation(model=…)`
  passes what the provider returned. Those are outside inputs arriving in a field
  the CLI prints.

  Control characters are now escaped as `\x1b`, `\x0a`, and so on when the CLI
  renders for a person — the tables, the `show` tree, the `tail` stream, and the
  `experiment-show` header. Escaping rather than stripping keeps the fact that
  something odd was recorded visible.

  Nothing about recording changes. The stored event keeps exactly what the
  application passed, `--json` hands a parser the value as written, and the
  loaders return it unchanged; escaping there would corrupt what a pipeline
  reads. This is display spoofing rather than execution, which is why it is a
  smaller fix than the credential items above.

  Measured at +0.12 µs per rendered cell, about 1.2 ms added to listing 2,000
  traces. Ordinary text skips the scan entirely: `str.isprintable` is false for
  every character the pattern matches and runs in C, which measured 4.4x cheaper
  per cell than reaching for the pattern every time.

- A secret used as a mapping **key** is now redacted. It was the one position a
  secret survived. Sweeping twelve value shapes rather than credential formats,
  eleven were replaced and one was not:

  ```
  {"sk-ABCD…": "value"}     -> {"sk-ABCD1234efgh5678": "value"}   leaked
  ("sk-ABCD…", "b")         -> ["[redacted]", "b"]
  {"sk-ABCD…"}  (a set)     -> ["[redacted]"]
  Cfg(api_key='sk-ABCD…')   -> "Cfg(api_key=[redacted])"
  b"sk-ABCD…"               -> "b'[redacted]'"
  ```

  It reached the trace file: a function taking `{"sk-…": {"remaining": 3}}` and
  returning `{"sk-…": "exhausted"}` recorded the key verbatim in both `input` and
  `output`. `_safe_key` rendered a key with `str` and nothing else, while its
  immediate neighbour `_safe_repr` did pass its text through the rules. Keys
  already drove detection — reading the key is what makes `{"api_key": …}`
  replace its value — so the key was read, just never rewritten.

  The original key still decides the value's fate; only the recorded form of the
  key changes. Keys that redact to the same marker are kept apart with a counted
  suffix (`[redacted]`, `[redacted] (2)`) instead of overwriting one another: a
  mapping held by credential would otherwise have collapsed to a single entry.
  That also fixes a collision that predates this change, where `{1: "a", "1": "b"}`
  silently kept only the second entry.

  Redaction is now asked for only when it could do something. A captured mapping
  is mostly short identifiers — `field_0`, `role`, `content` — that none of the
  fourteen built-in rules can match, so a cheap gate looks for the markers those
  rules need and skips the rule set when none is present. Custom patterns are
  unaffected: they are the caller's own, may match text carrying no built-in
  marker, and always run, which is what keeps them purely additive.

  Measured across three interleaved rounds: `capture_redaction` **33.6 → 23.2
  µs/unit**, so ordinary capture is about 31% *faster* than before this change,
  because most text no longer pays for rules that cannot match it. A mapping of
  200 short keys costs 67.5 → 106.0 µs, roughly 0.2 µs per key, which is what
  redacting keys costs. `capture_large_value` is unchanged within noise.

  The gate has to stay a superset of the rules or a rule silently stops working.
  Two things hold it there: it gates the shared redaction entry point rather than
  only the key path, so every redaction test in the suite runs through it, and a
  test asserts the property against the rules run *ungated* — the obvious
  spelling, asking whether redaction changed the text, passes for exactly the
  rules the gate has stopped reaching.

- `Cookie` and `Set-Cookie` headers no longer pass through untouched. A session
  cookie is a live credential — it is what a request presents *instead* of a
  password — so this was the same class as the `Authorization` header repaired
  below, in the other header an HTTP client sends:

  ```
  Cookie: session=abc123; auth_token=xyz789   -> unchanged
  Set-Cookie: sid=9f8e7d; HttpOnly            -> unchanged
  ```

  Neither rule that looks like it should have caught this did. The labeled rule
  lists `token`, but `\b` does not fall between `auth` and `token`, so
  `auth_token=` was never a match, and `session=` is not a listed name at all.

  Every cookie's value is now replaced and the names are kept, so the trace still
  says which cookies a request carried. Cookie attributes are kept too, because
  they describe the cookie rather than authenticate anything:
  `Set-Cookie: sid=9f8e7d; Path=/; HttpOnly` records as
  `Set-Cookie: sid=[redacted]; Path=/; HttpOnly`. Attributes are recognized by
  name rather than by position, which is what lets one rule cover both headers
  without knowing which it is looking at — `Cookie` carries only cookies,
  `Set-Cookie` carries one cookie and then attributes. A bare flag with no value
  carries nothing and is kept whatever it is called. An `Expires` date ends the
  run of pairs at its comma, and everything after it is left as written.

  The run of pairs is bounded by cookie syntax rather than by the end of the
  line, so a header quoted inside a sentence does not take the sentence with it.
  Values are replaced by splitting the matched run rather than by repeating a
  capture group, since a regex hands back only the last repetition.

  Measured cost: `capture_large_value` 85.9 ms against 79.3 before, the price of
  one more pass over the value — three spellings were compared and the one that
  shipped has the cheapest no-match scan. `capture_redaction` is 31.0 µs/unit
  against 30.3. A test pins the linearity, because a `;`-separated list invites
  the repeated group that made the URI rule quadratic.

  `tests/fixtures/redaction-cases.json` gains two cases, synced into the `bir`
  repo with `scripts/fixtures.py sync`.

- Built-in redaction now recognizes two credential formats it was missing.

  GitHub's fine-grained personal access tokens (`github_pat_…`) were not
  redacted. The rules covered only the classic `ghp_`, `gho_`, `ghs_`, `ghu_`,
  and `ghr_` prefixes, so they read as current while missing the token type
  GitHub now steers users toward. Both families share one pass.

  Passwords inside a connection string were not redacted either, and the same
  secret was treated inconsistently depending on how it was written:
  `password=hunter2` was replaced, and the same password in
  `postgres://admin:hunter2@db.internal:5432/prod` was not. Connection strings
  reach traces through config objects and through the error messages the client
  libraries raise when they cannot connect. The password is now replaced and the
  scheme, user, and host are kept, so the trace still says which database the
  call was reaching. A URL with no password (`https://user@example.com`) and an
  ordinary `host:port` are untouched — neither carries a password to hide — and a
  token placed in the *user* half instead (the `<token>:x-oauth-basic@`
  convention) is covered by the token-shape rules that run after it.

  The URI rule matches from `://` rather than from the scheme, which is a cost
  decision. The obvious spelling, `[A-Za-z][A-Za-z0-9+.-]*://`, is quadratic:
  every letter starts an attempt that runs its greedy class to the end of the run
  and backtracks over it looking for the `://`. Measured on one 64,000-character
  alphanumeric run — the shape a base64 body has — that spelling took 4,745 ms
  and grew 60x for 8x the input, against 0.03 ms and 7x for the one that shipped.
  A test pins the linearity the way the private-key rule's does.

  Measured cost: `capture_large_value` 79.4 ms against 79.2 before, so the
  per-character axis is unchanged; `capture_redaction` is 30.9 µs/unit against
  30.1, the per-call cost of one added pass.

  `tests/fixtures/redaction-cases.json` gains four cases, synced into the `bir`
  repo with `scripts/fixtures.py sync`.

- An `Authorization` header no longer leaks its credential for any scheme other
  than `Bearer`. RFC 7235 writes the header as a scheme followed by the
  credential, and the rule named `Bearer` explicitly so it could be preserved.
  Every other scheme therefore fell into the credential position and was replaced
  while the credential behind it survived:

  ```
  Authorization: Bearer abc123secret        -> Authorization: Bearer [redacted]
  Authorization: Basic YWRtaW46aHVudGVyMg== -> Authorization: [redacted] YWRtaW46aHVudGVyMg==
  Authorization: Token 9f8e7d6c5b4a32109f8e -> Authorization: [redacted] 9f8e7d6c5b4a32109f8e
  ```

  The surviving base64 in that second line decodes to `admin:hunter2`. The
  documentation already told users Bir redacts `authorization`, and this is the
  one layer the SDK keeps non-disableable.

  Only the text path was affected. A header captured as a mapping key
  (`{"authorization": "Basic …"}`) went through the whole-value key rule and was
  always replaced. A header captured as text — one entry in a list of headers,
  a client library's error message, the `repr` of a request — kept its
  credential, and those are the ordinary shapes.

  The scheme is now recognized and kept, and the credential after it is replaced,
  so a trace still records which authentication a call used. `Bearer`, `Basic`,
  `Token`, `ApiKey`, `NTLM`, `Negotiate`, and `SSWS` carry one credential;
  `Digest`, `Hawk`, `Signature`, and `AWS4-HMAC-SHA256` carry a comma-separated
  parameter list, and the whole list is replaced because the secret part of it —
  a Digest `response`, a SigV4 `Signature` — comes last rather than first. A
  header with no scheme still has its value replaced, and surrounding text is
  untouched: a header quoted inside a sentence does not take the sentence with
  it. `Bearer` is byte-for-byte unchanged.

  Redaction is now idempotent here, which it was not. The optional scheme group
  could backtrack past the already-redacted guard and consume the scheme, so
  re-redacting `Authorization: Bearer [redacted]` produced
  `Authorization: [redacted] [redacted]`. That was reachable: stored experiment
  rows are re-redacted when read, so `load_experiment()` and `bir experiment-show`
  destroyed the scheme on every read.

  `tests/fixtures/redaction-cases.json` gains six cases covering the schemes and
  the idempotence, synced into the `bir` repo with `scripts/fixtures.py sync` as
  the shared-fixture process requires. The server keeps its own independent
  redactor against that fixture and has the identical rule, so it needs the same
  fix; the new cases are what will tell it so.

  Measured no cost: the two rules are one pattern, so a captured value is still
  scanned for the header once. `capture_redaction` is 30.0 µs/unit against 30.9
  before and `capture_large_value` 78.8 ms against 78.1, both within noise, and
  the repeated group over an auth-param list stays linear in the size of the
  value.

### Fixed

- Experiment results now survive a process that is stopped without unwinding.
  `run_experiment` already wrote each example's row as the run proceeded, but the
  handle kept default buffering for the whole run, so the rows reached the disk
  only when the file was closed. Measured on a 20-example run stopped after 10
  examples had completed:

  ```
  SIGTERM: result rows on disk = 0   (10 examples had completed)
  SIGINT:  result rows on disk = 10
  SIGKILL: result rows on disk = 0
  ```

  `SIGINT` survived because Python unwinds and closes the file. `SIGTERM` is how
  an orchestrator stops a process — a pod eviction, `docker stop`, a cancelled CI
  job — and Python's default handler exits without unwinding, so the buffer went
  with it. The cost is proportional to how long a run takes, which for an
  evaluation over a model API is the point of the feature: an hour of paid model
  calls could be on the wrong side of that buffer. The trace store beside it was
  never affected, because each append opens, writes, and closes.

  A finished example's row is now flushed before the next example starts. The
  threaded and async runners were worse off still — they collected every result
  and wrote the file in one pass at the end, so an interruption lost the whole run
  — and they now stream rows too, in dataset order as that order completes.
  Waiting on the dataset-order prefix rather than on the whole set is what makes
  this possible without changing the documented ordering: rows, results, and
  aggregates still follow dataset order regardless of completion order.

  Flushing pushes the bytes to the operating system, which is what makes them
  outlive the process; it deliberately stops short of `fsync`, which would cost a
  disk round trip per example to also survive a machine-level crash, and which the
  trace store does not pay either. The write cost stays proportional to the number
  of examples rather than to their size.

  The `.summary.json` sibling is still written only when the run ends, so an
  interrupted run has result rows and no summary — nothing claims it completed.
  `load_experiment()` reads the rows; `list_experiments()` and `bir experiments`
  list summaries and so will not show it.

  One consequence is worth naming: all three runners now open the result file when
  the run starts rather than when it ends, so a cancelled async run leaves an empty
  result file where it previously left none.
  `test_cancellation_cleans_up_children_without_writing_summary` pinned the old
  behavior and now pins the new one, keeping its real subject — the summary must
  not appear — and a new test covers what the change was for: a cancellation after
  some examples have finished keeps their rows.

- A trace store that cannot be written no longer fails the call it was recording.
  Recording appends to a local JSONL file, and an `OSError` from that append
  propagated out of the context manager, so a call that had already succeeded
  raised at its caller. Measured against a trace directory with no write
  permission, every recording entry point broke: `@observe` (sync, async, and
  generator), `trace()`, `span()`, `generation()`, `tool_call()`, `retrieval()`,
  and `score()`. A function that charged an order returned `PermissionError`.

  The conditions are deployment conditions rather than programming mistakes — a
  read-only container filesystem, a full disk, a `.bir/` owned by another user, an
  unmounted ephemeral volume — and recording is bookkeeping about a call rather
  than part of it. A failed append is now reported instead of raised: the traced
  call returns its own result, and a call whose body raised still raises its own
  exception, which is the precedence the SDK already applied when there was a
  competing exception to prefer.

  It is not swallowed. Bir reports on its own `bir` logger, at `ERROR` when
  writing starts failing and at `WARNING` when it recovers, naming the path, the
  error, and how many events were dropped in between. The report is emitted on the
  transition only, so a persistent outage costs one message rather than one per
  event of every trace — and, incidentally, an application whose logging handler is
  itself traced cannot recurse. `logging.getLogger("bir")` routes or silences it.
  This is the SDK's own operational log and is unrelated to `bir.logging`, which
  stamps trace ids onto the application's records.

  Operations invoked for their effect are unchanged and still raise: `bir prune`,
  `bir send`, `load_events()`, and `load_traces()`, where the read or write *is*
  the requested operation.

  This reverses a decision rather than filling a gap. `test_storage_errors_are_not_swallowed`
  pinned the old behavior deliberately; it is now
  `test_storage_errors_are_reported_not_swallowed`, keeping the intent — the error
  must not vanish — while changing how it travels, since destroying a caller's
  result is not a way of reporting anything.

- `install_trace_id_filter()` now stamps the log records an application actually
  emits. With no argument it attached the filter to the root *logger*, and a
  logger's filters run only for records that logger itself creates: a record from
  `logging.getLogger("myapp")` propagates to the root logger's handlers but never
  through its filters, so it was never stamped.

  That cost log lines rather than just correlation. The documented format string
  asks for `%(bir_trace_id)s`, and a record without the attribute makes the
  formatter raise — `logging` then drops the line and writes the error to stderr.
  Running the module docstring's own example verbatim printed nothing but a
  `ValueError: Formatting field not found in record: 'bir_trace_id'` for every
  application log line; only records created directly on the root logger came
  through.

  A no-argument call now attaches to the root logger's handlers, which is where
  propagated records are seen, and to the root logger itself for records created
  on it. Configure logging first: a handler added afterwards carries no filter,
  and a call that finds no handlers at all warns (`RuntimeWarning`) naming the fix
  instead of attaching to nothing quietly — that ordering, install-then-configure,
  is the one the docs used to show. Passing an explicit logger or handler is
  unchanged, and the returned filter is one instance attached to each target, so
  the documented `removeFilter` still works on each.

  The gap survived because nothing emitted through the default:
  `test_install_defaults_to_root_logger` asserted only that the filter appeared in
  `root.filters`, and every behavioral case attached it directly to the logger
  under test — the one arrangement where a logger filter does run. The new cases
  emit from child loggers through a root handler, and one runs the documented
  recipe end to end and asserts nothing reached stderr.

### Changed

- `bir export-otel` streams the store instead of loading it. It was the last read
  path that materialized whole traces: on a 1.54 MiB store of 4,000 events across
  2,000 traces it peaked at 18.95 MiB, nine times `bir stats` and twelve times the
  store on disk, because the exporter walked the traces twice — once to resolve
  the Resource-level environment and source, once to emit — and so had to hold
  them.

  Both passes now read the store directly. The first reads only trace roots, which
  is all the Resource attributes come from, and counts each trace's events on the
  way; the second groups events into traces and releases each one as soon as its
  count is reached. Bir's own share of the peak fell from 10.05 MiB to 0.35 MiB,
  and the command as a whole from 18.95 MiB to 9.50 MiB — the remainder is the
  OpenTelemetry SDK's span objects and its OTLP encoder, which this change does
  not touch. `scripts/benchmarks.py` gained an `export_otel` case beside the other
  CLI cases so the ceiling stays visible; it sits outside the CI smoke subset
  because it needs the optional `otel` extra.

  Counting events is what makes releasing a trace early safe. The obvious signal —
  a trace is complete when its root arrives — holds for a store this SDK wrote,
  since the root event is written when the trace closes, but not for one where the
  root comes first, and the shared `tests/fixtures` store is written that way.

  Exported spans, their attributes, and the reported counts are unchanged, and a
  test pins that a path and the equivalent loaded traces export the same thing.
  What changed is the order traces are exported in: completion order rather than
  sorted by start time, which is what lets them be released one at a time. The
  events inside a trace are ordered exactly as before. `export_traces_to_otlp()`
  keeps its signature and its `int` return; passing already-loaded traces still
  exports them as given, since they have already been read.

### Fixed

- The `--mark-sent` upload sidecar is now bounded by the store instead of by
  everything ever sent. It records the event IDs a server accepted so later sends
  can skip them, and nothing ever removed one — while `prune`, the operation
  whose whole job is bounding local state, left it alone. Recording 2,000 traces,
  sending with `mark_sent=True`, pruning to `--keep-last 1`, and repeating grew
  it linearly and forever: 156 KB after the first cycle, 780 KB and 20,000 IDs
  after the fifth, against a store of 695 bytes holding one trace. Every entry
  named an event that no longer existed, and every send read, merged, sorted, and
  rewrote all of them.

  A successful prune now drops IDs for events the store no longer holds, since
  such an ID can never be matched by a later send. The same cycle now ends every
  round at 94 bytes and 2 IDs — the two events of the one trace prune kept.

  Compaction reads every file for the trace path, including size-rotated siblings
  a prune without `include_rotated` left alone, so an ID is dropped only when its
  event is in none of them. It runs under the lock ordering the storage lock
  already documented (trace lock first, sidecar lock second) and only after the
  prune has succeeded, and the sidecar stays advisory: a dry run changes nothing,
  a store that never used `mark_sent` gains no sidecar or lock file, and a
  sidecar that cannot be read or written leaves the prune's own result untouched.
  Re-sending after a compaction still skips everything still in the store.

- One damaged experiment summary no longer hides every experiment beside it. An
  experiment is a result JSONL plus a `*.summary.json`, and the listing parses
  every summary in the directory, so one it could not read raised for the whole
  directory — and because `experiment-show` and `experiment-report` find their
  target through the listing, all three commands failed over an unrelated third
  file. Measured with three experiments whose summaries were valid and one
  truncated to half its length, `bir experiments`, `bir experiments --json`, and
  `bir experiment-show <intact id>` all exited 1.

  The SDK could produce that state itself: `_write_experiment_summary` truncated
  in place with no temp-and-rename, unlike the sent-ID sidecar and prune, both of
  which stage and replace. A summary is now staged and renamed, so a killed or
  failed write leaves the previous summary readable instead of destroying it — a
  write failing part-way used to leave a valid 255-byte summary at 127 bytes and
  take the directory's listing with it.

  `experiments`, `experiment-show`, and `experiment-report` also accept
  `--skip-invalid`, matching the trace read commands: unreadable summaries are
  skipped, the count and the first message go to stderr so `--json` stays
  parseable on stdout, and the intact experiments are listed. A summary that
  parses but fails validation is skipped the same way.

  The default is unchanged and still strict. `list_experiments()` and
  `load_experiment_summary()` still refuse a directory they cannot read
  completely, because a program building on them should not receive a silently
  partial list. A damaged *result* file needed no change: it is read only by the
  experiment that owns it, so it already failed just that one `experiment-show`
  and left the listing alone — which is what the summary path now matches.

- A framework run whose end callback never arrives no longer strands everything
  recorded after it. A bridge enters a Bir context in one callback and exits it
  in another, and while the run is open it owns the ambient trace context — which
  is the feature, since an application's own `@observe()` work nests under it.
  When the terminal callback never came, the run stayed open for the life of the
  context: its event was never written, and every event recorded afterwards
  joined a trace whose root did not exist. Driving each bridge's own contract
  driver with no end callback and then recording one unrelated `@observe()` call,
  six of the seven declared bridges left the store with no trace roots at all —
  `bir traces` printed "No traces found", `bir show` reported the id as not
  found, and `load_traces()` returned `[]` while the events sat on disk.

  Two recoveries now apply. A handler that is told when a run is top-level
  reclaims a root it is still holding for an earlier one, because new top-level
  work means the earlier run is gone; LangChain, OpenAI Agents, and Pydantic AI
  report this, so a long-lived handler recovers on the next request and the
  abandoned run becomes a complete, findable trace instead of stranded events.
  CrewAI and LlamaIndex report no parent when a run begins, which makes a nested
  run indistinguishable from one following an abandoned run, so they do not
  guess: turning nested work into a second root would be worse than the leak.

  Every handler's open runs are also bounded at 1024, and an evicted run is
  written rather than dropped. A handler driven with 20,000 abandoned runs held
  20,001 entries and 21.8 MiB before; it now holds 1,024 and peaks at 1.4 MiB.
  Both paths record `metadata.abandoned` (`superseded` or `evicted`), so a run no
  framework ever closed is distinguishable in the store from one that closed
  normally.

  What neither recovers is a context that sees no further bridge callbacks:
  nothing distinguishes a run that is still executing from one that is gone, and
  guessing would break the nesting above. That state is now reported by the
  reader rather than silent.

  The five callback handlers' identical private `_ActiveRun` classes and plain
  registry dicts are replaced by one shared implementation, so the bound and the
  reclaim exist once rather than five times. The bridge conformance matrix gained
  cases for both, and a handler declares whether its framework tells it when a
  run is top-level.

- `bir traces`, `bir stats`, and `bir show` now say when the store holds events
  that belong to no trace. A trace's root event is written when the trace closes,
  so a trace that never closes leaves its children on disk with no root, and
  every trace-shaped read resolves a trace through that root. The events were
  therefore dropped in silence: `bir traces` printed "No traces found" over a
  store that held them, `bir show <id>` reported the trace as not found, and
  nothing connected the two facts.

  `traces` and `stats` now report the count and the first trace id on stderr, so
  `--json` output stays parseable, and `show` distinguishes a trace whose events
  are recorded but rootless from one that is simply absent. What is listed has
  not changed — a trace with no root is still not a trace, and `load_traces()`
  still drops it — but the reader no longer implies the store is empty when it is
  not.

  A root goes missing when the process died before the trace closed, when
  size-rotation dropped the file the root was written to, or when a framework
  integration never received the callback that would have closed the run. All
  three leave the same shape on disk, so the report names the shape rather than
  guessing the cause.

- Redaction no longer costs more than the value it is handed. The PEM
  private-key rule was one regex spanning both markers, and its trailing `.*?`
  rescanned the rest of the value for every `-----BEGIN ... PRIVATE KEY-----`
  that never got a matching footer, so the cost was the product of the value's
  length and the number of unterminated headers.

  Measured on a value made entirely of bare headers, doubling the input
  quadrupled the time: 128,000 characters took 2.0 s, 256,000 took 8.4 s, and
  512,000 took 31.1 s. It is now 19 ms, 38 ms, and 75 ms — 2.0x per doubling, and
  1.04x the cost of redacting the same length of ordinary prose. End to end, a
  traced call taking a 128,000-character argument with capture on went from
  2127.6 ms to 22.3 ms against 0.3 ms untraced.

  This was reachable from outside the process: capture runs inline in the traced
  call, so a value arriving from a caller could stall it for tens of seconds, and
  `max_value_length` gave no protection because truncation runs after redaction
  by design. Ordinary text was never the problem — 131,000 characters of prose
  carrying ten private-key headers cost 20.9 ms then and now — but a value made
  largely of headers was.

  The two markers are now located separately and paired, which reproduces exactly
  what the regex matched (leftmost header, nearest footer at or after it, resume
  past the block) at a cost linear in the value's length. A test pins that
  equivalence against the rule it replaced, and another times the pathological
  shape against prose of the same length rather than against a fixed budget, so
  the bound calibrates itself to whatever machine runs it. No redaction behavior
  changed: real keys, every label variant, and blocks inside larger payloads
  redact as before, and a header with no footer is still left alone.

- `scripts/benchmarks.py` gained `capture_large_value`, whose units are
  characters of one captured value rather than calls. `capture_redaction`
  measures the per-call cost of the rule set on a small fixed payload and cannot
  see a rule that stops being linear in the size of the value; the new case
  reports cost per character, where the regression above showed as 15.12 µs
  against today's 0.16 µs.

- A value that raises while it is being captured no longer breaks the traced
  call. Capture runs code Bir does not own — a mapping's `items()`, a sequence's
  `__iter__`, an exception's `__str__` — and only `__repr__` was guarded against
  it. A mapping whose `items()` raised (a config client, a lazily-loading row
  proxy) turned a working call into a failure two different ways: passed as an
  argument, the decorated body never ran, the caller got Bir's exception, and the
  trace was written with `status="error"` blaming the user's function; returned
  as a result, the function had already completed and produced its value, but the
  caller got the exception instead and no event was written at all.

  Every point where capture invokes the captured value's own code is now guarded,
  and a failure is recorded rather than raised. A value that could not be read at
  all records `[uncapturable]`; a mapping or list whose walk fails part-way keeps
  the entries it read and marks the rest, the same shape `max_collection_items`
  already produced; an exception whose `__str__` fails records
  `<unrepresentable TypeName>` and the caller's own exception propagates
  untouched. A `metadata=` mapping is copied before it is captured, and that read
  is guarded too, on every primitive that takes one.

  `@observe` also stopped refusing calls it could not describe.
  `inspect.signature` follows `__wrapped__`, so `@observe` over a decorator that
  widens its wrapper's signature was handed the narrow one and raised
  `TypeError: too many positional arguments` for a call the function itself
  accepts; the arguments are now recorded as `[uncapturable]` and the call runs.

  Redaction is unchanged and still applies to everything that was read. Passing a
  non-mapping where a mapping belongs is still a `TypeError`, since that is a
  mistake in the call rather than a value misbehaving while it is read.

### Changed

- Operations that read the module-level configuration now bind it once instead
  of reading the global repeatedly. `configure()` rebinds one immutable object,
  so any single read is consistent, but a sequence of reads could straddle two
  configurations: a trace could snapshot input capture from one and output
  capture from the next, a write could pair a new trace path with the previous
  rotation settings, and a trace root could carry a mixed service identity. Each
  of those now takes one binding for the whole operation.

  This is a latent race rather than a reported bug — under the GIL the window is
  a single unlucky scheduling — but it is exactly the kind that stops being rare
  on a free-threaded build. No behavior changes for a program that does not
  reconfigure while recording.

### Added

- CI runs the unit suite on the free-threaded build of Python 3.14, in its own
  job on Linux, so "supports 3.14" stops silently meaning "supports the GIL
  build of 3.14". The stability page now says which builds are tested and to
  what extent. New tests race `configure()` against concurrent recording and
  assert per-thread trace isolation; they identify the interpreter build in
  their failure messages so a CI log is unambiguous about where a failure
  happened.

- The transport and experiment-loading error paths are now covered by behavior
  tests. `bir/_sending.py` rose from 79.7% to 98.7% and
  `bir/_eval_persistence.py` from 79.2% to 99.2%, both from the package's least
  covered code to among its best, and the package total from 92.5% to 93.3%.

  The new tests drive `send_events` and `send_experiment` against a stubbed
  socket and assert what a user experiences when a server misbehaves: the
  message they read, and whether the SDK retried. Both request paths are
  covered, including the per-event fallback an older server without a batch
  endpoint forces, across server errors, rejections, network failures, read
  timeouts, non-2xx bodies, and malformed success responses. A rejection is
  asserted not to be retried, since asking a 4xx again only delays the error.
  Experiment loading is covered the same way — every field rejection is checked
  for naming the path, line, and field — along with the sanitizing that stops an
  experiment name carrying path syntax from writing outside the store.

  No behavior changed; this is test coverage of code that already worked.

- `bir send`, `bir send-experiment`, `bir prune`, and `bir export-otel` accept
  `--json`, so every command that produces a result can be read by a script
  rather than by matching English. The shapes mirror the result objects the
  library returns: `{accepted, attempted, skipped}` for a send,
  `{accepted, experiment_id}` for an experiment, `{removed_traces, kept_traces,
  removed_events, bytes_reclaimed, dry_run}` for a prune, and
  `{traces, spans, endpoint}` for an export. `prune` reporting `dry_run` as a
  boolean is the point of the exercise: a preview and a write used to differ
  only by a parenthetical in the summary line.

  The human summary is still the default, and a usage error stays a message on
  stderr with a non-zero exit code even under `--json`, so a script never parses
  a failure as a successful result. `eval-gate` was already JSON-only and is
  unchanged. `tail` and `experiment-report` have no JSON form on purpose: one
  streams events rather than producing a result, the other renders a document in
  a format you asked for.

- The deprecation policy on the stability page now has machinery behind it.
  `bir/_deprecation.py` provides one message format, a warning helper that
  blames the caller's line rather than a line inside Bir, a decorator for a
  deprecated callable, and a module `__getattr__` builder for a deprecated
  class, constant, or alias. Deprecating a name is two lines at its definition
  and the pattern to copy is in `tests/test_deprecation.py`.

  The promised grace period is now arithmetic rather than a judgment call:
  `_check_removal_release` refuses a removal release sooner than two minor
  releases out, so a deadline that would shorten a user's migration window fails
  the build instead of shipping. The stability page states the concrete deadline
  (a name deprecated against `0.3.0` warns from `0.4.0` and may go in `0.5.0`),
  and a test keeps that sentence true as the version moves. Nothing in the SDK
  is deprecated yet, and a test asserts the public API warns about nothing.

### Changed

- `bir traces`, `bir show`, and `bir stats` now read the local store in one
  streaming pass instead of loading every event into memory. They keep a few
  scalars per trace — name, timing, status, event count, and the tokens and cost
  its generations recorded — where before they retained every event, each
  carrying its captured input, output, metadata, and a copy of its own raw
  payload. `bir show` keeps only the events of the trace it prints.

  Measured on a 3.11 MiB store of 8,000 events across 4,000 traces: `bir stats`
  peaks at 3.97 MiB instead of 38.70 MiB (9.7×), `bir traces` at 4.13 MiB
  instead of 20.13 MiB (4.9×), and `bir show` at 0.23 MiB instead of 20.13 MiB
  (88.9×). What remains scales with the number of traces rather than the number
  of events or the size of what they captured, and `bir show` no longer scales
  with the store at all. Three benchmark cases cover these commands so a future
  change that starts materializing the store again is visible.

  Output is unchanged, including `--json`, filters, `--limit`, and
  `--skip-invalid`. `load_events()` and `load_traces()` are untouched: they are
  public API and still return the lists they always have. `bir export-otel`
  still loads whole traces, because the exporter takes them as a list.

### Added

- `bir traces`, `bir show`, `bir stats`, and `bir export-otel` accept
  `--skip-invalid`, which reads past lines they cannot parse and reports how
  many were skipped instead of refusing the store. An event is appended with one
  buffered write, so a killed process, an OOM kill, or a full disk can leave a
  truncated final line — and until now that one line made every recorded event
  before it unreachable, recoverable only by hand-editing JSONL. The report is
  written to stderr, so `--json` output stays parseable.

  Nothing became lenient by default. `load_events()` and `load_traces()` still
  refuse a store they cannot read completely, because a program building on them
  should not receive a silently partial list. `bir send` and `bir prune` have no
  such flag either: skipping a line there would fail to upload recorded data or
  delete traces the command could not account for.

- Distributed trace-context propagation now has a recorded decision:
  `docs/adr/0001-distributed-trace-context.md` adopts W3C Trace Context over a
  Bir-specific header or no propagation at all, with the remote context recorded
  rather than obeyed — an incoming trace id is adopted so events join one trace,
  the remote span id is kept as trace-root metadata because `parent_id` must
  resolve inside the store, and the remote sampled flag is recorded but never
  applied, since honoring it would let any caller force full recording on a
  service it calls. Extraction is opt-in per call; there is no ambient switch
  that starts trusting headers.

  Only the validated primitive ships: an internal `traceparent` parser and
  formatter with strict, size-capped, hex-only validation, exported from no
  public module and called by no recording path. Building it first was the
  point — the first implementation accepted a trace id with a trailing newline,
  because Python's `$` matches before one, and that id would have reached the
  JSONL store. The public API waits on `bir-app` confirming how it renders a
  trace whose events arrive from two processes. Schema `1.0` is unchanged.

- `scripts/benchmarks.py` measures SDK performance on fixed synthetic data:
  disabled, sampled-out, and recorded tracing; capture redaction; store append
  with rotation; `load_events`/`load_traces`; prune selection; batched sending
  against a stubbed transport; and sync and async experiment runs. Time and peak
  memory are measured in separate passes so `tracemalloc` never distorts a
  timing, and every case rebuilds its store before each repeat so runs do not
  inherit a file a previous repeat grew.

  `--json` records a run, `--baseline` compares against one and exits non-zero
  past a tolerance, refusing to compare runs that measured different amounts of
  work or were recorded under a different result schema. Memory growth must be
  visible in absolute terms as well as percentage terms, so the cheapest cases
  cannot fail on kilobyte-scale noise. CI runs `--smoke` as a canary that the
  harness still works; the release checklist records when to take a real
  baseline, since only one machine's numbers are comparable.

- The SDK now publishes an API stability and compatibility policy
  (`docs/site/stability.md`). It inventories everything the package treats as
  public — the core and evaluation APIs, the test and logging helpers, every
  integration module with its entry points, the 13 CLI commands, the 10 `BIR_*`
  environment variables, and the `1.0` event schema — and states the rules
  around them: `0.x` versioning, one minor release of `DeprecationWarning`
  before any public name is removed, support for every CPython version upstream
  still supports, and no runtime dependencies. It also explains what
  "supported" means for integrations that never import a provider package, and
  ends with a finite Beta entry checklist so readiness is a list to run rather
  than a judgment call.

  The page cannot drift: every inventory on it is compared against the running
  package in both directions, so an undocumented export and a documented name
  that does not exist both fail the build. The flat integration re-exports are
  pinned alongside the existing `bir` and `bir.evals` export pins.

### Fixed

- The LangChain, LlamaIndex, OpenAI Agents, and Pydantic AI handlers now record
  each event's parent from the tree the framework reports (LangChain's
  `parent_run_id`, LlamaIndex's `parent_id` or callback trace, and the Agents and
  OpenTelemetry span parents) instead of from whichever Bir event happened to be
  open. Runs a framework executes in parallel under one parent are recorded as
  siblings rather than nested inside each other; sequentially nested runs are
  unchanged, because there the two agree. A parent the handler never started or
  has already ended still falls back to the surrounding context, so no event
  points at an id that was never written, and an Agents span with no parent span
  resolves to the root opened for its trace. An event opened by a handler is
  still the surrounding parent while it is open, so an application's own
  `@observe()` functions and provider wrappers running inside a framework
  callback keep nesting under it.

  No public API, schema field, or event type changed: the recorded `parent_id`
  still names an event in the same trace. Consumers that rebuild the event tree
  from `parent_id` will see parallel work as siblings, which the previous
  behavior could not express.

### Added

- The Haystack tracer and the AutoGen (AG2) logger handler complete the bridge
  contract: every integration Bir ships is now declared and covered by one of the
  two matrices, and `UNDECLARED_PROVIDER_ROOTS` holds only the OTLP exporter,
  which reads finished traces rather than recording them. Neither handler is
  driven by a callback stream, so the matrix composes the cases their shape can
  actually produce: Haystack's runs open and close through a `with` block, and
  AG2 hands over a finished LLM call in a single `log_chat_completion`, so a run
  cannot be left open, closed twice, closed without being opened, or overlapped.
  AG2's declaration also records that it nests every event under the speaking
  agent's turn span rather than directly under the run root.

- The CrewAI event-bus handler now passes the same bridge contract. Its
  declaration records what CrewAI cannot supply: the bus emits LLM-call and
  tool-usage events with no correlation id and no parent reference, so the
  handler pairs each end with the most recent open call on the thread and nests
  by arrival order. The matrix asserts that behavior for such a framework
  instead of the reported-parent cases, so a declaration states which shape its
  framework can support rather than quietly skipping the question.

- Five framework handlers — the CrewAI event-bus handler, the LangChain callback
  handler, the LlamaIndex handler, the OpenAI Agents tracing processor, and the
  Pydantic AI span handler — now share a second conformance contract covering
  what a bridge owes Bir's event tree: a framework root recorded as a trace, nested runs linked to the
  run that owns them, attachment to an application's own `bir.trace(...)`, an
  implicit root when a run arrives with no trace at all, end callbacks for
  unknown runs ignored, repeated end callbacks recorded once, failures recorded
  with a redacted message, unfinished runs writing nothing, capture defaults and
  handler overrides, and reused handlers keeping sequential runs apart. Handler
  behavior is unchanged. The matrix did surface one shared trait worth naming:
  every handler parents from Bir's active-context stack rather than the parent
  id the framework supplies, so overlapping parallel runs record nested rather
  than as siblings. Both events still land in the right trace and no callback
  raises, which is what the shared case asserts; changing the recorded shape is
  a tracked roadmap decision, not part of this change.

- Provider call wrappers now share one conformance contract. Each `trace_*`
  sync/async family declares its capabilities — request shape, response shape,
  streaming entry point, chunk shape, provider import roots — and a shared
  matrix runs the same lifecycle cases against every declaration: argument
  forwarding with `bir_` options stripped, one generation per call with the
  declared name and metadata, redacted provider errors, the active-trace
  requirement, capture defaults and per-call overrides, lazy streams, chunks
  yielded unchanged, finalization on early close, mid-stream error, and consumer
  cancellation, and the whole-response fallback when a provider ignores a
  streaming request. A registry check refuses to let a new integration module
  land undeclared, and the declared provider roots must match the fresh-import
  guard so a newly added provider is covered by it. This closes gaps the
  per-provider suites had left uneven — sync early close, cancellation, and the
  whole-response fallback were previously asserted for only some wrappers.
  Wrapper behavior is unchanged; the shared guarantees are now documented in the
  integrations guide.

### Changed

- The large SDK, evaluation, and CLI implementations are now split into focused
  private modules for configuration and validation, capture and redaction, local
  storage and locking, transport, experiment persistence and reporting, and CLI
  construction and presentation. This is an internal-only restructuring: public
  Python APIs and import paths, CLI behavior, schema version `1.0` and serialized
  formats, runtime dependencies, safety guarantees, and optional-provider lazy
  loading are unchanged.

- The disk-backed upload spool now tracks and closes partially consumed SQLite
  cursors before closing its connection and deleting the temporary database.
  Bounded sends that fail between batches no longer replace the original network
  error with Windows `WinError 32` cleanup failures on Python 3.11+; successful
  sends and retry/checkpoint behavior are unchanged.

- `bir prune` now streams validated events into a temporary SQLite trace index
  and uses its disk-backed selected-ID membership throughout selection and file
  rewriting. This replaces the production `load_traces()` path and its growing
  Python set while preserving the existing `--before`, `--keep-last`, and
  `--status` rules, exact removed/kept counts, first-seen tie ordering,
  duplicate-root behavior, rootless event handling, and active/rotated scope.
  SQLite sorting uses a bounded page cache, failed reselection rolls back, and
  the temporary database is removed after success or failure.

- `bir prune` now streams surviving JSONL lines directly into sibling staging
  files instead of collecting and joining every retained line in memory. Dry
  runs validate and count the same normalized output without creating staging
  files, partial staging files are removed on failure, and the existing
  lock/atomic-replace behavior and result counts are preserved. Combined with
  the disk-backed trace index, selection and rewriting no longer grow Python
  memory with the store's event or trace count: the working set is bounded by
  the largest individual event or line, a bounded SQLite cache, and small
  per-source staging bookkeeping. Public `load_events()` and `load_traces()`
  behavior remains unchanged.

- `send_events(batch_size=N)` and `bir send --batch-size N` now opt into
  disk-backed bounded upload preparation. Selected active/rotated JSONL files
  are parsed once into a temporary SQLite spool, preserving the globally
  root-first, first-ID-wins order before sending sequential groups of at most
  `N` events. Retries and the batch-endpoint fallback apply independently to
  each group. With `mark_sent=True`, explicit accepted IDs are checkpointed
  after every successful group, so a later failure resumes without re-sending
  completed groups. The temporary database is removed after success and
  failure. Omitting `batch_size` preserves the historical single-request path;
  the returned ID list and opt-in sent-ID set still grow with their ID counts.

- Local trace loading now builds on an internal, lazy JSONL event iterator that
  validates one line at a time and preserves active/rotated file ordering and
  existing file-and-line error context. Public `load_events()` and
  `load_traces()` return types and behavior are unchanged; this establishes the
  bounded-memory primitive for later prune and upload work without changing the
  wire schema or runtime dependencies.

- Ruff is now a dev-only quality gate over Python sources: the repository uses
  correctness/import checks (`E4`, `E7`, `E9`, `F`, `I`) and a 120-column,
  Python 3.10-compatible formatter baseline. Release verification and canonical
  CI both reject lint violations or formatting drift while runtime dependencies
  remain empty.

- The release gate now runs the full SDK suite under statement and branch
  coverage, promotes `ResourceWarning` to an error, and enforces an audited 89.0%
  floor through `pyproject.toml`. Coverage remains a dev-only extra and runtime
  dependencies stay empty. HTTP error responses from event-batch, event, and
  experiment sends are now closed deterministically after their bodies are read,
  removing the resource warnings that the stricter gate exposed.

- Release verification now resolves Pyright through the active Python
  interpreter first and validates `.venv` / `PATH` launchers before using them.
  Moving the repository can no longer leave an executable-looking launcher with
  a stale absolute shebang that prevents an otherwise valid release check. The
  release checklist now includes the supported clean-environment bootstrap.

## 0.3.0 - 2026-07-03

### Added

- Opt-in per-example **`timeout`** (seconds) on `run_experiment()` and
  `run_experiment_async()`, so one stuck example — a hung network LLM call — can
  no longer stall an entire eval run. When set, an example whose task exceeds the
  limit is recorded as an `"error"`-status result with a `"task timed out after
  Ns"` message (the same shape as any other failure, so `raise_on_error` is
  honored and dataset order is preserved) and the run continues with the
  remaining examples. The limit applies to each example's own runtime, measured
  from the moment its task starts: in the threaded runner (`max_workers > 1`) an
  example queued waiting for a free worker accrues no timeout while it waits —
  the worker records the task's start and the collector allows `timeout` seconds
  from that recorded start, and a timed-out example's error row is stamped with
  the task's real start time so its `duration_ms` reflects the timed-out wait.
  The serial `max_workers=1` path uses a dedicated single-worker executor per
  example (its task starts immediately, and a timed-out worker never blocks the
  next one); the async runner wraps each example coroutine in
  `asyncio.wait_for(...)`, cancelling and awaiting the timed-out task so no
  pending-task warning leaks. Python cannot force a thread to stop, so a
  timed-out sync task keeps running in the background until it returns on its
  own; its result is discarded, and it holds its worker slot until then, which
  can delay — but never time out — still-queued examples. `timeout` must be a
  positive, finite
  number and is validated at call time. It is an additive keyword — no new
  exported symbol — and `timeout=None` (the default) is byte-for-byte identical
  to the previous behavior, including the persisted JSONL/summary files
  (`schema_version` stays `1.0`; a timeout reuses the existing error fields).
  Stdlib only (`concurrent.futures`, `threading`, `asyncio`); `dependencies = []`.

- **Richer OpenTelemetry export.** `export_traces_to_otlp(...)` (and `bir export-otel`)
  now carry over the environment, source, and provider the SDK already records.
  The OpenTelemetry `Resource` gains `deployment.environment` (from a trace's
  `configure(environment=...)`) and `bir.source` (from `configure(source=...)`), and
  generation spans gain `gen_ai.system` when an integration recorded the provider
  (LiteLLM's `metadata.provider` or Pydantic AI's `metadata.gen_ai_system`) — never
  guessed from the model string. A new `environment=` argument (and `--environment`
  CLI flag) sets `deployment.environment` explicitly, overriding what the traces
  recorded. When one export mixes environments or sources and none is forced, the
  conflicting attribute moves from the `Resource` onto each span (`bir.environment` /
  `bir.source`) instead of being dropped. With no environment/source/provider
  recorded the export is byte-for-byte unchanged. `opentelemetry` stays in the
  optional `[otel]` extra (`dependencies` remain empty), the local JSONL stays
  read-only, and `schema_version` is unchanged (`1.0`).

- **`SECURITY.md`** at the repo root documenting Bir's privacy and security posture:
  that input/output capture is opt-in and disabled by default, the built-in
  (non-disableable, best-effort) redaction categories actually implemented today and how
  to widen them via `configure(additional_secret_keys=..., additional_redaction_patterns=...)`,
  the local-first `.bir/` storage model, the shared cross-repo redaction contract, and a
  private vulnerability-disclosure process via GitHub Security Advisories. Docs-only — no
  code, redaction rule, schema (`schema_version` stays `1.0`), or fixture change. Linked
  from the README.

- New **`bir config`** CLI command that prints the effective, resolved SDK
  configuration so the most common support question — "is capture on, where is the
  trace path, what is the sampling rate?" — can be answered without a Python REPL.
  It reads the live configuration and reports the absolute `trace_path`, the
  `capture_inputs`/`capture_outputs` flags, the `enabled` master switch,
  `sample_rate` and any exact-name `sample_rules`, the
  `service_name`/`environment`/`source` trace metadata, the
  `max_bytes`/`backup_count` rotation settings, and the
  `max_value_length`/`max_collection_items` capture-size limits, followed by which
  `BIR_*` environment variables are currently set. An in-process `configure(...)`
  call or a set environment variable is reflected. It is strictly **read-only** —
  it never mutates configuration and always exits 0 — and **non-leaky**: the
  additional redaction rules and the local `model_prices` table are summarized as
  counts only (never the patterns or prices), and only the **names** of set `BIR_*`
  variables are listed, never their values. `--json` emits the same fields as a
  deterministic, sorted object. CLI-only and stdlib-only (`dependencies = []`); no
  new public top-level symbol, schema (`schema_version` stays `1.0`), or fixture
  change.

- `bir.evals.similarity_above(threshold, ...)`, a deterministic fuzzy
  string-similarity evaluator that fills the gap between `exact_match()` (exact
  equality) and `contains()` (substring presence). It scores `1.0` when the
  normalized `difflib.SequenceMatcher` ratio between the output text and the
  expected text is at or above `threshold` (the boundary is inclusive) and `0.0`
  otherwise, so real LLM outputs with typos, reordering, or minor wording
  differences can be checked without an embedding model or new dependency. The
  expected value can be configured or supplied per example (the same
  `_USE_EXAMPLE_EXPECTED` path as `contains()`), `case_sensitive=False`
  lowercases both sides before comparing, and the achieved `ratio` and
  `threshold` are recorded in `EvalResult.metadata` so failures are inspectable.
  `threshold` is validated as a finite number in `[0, 1]` and a non-string
  expected raises `TypeError`, consistent with the other evaluators. Exported
  from `bir.evals.__all__`. Stdlib only (`difflib`); `dependencies = []`, no
  schema (`schema_version` stays `1.0`) or fixture change.

- New **`bir prune`** CLI command to reclaim space by removing whole old or
  unwanted traces from the local store, so a long-lived `.bir/traces.jsonl` (and
  its rotated siblings) no longer grows without bound. Selection is by
  `--before ISO` (drop traces starting before the cutoff), `--keep-last N` (keep
  only the N most recent), and an optional `--status {success,error}` restriction;
  `--include-rotated` extends pruning to size-rotated siblings. It operates on
  whole traces (never splitting one across the keep/drop boundary) and rewrites
  each file via a temp file + atomic replace under the same advisory lock an append
  takes, so a concurrent writer can never interleave and a partial failure leaves
  the original file intact. It is **destructive but safe by default**: a bare
  `bir prune` with no selection filter is rejected, and even with a filter it only
  previews unless `--yes` is given (`--dry-run` always forces a preview),
  reporting `removed=<traces> kept=<traces> events=<dropped> bytes=<reclaimed>`. An
  empty store and a no-match run both exit 0 and write nothing. Traces only;
  experiments are untouched.

- Dependency-free **Ollama** integration (`bir.integrations.ollama`) for the
  official `ollama` Python client. `trace_chat` wraps `ollama.chat` (or a
  client's `.chat`) and `trace_generate` wraps `ollama.generate` (or `.generate`),
  each invoking the provider callable inside one Bir `generation`, forwarding
  arguments unchanged, stripping the `bir_`-prefixed options, and returning the
  provider result unchanged. They record the response `model`, the assistant text
  (`message.content` for chat, `response` for generate), and token usage from the
  top-level `prompt_eval_count` (input) and `eval_count` (output), deriving the
  total. With `stream=True` each returns a lazy iterable that yields Ollama's
  chunks unchanged and finalizes the accumulated output and final token usage from
  the terminal `done` chunk when the stream is exhausted, closed, or raises (the
  error re-raised unchanged with its text redacted). Async counterparts
  `trace_chat_async` / `trace_generate_async` await the `ollama.AsyncClient`
  methods and stream the same chunks via `async for`. The `ollama` package is
  never imported, so it adds no runtime dependency (`dependencies = []`); the four
  wrappers are re-exported from `bir.integrations` as `trace_ollama_chat`,
  `trace_ollama_chat_async`, `trace_ollama_generate`, and
  `trace_ollama_generate_async` to avoid colliding with the Mistral and Cohere
  `trace_chat`. No schema (`schema_version` stays `1.0`) or fixture change.
- A master tracing kill switch: `configure(enabled=False)` (and the inverse
  `BIR_DISABLED` environment variable) turns **all** recording off cleanly while
  user code runs unchanged. `@observe`, `trace`/`span`/`generation`/`tool_call`/
  `retrieval`, and `score` still run the wrapped body and still propagate
  exceptions, but nothing is written — an explicit, intent-revealing alternative
  to the implicit `sample_rate=0.0` workaround for feature flags, incident
  toggles, and tests. It is enforced through the same "trace dropped" path as
  sampling: a trace already in flight when recording is disabled stops writing
  immediately, and `configure(enabled=True)` restores full recording for traces
  started afterward. `get_current_trace_id()` / `get_current_span_id()` still
  return the live in-process ids inside a trace while disabled, so log
  correlation keeps working even though nothing is persisted. `enabled` defaults
  to `True`, so untouched behavior is byte-for-byte unchanged; a truthy
  `BIR_DISABLED` (`1`/`true`/`yes`/`on`) sets `enabled=False` at import and an
  explicit `configure(enabled=...)` always wins over it. Dependency-free
  (`dependencies = []`), no schema (`schema_version` stays `1.0`) or fixture
  change.

- `bir stats` now accepts the same `--name`, `--status`, `--since`, and `--until`
  filters as `bir traces`, with identical semantics (case-sensitive name substring,
  exact status, inclusive ISO 8601 start-time bounds with naive values treated as
  UTC, malformed timestamps exiting non-zero). Filters combine with AND and apply
  before aggregation, so every figure — counts, tokens, cost, and latency, in both
  the table and `--json` — reflects only the matching traces. An empty filtered
  result exits 0 with zeroed counts, and `bir stats` with no filters is unchanged.
  CLI-only; no schema (`schema_version` stays `1.0`) or fixture change.

- `BirAutoGenHandler` (`bir.integrations.autogen`) bridges **AutoGen (AG2)**
  multi-agent runs into Bir traces without importing `autogen` / `ag2`. It
  implements AG2's runtime-logging `BaseLogger` interface by method name, so
  `autogen.runtime_logging.start(logger=BirAutoGenHandler())` records each run as a
  Bir trace: `start` opens the trace (a structural span when nested in another
  active trace), each agent's turn becomes a span, `log_chat_completion` becomes a
  generation carrying the model, token usage, and reported cost, `log_function_use`
  becomes a tool call, and a failing completion, function, or `exception` event is
  recorded with error status. Open nodes are tracked on a per-thread last-in-first-out
  stack, so concurrent runs on separate threads and nested runs stay isolated, and
  input/output capture follows the same opt-in settings as every other integration,
  overridable per handler with `capture_inputs`/`capture_outputs`. The symbol is
  re-exported from `bir.integrations`. Dependency-free (`dependencies = []`), no
  schema (`schema_version` stays `1.0`) or fixture change.
- A generated **API Reference** page on the documentation site
  (`docs/site/api-reference.md`, in the nav) that renders the public surface from
  the source docstrings via the mkdocstrings Python handler: the top-level `bir`
  tracing API, loaders, trace-context accessors, and dataclasses, plus the
  `bir.evals`, `bir.testing`, and `bir.logging` modules. Each module block follows
  its `__all__`, so the reference stays in sync with the code and never leaks
  imports. The page builds under the existing `mkdocs build --strict` gate (no
  warnings) and reuses the same `docs` extra. Docs tooling only: `mkdocstrings`
  and `mkdocstrings-python` are added to the optional `docs` extra in
  `pyproject.toml`; the runtime install stays dependency-free (`dependencies = []`).
  No code, public API, schema, or fixture change.
- `bir traces` now filters the listing with `--name` (case-sensitive substring of
  the trace name), `--status {success,error}` (exact status), and `--since`/`--until`
  (inclusive ISO 8601 bounds on the trace start time; values without an offset are
  treated as UTC and a malformed timestamp exits non-zero). Filters combine with
  AND, apply to both the table and `--json`, and run before `--limit` so the limit
  counts only matching traces. With no filters the output is byte-for-byte unchanged.
  CLI-only and stdlib-only: `load_traces` and the trace schema are untouched.

- `bir experiment-report <experiment-id>` and the public
  `bir.evals.render_experiment_report(result, *, format="html")` render one
  persisted experiment to a self-contained report — the run summary, the
  per-evaluator aggregate means, and the per-example table of statuses and
  scores. The default `html` format is a complete standalone document with inline
  styles and no external assets; `--format markdown` emits the same sections as
  Markdown. The CLI writes to stdout or to `--output PATH`, reads the same `--dir`
  directory as `bir experiment-show`, and exits non-zero (printing nothing to
  stdout) for an unknown id. Output is deterministic (evaluators ordered by name,
  examples in dataset order) and every experiment-derived string is escaped for
  the chosen format, so already-redacted example text cannot inject markup. Only
  already-persisted values are rendered; the experiment JSONL/summary schema and
  `schema_version` are untouched. Stdlib only; no new dependency.
- Opt-in per-example detail for experiment comparison:
  `compare_experiments(..., per_example=True)` and `bir eval-gate --per-example`.
  When enabled, the returned `ExperimentDiff` populates an additive
  `example_deltas` field — keyed by shared evaluator then example_id, in sorted
  order — with the candidate-minus-baseline score delta of every example scored
  in both runs, so a failing gate points at the examples that moved instead of
  only the aggregate mean. Examples present in only one run (or not scored by the
  evaluator) are skipped. It is reporting detail only: the aggregate comparison,
  `has_regressions`, and the gate exit code are unchanged, and `example_deltas` is
  empty by default and omitted from `to_dict()` (and the CLI JSON) unless the
  flag is set, so existing output is byte-for-byte unchanged. The persisted
  experiment JSONL/summary schema and `schema_version` are untouched. Stdlib only;
  no new dependency.
- `bir.integrations.trace_converse_stream_async` (AWS Bedrock) and async
  `stream=True` support for the Vertex `trace_generate_content_async` wrapper,
  completing async streaming coverage across every dependency-free provider. Both
  resolve to a lazy async iterator that yields the provider's stream events
  unchanged via `async for` and finalizes the model, accumulated output, and final
  token usage when the stream is exhausted, `aclose()`d, or raises mid-stream (the
  error re-raised unchanged with its text redacted). Bedrock reuses the sync
  Converse-stream accumulation (`contentBlockDelta.delta.text`, the `messageStop`
  stop reason, and the terminal `metadata` usage block) and Vertex reuses the sync
  chunk accumulation (each chunk's `text` with a candidate-parts fallback,
  `model_version`, and the final `usage_metadata`). A provider that returns a
  one-shot response still records via the non-streaming path, and the Bedrock async
  non-streaming and Vertex non-streaming async paths are unchanged. `boto3`,
  `aioboto3`, and `vertexai` stay unimported; `trace_converse_stream_async` is
  re-exported from `bir.integrations`. Stdlib only; no new dependency, schema, or
  fixture change.
- `configure(max_value_length=..., max_collection_items=...)`, opt-in capture-size
  limits that bound a single captured value so one huge payload (a base64 image, a
  megabyte of model output) cannot bloat the local store. `max_value_length`
  truncates an over-long captured string to that many characters and appends a
  visible `…[truncated]` marker; `max_collection_items` keeps only the first that
  many items of a captured list, tuple, set, or mapping and records a single
  `…[truncated]` marker for the remainder. Truncation always runs *after*
  redaction, so a secret is replaced before any cut and redaction can never be
  weakened. Both default to `None` (unlimited), apply to every capture path
  (inputs, outputs, metadata, repr fallbacks, and dataset/experiment capture),
  compose with the existing capture-depth cap, and can also be set from the new
  `BIR_MAX_VALUE_LENGTH` / `BIR_MAX_COLLECTION_ITEMS` environment variables.
  Additive `configure()` keywords and env vars only - no new top-level symbol,
  dependency, schema, or fixture change, and with neither limit set captured output
  is byte-for-byte unchanged.
- `--mark-sent`, `--retries`, `--backoff`, and `--timeout` options on the
  `bir send` CLI command, forwarded to `send_events()`. `--mark-sent` records the
  event IDs the server accepts in a `<trace_path>.sent` sidecar and skips them on
  later sends for cheap idempotent re-sends; `--retries` (default `2`), `--backoff`
  seconds (default `0.5`), and `--timeout` seconds (default `10`) tune
  transient-failure handling, accept non-negative values only, and match the knobs
  already exposed by `bir send-experiment` and `send_events()`. `--timeout` is only
  forwarded when supplied, so the library default still applies otherwise. CLI
  wiring only - no new top-level symbol, dependency, schema, or fixture change, and
  `bir send` with no new flags behaves exactly as before.
- `configure(sample_rules=...)`, an opt-in mapping of exact trace root names to
  sampling rates. A matching rule overrides the global `sample_rate` for that
  root; unmatched roots keep using the global rate. The decision is still made
  once per trace root and inherited by every descendant event. Passing
  `sample_rules` replaces the prior table, `sample_rules={}` clears it, and
  omitting the argument leaves existing rules unchanged. Additive keyword on
  `configure()` only - no new top-level symbol, dependency, schema, or fixture
  change.
- `bir.integrations.BirHaystackTracer`, a dependency-free Haystack 2.x integration
  that maps a pipeline run into a Bir trace without importing `haystack`. It
  implements Haystack's tracing seam (the `Tracer`/`Span` protocol: `trace` /
  `current_span`); register it with `haystack.tracing.enable_tracing(BirHaystackTracer())`
  and each `haystack.pipeline.run` becomes a Bir trace root. Component runs are
  mapped by the component's class name: generators (class name ending in
  `Generator`) become generations carrying the model and token usage when present,
  tool components (`ToolInvoker` and other `*Tool*` components) become tool calls,
  and every other component becomes a span. A component that raises is recorded with
  error status, and active spans are tracked on a context-local stack so concurrent
  and nested pipeline runs stay isolated. Input/output capture follows the same
  opt-in settings as the other integrations, overridable per tracer with
  `capture_inputs`/`capture_outputs`; the model and token usage are always recorded.
  Enable Haystack content tracing (`haystack.tracing.enable_content_tracing()` or
  `HAYSTACK_CONTENT_TRACING_ENABLED=true`) so component inputs/outputs reach the
  tracer. No new dependency or schema change.
- `bir.logging` module with a stdlib `logging.Filter`, `BirTraceIdFilter`, that
  stamps the active Bir ids onto every `LogRecord` as `bir_trace_id` /
  `bir_span_id` (the `TRACE_ID_FIELD` / `SPAN_ID_FIELD` constants), so standard
  formatters can render them with `%(bir_trace_id)s` / `%(bir_span_id)s` and no
  per-call `extra={...}` plumbing. Inside a trace the values equal
  `get_current_trace_id()` / `get_current_span_id()`; outside any trace they are
  `None` and the filter never raises or drops a record. A tiny
  `install_trace_id_filter(target=None)` helper attaches the filter to a logger or
  handler (default: the root logger) and returns it for later removal. The ids are
  read from the same task-local context as the accessors, so each asyncio task and
  thread sees its own. Read-only, consistent with the accessors — no setter and no
  cross-process propagation. New symbols are scoped to `bir.logging` only (the
  top-level package API is unchanged); stdlib `logging` only, no new dependency or
  schema change.
- `bir experiment-show <experiment-id>` CLI command that prints one experiment's
  summary (evaluator aggregate means) and a per-example table of id, status, and
  scores, mirroring `bir show` for traces. `--dir` reads the same experiments
  directory as `bir experiments` (default `.bir/experiments`), and `--json` emits
  a deterministic nested object with the summary fields and a `results` list of
  per-example `example_id`, `status`, `scores`, and `error`. An unknown id prints
  nothing to stdout and exits non-zero. It reuses the public `load_experiment` /
  `list_experiments` loaders — CLI-only, with no new top-level symbol, dependency,
  or schema change.
- `bir.integrations.trace_converse_async` (AWS Bedrock) and
  `bir.integrations.trace_vertex_generate_content_async` (Google Vertex AI), the
  asynchronous counterparts of `trace_converse` and the Vertex
  `trace_generate_content`. Each awaits an async provider coroutine (for example an
  `aioboto3` `bedrock-runtime` `converse`, or a
  `GenerativeModel.generate_content_async`) inside one Bir `generation`, forwards
  arguments unchanged, strips the `bir_`-prefixed options, and returns the awaited
  provider result — recording the same model and
  `inputTokens`/`outputTokens`/`totalTokens` (Bedrock) or `usage_metadata` (Vertex)
  as the sync wrappers, with neither `boto3` nor `vertexai` imported. This completes
  async coverage across every dependency-free provider. Non-streaming only for now:
  the Converse stream (`trace_converse_stream`) and the Vertex `stream=True` surface
  stay synchronous. New public exports — no dependency or schema change, and the sync
  wrappers are byte-for-byte unchanged.
- `set_model(model)` setter on the `generation()` context manager, parallel to
  `set_output`/`set_usage`/`set_cost`/`set_metadata`. The model is read when the
  generation exits, so this records or refines a model only known after the
  provider responds (a streaming refinement, a router-chosen model) without
  passing it to `generation(model=...)` up front. The latest call wins; a
  non-empty string is validated like an event name, and `None` is accepted and
  records no model (clearing any constructor value). Additive method on an
  existing context manager — no new export and no schema change; the dependency-free
  provider integrations now use it in place of writing `gen.model` directly.
- `metadata=` keyword on `@observe()`, recording a static mapping on the trace
  ROOT event the decorated call produces. It is the decorator-side counterpart to
  `trace(metadata=...)` for tagging an entry point (route, tenant, feature flag)
  without rewriting it as a manual `with trace(...)` block. The mapping is redacted
  with the same rules as captured input/output, is attached only when the call
  opens a new trace root (a nested `@observe()` records a span and never carries
  it), and composes with the generator `metadata.generator.*` outcome for observed
  generators. Additive keyword on an existing symbol — no new export, no schema
  change; `@observe()` calls without `metadata` are byte-for-byte unchanged.
- `Typing :: Typed` trove classifier in `pyproject.toml`, so PyPI and tooling
  advertise that the distribution ships inline type annotations. The SDK already
  ships the PEP 561 `py.typed` marker; this is the standard metadata signal that
  pairs with it. Metadata only — no code, API, dependency, schema, or fixture
  change.
- `bir.integrations.crewai.BirCrewAIHandler`, a dependency-free bridge that records
  [CrewAI](https://www.crewai.com/) crew runs as Bir traces. CrewAI's lowest-coupling
  observability seam is its event bus (`crewai.utilities.events.crewai_event_bus`),
  which emits typed start/completed/failed events for crews, tasks, agent executions,
  LLM calls, and tool usage; forward each `(source, event)` the bus emits to
  `handler.on_event` and each crew run becomes a Bir trace. Events are read by duck
  typing — tolerant of field changes across CrewAI versions — and classified by their
  `event.type`: a crew-kickoff event opens a Bir trace root, task and agent-execution
  events become structural spans, LLM-call events become generations carrying the
  model and token usage, and tool-usage events become tool-call events; a
  `*_failed`/`*_error` event closes its node with error status. Crew, task, and agent
  nodes are tracked by their framework id so concurrent and nested runs stay isolated,
  while LLM-call and tool-usage events (which CrewAI emits without a correlation id)
  are paired by a per-thread last-in-first-out stack. Input/output capture follows the
  same opt-in settings as every other integration (overridable per handler with
  `capture_inputs`/`capture_outputs`), and `crewai` is never imported.

- `bir.integrations.dspy`: new dependency-free DSPy integration. `trace_lm` and
  `trace_lm_async` wrap a `dspy.LM` instance's request method
  (`lm.forward`/`lm.aforward`), which returns the LiteLLM-style response, and
  record one generation with model and token usage. The request model is read
  from the bound `LM` instance (`lm.model`) or an explicit `model` keyword and
  refined from the response's `model` when present; `dspy` is never imported.

- `bir.integrations.pydantic_ai.BirPydanticAIHandler`, a dependency-free bridge
  that records [Pydantic AI](https://ai.pydantic.dev/) agent runs as Bir traces.
  Pydantic AI's lowest-coupling observability seam is its OpenTelemetry
  instrumentation (`Agent(instrument=True)`), so the handler implements the OTel
  `SpanProcessor` interface (`on_start`/`on_end`/`shutdown`/`force_flush`) and is
  registered on the tracer provider Pydantic AI uses. Spans are read by duck
  typing — tolerant of attribute-key changes across instrumentation versions — and
  classified by `gen_ai.operation.name` (falling back to the span name): an
  agent-run span opens a Bir trace root, a `chat` span becomes a generation
  carrying the model and token usage, and an `execute_tool` span becomes a
  tool-call event; every other span becomes a Bir span. Failures (OTel `ERROR`
  status or a recorded `exception` event) are recorded with error status, active
  runs are tracked by OpenTelemetry span id so concurrent and nested runs stay
  isolated, and input/output capture follows the same opt-in settings as every
  other integration (overridable per handler with `capture_inputs`/`capture_outputs`).
  Neither `pydantic_ai` nor `opentelemetry` is imported.

- `bir.integrations.instructor`: new dependency-free Instructor integration.
  `trace_create` and `trace_create_async` wrap an Instructor-patched client's
  `chat.completions.create` callable and record one generation with model and
  token usage. Both the direct parsed-model return shape and the
  `(parsed_model, raw_completion)` tuple from `create_with_completion` are
  handled automatically; `instructor` is never imported.

- `run_experiment()` now accepts an opt-in `max_workers` keyword argument (positive
  integer, default `1`). When `max_workers > 1`, examples run concurrently inside a
  `concurrent.futures.ThreadPoolExecutor`, giving a large speedup for I/O-bound
  synchronous tasks such as network LLM calls behind a sync client. Results, JSONL
  rows, and summary aggregates are always written in dataset order regardless of
  completion order. All existing semantics — `raise_on_error`, `record_traces` trace
  isolation, redaction, and the on-disk schema — are unchanged. Requires no new
  dependencies (stdlib only). The default `max_workers=1` is byte-for-byte identical
  to the previous behavior.

- `bir.testing.capture_traces()` context manager (and its `CapturedTraces` handle)
  for asserting on your own instrumentation in tests. It redirects trace writes to
  a private temporary file for the duration of a `with` block and reads the
  captured events/traces back in memory through the public `load_events` /
  `load_traces` loaders, then restores the previous configuration (including a
  user-set `trace_path`) on exit — even if the body raises — and removes the temp
  file. Only *where* events are written changes: capture opt-in, sampling, and
  redaction are untouched, so a captured event matches a real write. Scoped to the
  `bir.testing` submodule to keep the top-level API small; stdlib `tempfile` only,
  with no new dependency, schema, or fixture change.
- GitHub Pages deploy workflow (`.github/workflows/docs-deploy.yml`) that
  rebuilds the MkDocs site behind the same `mkdocs build --strict` gate and
  publishes it to <https://bir-ai.github.io/bir-python/> on every push to `main`
  (and on manual `workflow_dispatch`). The deploy job depends on the strict
  build, so a docs change that fails `--strict` is never published. The existing
  PR-time strict-build gate in `ci.yml` is unchanged, and the default `mkdocs`
  theme and `docs` extra are kept (no code, dependency, schema, or fixture
  change).
- `bir export-otel` CLI subcommand that replays local traces to an OTLP endpoint
  through the existing `bir.integrations.otel.export_traces_to_otlp` exporter. It
  reads the same files as `bir traces` (`--path`, `--include-rotated`), requires
  `--endpoint`, accepts a repeatable `--header KEY=VALUE` for backend auth plus
  `--service-name` and `--timeout` passthrough, and prints how many traces and
  spans were exported. The exporter is imported lazily, so the CLI keeps working
  without the optional `otel` extra; running the command without it exits
  non-zero with the `pip install 'bir-sdk[otel]'` install hint. Stdlib only; the
  opentelemetry packages stay in the `otel` extra, with no schema or fixture
  change.
- Async `stream=True` support for the async Mistral and Cohere `trace_chat_async`
  wrappers and the async LiteLLM `trace_completion_async` wrapper. They now resolve
  to a lazy async iterator that yields the provider's stream events unchanged via
  `async for` and finalizes the model, accumulated output, and final token usage
  when the stream is exhausted, `aclose()`d, or raises mid-stream — completing
  async streaming coverage alongside OpenAI, Anthropic, and Gemini. A provider that
  ignores streaming and returns a one-shot response still records via the
  non-streaming path. Stdlib only; no new dependency, schema, or fixture change.
- `python -m bir` module entry point that dispatches to the same
  `bir.cli:main` as the `bir` console script, for invoking the CLI when the
  console script isn't on `PATH` (fresh venvs, `pipx run`, CI). Both paths share
  one implementation, so behavior and exit codes are identical. Stdlib only; no
  new dependency, schema, or fixture change.

### Changed

- **Python 3.14 is now tested and declared supported.** The CI matrix runs the
  full suite on 3.14 across ubuntu/windows/macos, and the
  `Programming Language :: Python :: 3.14` trove classifier is added to
  `pyproject.toml`. `requires-python` stays `>=3.10` — no code, dependency,
  public API, event schema (`schema_version` stays `1.0`), or fixture change.

- The release gate (`scripts/verify_release.py`) now verifies the **source
  distribution (sdist) in addition to the wheel**. After building and checking
  the wheel, it builds the sdist (`python -m build --sdist --no-isolation`, with
  a deterministic stdlib fallback when the build backend is absent), asserts the
  tarball ships the `src/bir` sources, `bir/py.typed`, `pyproject.toml`,
  `LICENSE`, and `README.md`, asserts it excludes local/generated paths
  (`.bir/`, `build/`, `site/`, caches, virtualenvs), confirms the sdist's
  `PKG-INFO` name/version match `pyproject.toml`, and installs the sdist into a
  fresh virtual environment offline before running the same import/behavior
  smoke test as the wheel. This closes a gap where a broken sdist (missing
  marker/metadata or a leaked local path) could publish to PyPI undetected, since
  many downstream installs and mirrors build from the sdist. The wheel and sdist
  inspectors now share a single forbidden-path list. `build`, `setuptools`, and
  `wheel` are added to the optional `dev` extra as the build tooling the gate
  uses; the runtime install stays dependency-free (`dependencies = []`). The
  check remains offline/hermetic, so CI gains no network dependency. Tooling
  only — no runtime dependency, public API, event schema (`schema_version` stays
  `1.0`), or fixture change.
- CI now runs the SDK unit tests and example smoke tests on Windows and macOS in
  addition to Linux. The `sdk` job is a matrix of `ubuntu-latest`,
  `windows-latest`, and `macos-latest` crossed with Python 3.10–3.13
  (`fail-fast: false`), so the cross-platform file-locking and persistence code
  paths — including the Windows `msvcrt` byte-range lock branch the README
  advertises and the temp-file/rotation handling — are exercised in CI instead of
  only in users' environments. The `PYTHONPATH=src` unit-test and example steps
  pin `shell: bash` so one command runs identically on every runner, and `pyright`
  plus the release-package verification still run exactly once on the canonical
  ubuntu / 3.12 leg. CI configuration only — no runtime dependency, public API,
  event schema, JSON formatting, or fixture change.

### Fixed

- `send_events` and `send_experiment` now validate their `timeout` argument the
  same way they already validate `retries` and `backoff`: a negative, NaN, or
  infinite value raises `ValueError` naming `timeout`, and a boolean or other
  non-numeric value raises `TypeError`, before any file or network access.
  Previously an invalid timeout flowed into `urllib.request.urlopen` and failed
  with an opaque downstream error (the CLI's own `--timeout` validation was
  unaffected). Valid calls are unchanged; no public API, event schema, or
  fixture change.
- `run_experiment_async` with `record_traces=True` and a `timeout` now closes a
  timed-out example's trace instead of leaking it. Previously the
  `asyncio.wait_for` cancellation unwound the traced runner past its
  `except Exception`, so the trace root event was never written: the example's
  already-recorded child events became orphans — invisible to `load_traces`,
  `bir traces`, and `bir show` (which require a root) yet still uploaded by
  `send_events` — and the timeout error result carried `trace_id=None` even
  though a trace had been opened. The traced runner now catches the
  cancellation, writes the trace root with `"error"` status (child events stay
  attached and loadable) and re-raises so `wait_for` still reports the timeout;
  the trace id is surfaced to the timeout error result, whose `trace_id` now
  links the closed trace. `timeout=None` and non-traced runs are byte-for-byte
  unchanged, and `schema_version` stays `1.0`.
- Prompt rendering during capture can no longer raise out of a generation.
  With `capture_rendered=True`, a template that fails to render — literal
  braces in the template (e.g. `'Return JSON like {"a": 1} for {q}'`), a
  missing variable, or any other `str.format` failure — previously raised
  `KeyError` from the generation's `__exit__`: a successful body escaped the
  `with` block with the render error, and a body exception was masked by it.
  Rendering failures are now recorded on the prompt metadata as a
  `rendered_error` marker (redacted like any error text) instead of a
  `rendered` value; the generation event is still written and a body exception
  propagates unmasked. Rendering stays lazy (`prompt()` still never renders
  eagerly), a direct `PromptRecord.render()` call is unchanged, and
  `schema_version` stays `1.0` (`rendered_error` lives inside the free-form
  prompt metadata payload).
- `load_traces` (and the `send_events` ordering that builds on it) now sorts an
  enclosing parent before a nested child even when they share an identical
  `start_time`. Previously the `start_time` tie fell through to an `end_time`
  tiebreaker, which placed a nested child (it ends first) ahead of its parent — a
  latent mis-ordering that only surfaced when the clock resolution was coarser than
  back-to-back event creation (notably on Windows, where a span and the tool call
  inside it could share a timestamp). Ties are now broken by event depth (ancestors
  first), so a parent always precedes its descendants. Internal load-ordering only;
  no schema, fixture, or stored-event change.
- Release verification wheel metadata now preserves `pyproject.toml` optional
  extras (`dev`, `docs`, and `otel`) as extra-scoped `Requires-Dist` entries
  while keeping the base install free of unconditional runtime dependencies.

### Security

- Expanded best-effort capture redaction to recognize credit-card / PAN numbers:
  13-19 digit runs (optionally split into groups by single spaces or hyphens)
  that pass the Luhn checksum are replaced with `[redacted]` across every capture
  path (captured strings, repr fallbacks, error text, prompt/score metadata, and
  integration payloads). This is an additive built-in rule: no existing rule is
  weakened, the candidate run is Luhn-gated so ordinary long integers, ids, and
  phone numbers are left intact, and runs of 20+ digits are excluded entirely.
  **CROSS-REPO CONTRACT: bir-app's independently maintained redactor and its copy
  of `redaction-cases.json` must be updated to match before or with this change.
  Do not release this SDK change while the bir-app redactor or fixture is out of
  sync.**
- Expanded best-effort capture redaction to recognize Stripe secret/restricted
  keys (`sk_live_`/`sk_test_`/`rk_live_`/`rk_test_`), Azure storage-style account
  keys (88-character base64 ending in `==`), and PEM private-key blocks
  (`-----BEGIN ... PRIVATE KEY-----` ... `-----END ... PRIVATE KEY-----`). These
  are additive built-in rules: no existing rule is weakened and the new patterns
  are anchored to avoid over-redacting benign text. **CROSS-REPO CONTRACT:
  bir-app's independently maintained redactor and its copy of
  `redaction-cases.json` must be updated to match before or with this change. Do
  not release this SDK change while the bir-app redactor or fixture is out of
  sync.**

## 0.2.0 - 2026-06-24

### Security

- Expanded best-effort capture redaction to recognize JWTs, AWS access key IDs,
  Google API keys, Slack tokens, and GitHub provider tokens. **CROSS-REPO CONTRACT:
  bir-app's independently maintained redactor and its copy of
  `redaction-cases.json` must be updated to match before or with this change. Do
  not release this SDK change while the bir-app redactor or fixture is out of
  sync.**

### Added

- `configure(source=...)` and the `BIR_SOURCE` environment variable, which tag
  trace roots with `metadata.source`. This is the SDK-side counterpart to the
  `source` field the Bir server and dashboard already filter on (the product's
  Playground records `"playground"`), so SDK-generated traces become filterable
  by origin alongside product-generated ones. The value must be a non-empty
  string, is recorded only on trace roots, and an explicit `source` in a
  `trace(metadata=...)` block still wins. No event-schema or fixture change
  (`metadata.source` is already part of the `1.0` event metadata the server
  reads). Stdlib only.
- `bir.integrations.otel.export_traces_to_otlp`, an opt-in OpenTelemetry/OTLP
  exporter for replaying locally recorded Bir traces to an existing
  observability backend. Install it with the new optional `otel` extra
  (`pip install 'bir-sdk[otel]'`); normal runtime installs stay dependency-free,
  and OpenTelemetry packages are imported lazily only when the exporter is
  called. The exporter accepts a `LoadedTrace`, an iterable of loaded traces, or a
  trace-file path loaded through `load_traces`, then maps each Bir trace to one
  OpenTelemetry trace with parent/child span relationships, original
  timestamps, success/error status, GenAI semantic-convention attributes for
  model and token usage, and `bir.*` attributes for event ids, event type,
  scores, total tokens, cost, and currency. It can build the default OTLP/HTTP
  exporter from `endpoint`, `headers`, and `timeout`, or use an injected
  `span_exporter` for custom transports and tests. The integration is re-exported
  from `bir.integrations`, documented in the README, included in release package
  verification, and covered by tests for dependency isolation, span-tree shape,
  attribute mapping, exporter wiring, and error handling. No schema or fixture
  change.
- `bir.integrations.openai_agents.BirAgentsTracingProcessor`, a dependency-free
  bridge that implements the OpenAI Agents SDK tracing-processor interface
  (`on_trace_start`/`on_trace_end`, `on_span_start`/`on_span_end`, `shutdown`,
  `force_flush`). Register it with `agents.add_trace_processor(...)` and each agent
  run's trace becomes a Bir trace root; spans are mapped by their `span_data.type`
  — model spans (`generation`, `response`) to generations with model and token
  usage when present, tool spans (`function`, `mcp_tools`) to tool calls, and every
  other kind (`agent`, `handoff`, `guardrail`, `custom`, ...) to a span — with
  failed spans recorded as errors. Active traces and spans are tracked by their
  Agents id so concurrent and nested runs stay isolated, and input/output capture
  follows the same opt-in settings as the other integrations, overridable per
  processor with `capture_inputs`/`capture_outputs`. The processor never imports the
  `openai-agents` package, so it adds no runtime dependency, and it introduces no
  schema or fixture change. Re-exported from `bir.integrations`.
- `configure(model_prices=...)`, an opt-in, local-only price table that fills a
  generation's `input_cost`/`output_cost`/`total_cost` from its token usage. Each
  entry maps a model name to a non-negative, finite `input` and/or `output`
  per-token rate plus an optional `currency` (default `USD`); Bir bundles no
  prices, so the rates are the user's responsibility. Cost is derived only for a
  generation that has usage, a matching model, and no explicit `set_cost(...)`
  (which always wins and is never overwritten), routing through the same cost
  validation, currency handling, and total derivation as a manual `set_cost`. The
  table is validated once at `configure()` time — a non-mapping table, a
  non-string or empty model name, a non-mapping or empty rate entry, an unknown
  rate key, a boolean/negative/non-finite rate, an invalid currency, or an
  over-large table raises `ValueError`/`TypeError` immediately. Stdlib-only and
  fully opt-in: with no table configured, generation cost behavior is byte-for-byte
  unchanged. No new public top-level symbol, runtime dependency, schema, or fixture
  change.
- `set_metadata(...)` method on the `trace()`, `span()`, `generation()`,
  `tool_call()`, and `retrieval()` context managers, so metadata discovered while
  the body runs (a resolved route, a cache-hit flag, a request id) can be recorded
  before the event is written. It merges into any metadata supplied at creation
  with a plain update — later keys win, including across repeated calls — and the
  merged metadata is redacted at `__exit__` with the same rules as constructor
  metadata, composing with the generation `prompt` block, the retrieval `kind`,
  and the trace `service` metadata already injected. Spans, which previously
  carried no metadata, now persist it. Works with both `with` and `async with`,
  and the argument must be a mapping (a `TypeError` is raised otherwise). No new
  public top-level symbol, runtime dependency, schema, or fixture change.
- `get_current_trace_id()` and `get_current_span_id()` public accessors that
  return the active trace id and innermost open span/generation/tool-call id (or
  `None` outside any trace), for stamping application logs and metrics so they can
  be correlated with Bir traces. The values are exactly the `trace_id`/`parent_id`
  written to the JSONL for an event created at that point, and are read from the
  task-local context, so concurrent asyncio tasks and threads each see their own
  ids. They are read-only — no setter or context object is exposed, and no
  cross-process propagation is added. No new dependency, schema, or fixture change.
- `bir stats` command that aggregates local traces into a quick usage, cost, and
  health summary: the total trace count with success/error splits, summed
  input/output/total token usage over generation events, summed cost grouped by
  currency (different currencies are reported separately and never summed), and
  trace latency count, mean, and p95. The p95 is the nearest-rank 95th percentile
  computed with the standard library only. `--json` emits the same figures as a
  deterministic object, `--path`/`--include-rotated` resolve the same files as
  `bir traces`, and an empty store exits 0 with zeroed counts. It reuses the
  public `load_traces`/`load_events` loaders and adds no runtime dependency or
  schema change.
- `bir show <trace-id>` command that prints one recorded trace as an indented
  event tree ordered by parent/child, showing each event's type, name, status,
  and duration plus the model and token usage on generations and the value on
  scores. `--json` emits a deterministic nested `{"event", "children"}` tree for
  scripts, and `--path`/`--include-rotated` resolve the same files as
  `bir traces`. An unknown trace id exits non-zero and prints nothing to stdout.
  It reuses the public `load_traces` loader and adds no runtime dependency, schema
  change, or fixture change.
- Synchronous streaming for the Mistral, Cohere, and LiteLLM wrappers. Passing
  `stream=True` to `trace_chat` (`bir.integrations.mistral`),
  `bir.integrations.cohere.trace_chat`, or `trace_completion`
  (`bir.integrations.litellm`) now returns a lazy iterable that yields the
  provider's chunks unchanged in order and records the accumulated output text and
  final token usage once the stream is consumed, instead of recording the
  unconsumed stream object's repr with no usage. Mistral and LiteLLM read
  OpenAI-shaped chunks (`choices[0].delta.content` and `usage` with
  `prompt_tokens`/`completion_tokens`/`total_tokens`); Cohere v2 reads its typed
  events (`content-delta` text at `delta.message.content.text` and the terminal
  `message-end`/`stream-end` usage). The response model refines the request model
  when chunks carry one. A provider that ignores streaming and returns a one-shot
  response still records via the non-streaming path, and a mid-stream error
  produces an error-status generation re-raised unchanged with the persisted error
  text redacted. This matches the existing OpenAI/Anthropic/Gemini sync streaming
  behavior; the async Mistral/Cohere/LiteLLM wrappers do not stream yet. No new
  dependency, schema, or fixture change.
- Synchronous streaming for the AWS Bedrock and Google Vertex AI wrappers,
  completing sync streaming coverage across the provider wrappers. A new
  `trace_converse_stream` (`bir.integrations.bedrock`, re-exported from
  `bir.integrations`) wraps a `bedrock-runtime` `converse_stream` call: it returns
  a lazy iterable that yields the Converse stream's events (the items of the
  response `stream` member) unchanged and records the accumulated
  `contentBlockDelta.delta.text`, the `messageStop` stop reason (as
  `metadata.stop_reason`), and the terminal `metadata` event's
  `inputTokens`/`outputTokens`/`totalTokens`, keeping the request `modelId` as the
  model. Passing `stream=True` to `trace_generate_content`
  (`bir.integrations.vertexai`) yields Vertex's `GenerationResponse` chunks
  unchanged and records the accumulated text (each chunk's `text`, falling back to
  the first candidate's text parts), refining the model from a chunk
  `model_version` and reading the final `usage_metadata`
  (`prompt_token_count`/`candidates_token_count`/`total_token_count`). A call that
  did not actually stream still records via the one-shot path, and a mid-stream
  error produces an error-status generation re-raised unchanged with the persisted
  error text redacted. Neither provider SDK is imported, and the async wrappers
  and non-streaming behavior are unchanged. No new dependency, schema, or fixture
  change.
- Async counterparts for the dependency-free provider wrappers, for applications
  using async provider clients (`AsyncOpenAI`, `AsyncAnthropic`, the `google-genai`
  async client, `litellm.acompletion`, and the async Mistral and Cohere clients).
  Each is named with an `_async` suffix and mirrors its sync wrapper exactly but
  awaits the provider coroutine inside one Bir generation:
  `trace_chat_completion_async` and `trace_response_async`
  (`bir.integrations.openai`), `trace_messages_async`
  (`bir.integrations.anthropic`), `trace_generate_content_async`
  (`bir.integrations.google`), `trace_chat_async` (`bir.integrations.mistral` and
  `bir.integrations.cohere`), and `trace_completion_async`
  (`bir.integrations.litellm`). Arguments are forwarded unchanged, the awaited
  provider result is returned unchanged, and `bir_*` options are never forwarded.
  For the surfaces that stream synchronously today (OpenAI Chat Completions and
  Responses, Anthropic, Gemini), passing `stream=True` resolves to an async
  iterator that yields the provider's async-stream events unchanged via
  `async for` and finalizes the model, output, and usage when the stream is
  exhausted, closed (`aclose()`), or raises mid-stream (re-raised unchanged, the
  persisted error redacted) — never buffering the stream. The wrappers require an
  active trace and work under async `@observe()` functions and
  `async with bir.trace(...)`. Re-exported from `bir.integrations`; sync wrappers
  are unchanged. No new dependency, schema, or fixture change. AWS Bedrock,
  Vertex AI, and the LangChain/LlamaIndex callback handlers are unchanged.
- `configure()` now accepts two additive redaction options,
  `additional_secret_keys` and `additional_redaction_patterns`, so applications
  can teach Bir their own credential field names and secret text formats. They
  only ever widen redaction: the built-in rules and the `[redacted]` marker can
  never be disabled, replaced, or reordered, and there is no switch to turn
  defaults off. `additional_secret_keys` is an iterable of extra mapping-key
  names matched by whole-name, case-insensitive equality (treating `-` and `_`
  as equivalent), distinct from the built-in substring rules.
  `additional_redaction_patterns` is an iterable of regex strings and/or
  compiled `re.Pattern` objects whose every match is replaced with `[redacted]`,
  running after every built-in text pattern. Both are validated and compiled once
  during `configure()` (empty keys/patterns, invalid regexes, non-string entries,
  bytes patterns, and over-large lists raise `ValueError`/`TypeError`
  immediately), and the rules flow through every existing capture and persistence
  path (captured inputs/outputs, repr fallbacks, error text, prompt and score
  metadata, integration inputs/outputs, and dataset/experiment files). Passing
  either argument replaces the previously configured additional rules of that
  kind (an empty iterable clears them); omitting it leaves them unchanged. Stdlib
  only (`re`); no schema, fixture, or dependency change.
- `@observe()` now traces generator and async-generator functions across their
  whole iteration lifetime instead of closing the trace when the generator object
  is created. The wrapper stays lazy (no body runs and nothing is written until
  the first iteration), the root trace spans the first `next`/`asend` through
  exhaustion so child spans and generations created in the body attach to it, and
  it is finalized on exhaustion (success), on an exception from the body (recorded
  as a redacted error and re-raised unchanged), or on an early
  `close`/`aclose`/cancellation (recorded as a success whose
  `metadata.generator.outcome` is `"closed"`). `send`/`throw`/`close`,
  `asend`/`athrow`/`aclose`, and the body's `finally` blocks are all preserved,
  contextvars never leak between iterations or into later work, and concurrent
  async generators in separate tasks stay isolated. Output capture stays opt-in
  and records only a bounded yielded-item count under `metadata.generator.items`
  rather than buffering the stream. Existing sync-function and coroutine behavior
  is unchanged. Stdlib only (`inspect`, `contextvars`).
- `trace_response()` in `bir.integrations.openai`, a dependency-free wrapper for
  OpenAI's Responses API (`client.responses.create`). It forwards arguments
  unchanged, returns the provider response object, and records one generation with
  the model, aggregated `output_text` (falling back to the full response shape),
  and `input_tokens`/`output_tokens`/`total_tokens` usage. With `stream=True` it
  returns a lazy iterable that yields the provider's events unchanged, assembles
  output only from `response.output_text.delta` events, and finalizes the model and
  usage from the terminal `response.completed` event on exhaustion, close, or error.
  Re-exported from `bir.integrations`. Chat Completions support
  (`trace_chat_completion`) is unchanged.
- `run_experiment_async()`, an asynchronous experiment runner for async or sync
  tasks. It accepts coroutine functions, plain sync callables, and sync callables
  that return an awaitable (decided per call with `inspect.isawaitable`), runs up
  to `max_concurrency` examples at once (a positive integer, default `1`), and
  always persists results, JSONL rows, and summary aggregates in dataset order
  regardless of completion order. Evaluator execution, task input binding,
  redaction, `raise_on_error` semantics, `record_traces` trace trees, and the
  persisted JSONL/summary schema match `run_experiment()`. Each example runs in
  its own asyncio task, so concurrent `record_traces=True` runs keep isolated
  trace trees; cancelling the runner cancels and awaits the in-flight example
  tasks and re-raises `CancelledError` without writing a misleading success
  summary. Stdlib only (`asyncio`, `inspect`).
- A structured MkDocs documentation site covering the quickstart, core API,
  privacy and capture, sampling and service metadata, server uploads,
  integrations, evals, CLI, and environment configuration. The documentation
  toolchain is isolated in the optional `docs` dependency extra.
- Bounded retry with exponential backoff for `send_events()`. New `retries`
  (default `2`) and `backoff` (default `0.5`) keyword arguments retry transient
  failures — network errors, timeouts, and HTTP 5xx — sleeping
  `backoff * 2**attempt` seconds between attempts; HTTP 4xx is still raised
  immediately without retry. A healthy send makes a single attempt, so the
  default behavior is unchanged. Stdlib only (`time`).
- Matching bounded retry with exponential backoff for `send_experiment()` and
  `bir send-experiment`. New `retries` (default `2`) and `backoff` (default `0.5`)
  keyword arguments — and non-negative `--retries`/`--backoff` CLI options — retry
  the same transient failures (network errors, timeouts, and HTTP 5xx) sleeping
  `backoff * 2**attempt` seconds between attempts. HTTP 4xx, a missing experiment
  or summary file, and an invalid success response body are still raised
  immediately without retry, and a healthy send still makes one request with no
  sleep, so the default behavior is unchanged. Stdlib only.
- Opt-in `send_events(mark_sent=True)` to make re-sends cheap. Accepted event IDs
  are recorded in a sidecar file next to the trace file (`<trace_path>.sent`) and
  skipped on later sends, so `attempted` reflects only events not yet recorded as
  sent. The sidecar is SDK-local bookkeeping: it never modifies the trace JSONL or
  the event schema, and a missing or corrupt sidecar is treated as empty so it can
  never block a send. Defaults to `False` (nothing recorded), keeping re-sends
  safe via the server's existing event-ID idempotency.
- Opt-in size-based rotation for the local trace file via
  `configure(max_bytes=..., backup_count=...)`. When `max_bytes` is set, the
  active `.bir/traces.jsonl` is rotated on whole-line boundaries before a write
  would exceed the cap (`traces.jsonl` -> `traces.jsonl.1` -> ..), keeping at
  most `backup_count` rotated files (default `3`) and dropping the oldest, so
  every file stays valid JSONL. `load_events()` and `load_traces()` gain an
  additive `include_rotated=True` flag that also reads rotated files oldest-first;
  the default still reads only the active file. `max_bytes` defaults to `None`
  (unlimited), keeping the previous single-file behavior unchanged. Stdlib only.
- Opt-in `send_events(include_rotated=True)` and `bir send --include-rotated` to
  upload size-rotated trace files so rotation can no longer strand unsent events.
  Retained rotated siblings (`traces.jsonl.1` ..) are uploaded oldest-first
  followed by the active file, complete traces stay root-first, and events are
  deduplicated by ID when a rotated file overlaps the active one. `mark_sent`
  keeps anchoring its sidecar to the active trace path, so recorded IDs are
  skipped across the whole selected file set. `bir traces --include-rotated`
  reuses the same flag on the public loader. Defaults to `False` (active file
  only), so existing `send_events()` calls and `bir send` invocations upload only
  the active file as before. Stdlib only.
- Dependency-free AWS Bedrock integration: `trace_converse()` wraps a
  `bedrock-runtime` `converse` call, recording the request `modelId` and the
  Converse `usage` block (`inputTokens`/`outputTokens`/`totalTokens`, deriving the
  total when omitted) without importing `boto3`.
- Dependency-free Google Vertex AI integration: `trace_generate_content()`
  (exported from `bir.integrations` as `trace_vertex_generate_content`) wraps a
  Vertex `GenerativeModel.generate_content` call, recording the model from
  `bir_model` (refined by the response `model_version`) and `usage_metadata` token
  counts without importing `vertexai`.
- Aggregate-score experiment comparison through `compare_experiments()` and
  `ExperimentDiff`, plus a stdlib-only `bir eval-gate` command that exits
  non-zero when a candidate regression exceeds the configured tolerance. The
  comparison takes per-evaluator tolerance overrides via `score_tolerances`
  (repeatable `--score-tolerance NAME=VALUE` on the CLI), which override the
  global `tolerance` only for the named shared evaluators while preserving the
  strict per-evaluator `math.isclose` boundary; non-negative finite values are
  required and an override naming a non-shared evaluator is rejected so typos
  fail loudly. A `missing_score` policy (`--missing-score {ignore,regress}`)
  controls evaluators present only in the baseline: `ignore` (the default)
  reports them without failing, matching the previous behavior, while `regress`
  treats a removed evaluator as a regression because it silently drops coverage.
  `ExperimentDiff.to_dict()` additionally reports `effective_tolerances`,
  `missing_score`, and `regression_reasons` so the gate decision is fully
  machine-readable. Conflicting or malformed CLI assignments are rejected with
  clear errors. Stdlib only.
- A stdlib-only `bir` command-line interface, installed as a console script, for
  inspecting local traces and experiments and sending them to a server without
  writing a script. Subcommands: `bir traces`, `bir tail`, `bir experiments`,
  `bir send`, and `bir send-experiment`, with `--json` output on `traces` and
  `experiments` for scripting. The CLI builds on the existing public API and
  adds no runtime dependencies.
- Environment-variable defaults for SDK configuration so deployments can
  configure Bir without code changes. `BIR_TRACE_PATH`, `BIR_CAPTURE_INPUTS`,
  `BIR_CAPTURE_OUTPUTS`, `BIR_SAMPLE_RATE`, `BIR_SERVICE_NAME`, and
  `BIR_ENVIRONMENT` provide the defaults read once at import time. Explicit
  `configure(...)` arguments still take precedence, capture stays disabled
  unless explicitly enabled, and invalid values raise a clear error.
- Shipped a PEP 561 `py.typed` marker so downstream type checkers (mypy,
  pyright) use the SDK's inline type annotations instead of ignoring them.

### Changed

- Local trace append/rotation and sent-ID sidecar merge/replace operations now
  use stdlib advisory file locks in addition to the existing in-process locks.
  Concurrent local processes writing one trace path no longer race rotation or
  lose sent-ID updates; sidecar replacements use unique, cleaned-up temp files.
  POSIX uses `flock` and Windows uses byte-range locking. No runtime dependency,
  public API, event schema, JSON formatting, or default rotation behavior changed.
- CI now installs the optional `docs` extra and runs `mkdocs build --strict`
  once per pull request and push to `main`, catching documentation navigation,
  link, and warning regressions without adding runtime dependencies.
- `scripts/verify_release.py` now builds and installs its verification wheel
  under the `bir-sdk` distribution name and asserts that
  `importlib.metadata.version("bir-sdk")` matches the project version, so
  distribution-name drift fails release verification instead of being masked by
  a wheel named `bir`.
- `scripts/verify_release.py` now ships the `[project.scripts]` console-script
  entry point in its verification wheel and asserts the installed `bir` command
  is invokable, so console-script regressions fail release verification.
- CI now runs the SDK unit tests and example smoke tests against Python 3.10,
  3.11, 3.12, and 3.13 (previously only 3.12) so version-specific regressions
  across the advertised support range surface before release.

### Fixed

- Release verification wheels now include and inspect the complete `bir` package
  tree, including every `bir.integrations` module, and smoke-test those imports
  in a clean environment without provider SDKs.
- Made the Pyright release gate independent of interpreter discovery by keeping
  the checked offline example tests free of runtime Pytest imports. The same
  three smoke scenarios remain collectable by Pytest with per-test SDK and
  temporary-path isolation; Pytest remains a development-only dependency.
- `bir.__version__` now reads the published `bir-sdk` distribution metadata
  instead of `bir`. Installed packages report their real version instead of
  silently falling back to a hardcoded string; the fallback applies only when
  running from source (`PYTHONPATH=src`) without an install.

## 0.1.1 - 2026-06-18

### Changed

- Relicensed the project from FSL-1.1-ALv2 to the Apache License 2.0.
- Updated project links to the `bir-ai/bir-python` GitHub repository.

## 0.1.0 - 2026-06-17

Initial local MVP SDK release.

### Added

- `@observe()` decorator for sync and async (coroutine) Python functions, producing the same trace and span events on both paths.
- `trace()` context manager for manually scoped root traces.
- Nested `span()` context manager.
- `generation()` context manager with optional model, usage, and user-provided cost fields.
- `tool_call()` context manager for external function or tool usage.
- `retrieval()` context manager for RAG lookups using the existing tool call event contract.
- `async with` support for the `generation()`, `tool_call()`, `retrieval()`, and `trace()` context managers, matching `span()` and recording the same events as their sync `with` form.
- `prompt()` helper for attaching prompt name, version, and optional prompt payload metadata to generation events.
- `BirCallbackHandler` for dependency-free LangChain callback tracing.
- LangChain token usage extraction from common `usage_metadata` and `response_metadata` response shapes.
- `score()` helper for attaching evaluation scores to active traces, with optional redacted `metadata` for evaluator reasoning or thresholds.
- Local JSONL trace storage at `.bir/traces.jsonl` by default.
- `load_events()` and `load_traces()` helpers for reading local JSONL traces.
- `send_events()` helper for posting local events to the Bir FastAPI ingestion server.
- `SendEventsResult.attempted` and `SendEventsResult.skipped` for clearer upload summaries.
- Validation that generation usage and cost setters include at least one field.
- Thread-safe local trace writes within a single SDK process.
- Opt-in input and output capture.
- Best-effort redaction for common secret-like keys and text patterns.
- `bir.evals` deterministic evaluators: `exact_match()`, `contains()`, `regex_match()`, `json_valid()`, `field_equals()`, `field_contains()`, `latency_under()`, `cost_under()`, `numeric_between()`, and `custom_evaluator()`.
- `bir.evals.answer_context_overlap()` deterministic RAG faithfulness heuristic that scores answer/context word overlap.
- `bir.evals.retrieved_context_contains()` deterministic RAG retrieval check that scores whether an expected string appears in the retrieved `contexts` list.
- `bir.evals.answer_contains_citation()` deterministic RAG citation check that scores whether an answer (a plain string or the `answer` field of a dict) contains a bracketed citation marker such as `[1]` or `[doc-1]`, with an optional `pattern` override for custom citation formats.
- Local JSONL dataset loading and experiment result writing through `Dataset` and `run_experiment()`.
- `Dataset.to_jsonl(..., redact=False)` for intentional raw dataset export while keeping redaction enabled by default.

### Notes

- `@observe()` traces coroutine functions, and the `span()`, `generation()`, `tool_call()`, `retrieval()`, and `trace()` context managers all support `async with`, producing the same events on the sync and async paths.
- Server-side ingestion and dashboard viewing are separate local MVP components.
- Cost values are explicit user-provided values; Bir does not calculate provider pricing.
