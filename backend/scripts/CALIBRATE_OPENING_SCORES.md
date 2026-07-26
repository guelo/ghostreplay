# Calibrate Opening Scores (v2)

`calibrate_opening_scores_v2.py` produces reproducible calibration evidence for
the opening-score **v2** model. v2 is the only live scoring model — there is no
v1 baseline — so this script *calibrates v2 directly* rather than comparing two
models. It scores every candidate `(user_id, player_color)` pair **in memory**
and reports the distributions, source mix, phase-horizon behaviour, recursion
accounting, readiness-fold grid, and PASS/FAIL diagnostics needed to decide
score defaults and grade thresholds.

## No-write default

The default run performs **zero database writes**. It reads evidence via
`overlay_evidence` and scores in memory via `compute_all_root_scores`; it never
calls `recompute_opening_scores` (which would reserve a generation and persist a
batch). Run it freely against the production database for read-only calibration:

```bash
cd backend
python -m scripts.calibrate_opening_scores_v2
```

Add `--json` for a machine-readable report, `--min-observations N` to change the
cohort threshold, and `--users` / `--pairs` / `--limit` to target a subset.

## Cohort definition

A pair is **included** in the distribution statistics only when it has at least
`--min-observations` quality observations (default `20`). Pairs below the
threshold are listed under `cohort.excluded_low_evidence_pairs` but contribute no
percentiles, so a handful of one-game accounts can't skew the picture.

Named-root distributions are reported **three ways**, because pooling named-root
rows mixes correlated samples (named roots share ancestor/descendant FENs):

- **Pooled** (`named_score_distribution.pooled`) — all named-root scores
  concatenated. Fast to read, but the rows are *not* independent; treat
  percentiles as indicative.
- **Per-user median summary** (`named_score_distribution.per_user_median_summary`)
  — each pair's own median, then summarized across pairs, so a broad-tree user
  with hundreds of rows doesn't dominate.
- **Per-pair** (`named_score_distribution.per_pair`) — each included pair's full
  distribution (`summarize(named_scores)`, i.e. p5..p95 shape + histogram), so a
  single pair's shape is not lost behind its median.

Only the **included** cohort feeds these distributions. All other telemetry
(source mix, excluded sessions, horizon, recursion, throughput) aggregates over
**all candidate pairs**, so the well-formed early-return telemetry of
low/zero-evidence pairs (e.g. structural raw-middlegame and recursion key counts)
still surfaces even when the included cohort is empty.

The synthetic `__repertoire__` hero row is reported in its **own** section
(`synthetic_hero_distribution`), never mixed into the named-root distribution.

## Metrics emitted

| Section | What it reports |
|---------|-----------------|
| `cohort` | candidate pairs, included count, low-evidence pairs |
| `named_score_distribution` | `pooled` + `per_user_median_summary` + `per_pair` (each: percentiles & 5-bucket histogram) |
| `synthetic_hero_distribution` | the `__repertoire__` row, kept separate |
| `source_mix` | `session_eval` / `analysis_cache` / `eval_delta` as % (zero-denominator guarded) |
| `excluded_sessions_total` | sessions dropped for broken board continuity |
| `horizon` | opening-interval-length distribution; **raw-middlegame root count** and **unscored root count** as two distinct numbers |
| `recursion` | actual-key count and perfect-key count reported **separately** (`_metrics` is keyed `(fen, perfect)`), vs the named-root count |
| `throughput` | total scoring wall-time, **per-pair scoring latency** (median / p95 / max), and emitted row count |
| `gates` | pass/fail vs the documented numeric bars: scoring `< 5s/pair` and cache read `< 50ms` (`n/a` when not measured) |
| `grid` | per-cell distributions over the anchor-first arm grid (each row identified by all six axes), plus per-key deltas vs the current-model reference |
| `diagnostics` | User-14 user-turn true-positive, opponent regression guard + unprepared-branch leak, and thin-but-earned cliff gates |

The recursion section is the bound proof: actual/perfect key counts scale with
the number of unique reachable normalized FENs, not the named-root count, and the
two passes are counted apart rather than conflated into one ≈2× number.

The horizon section keeps **raw-middlegame roots** (roots whose own board
satisfies the middlegame predicate) distinct from **unscored roots** (roots with
no reachable quality observation). A raw-middlegame root can *still* be scored
through observed off-book children, so the two must never be conflated.

## Write-bench mode (cache latency)

Cache-write/read latency benchmarking is **gated** behind `--write-bench`, which
requires `--allow-writes` **and** a `--database-url` that passes the safety rule:

- the URL must be **SQLite under `backend/.tmp/`**, *or* contain an explicit
  `calibrate` database name (e.g. `..._calibrate`), **and**
- it must not be the configured production URL.

Any other URL is refused (`validate_write_bench_database_url`). Under
`--write-bench` the script runs **one** `recompute_opening_scores` on the
isolated database, then times a `list_cached_opening_scores` read:

```bash
python -m scripts.calibrate_opening_scores_v2 \
  --write-bench --allow-writes \
  --database-url sqlite:///.tmp/opening_calibrate.db
```

Populate the isolated database with representative evidence first (e.g. copy a
subset of `session_moves` / `analysis_cache`), or the bench measures an empty
cache.

## CLI reference

The script takes three **subcommands**, and each exposes *only* its own options — an
option belonging to another mode is rejected as unrecognized (exit 2) rather than
accepted and ignored, in either token order:

```
argv := [ "report" ] REPORT_OPTS*
      | "capture-cohort" CAPTURE_OPTS*
      | "select-release" SELECT_OPTS*
```

### `report` (the default)

The legacy bare form still works unchanged: `--json`, `--limit 5`, or no arguments at
all are all `report`. The test is on `argv[0]` only, so an option *value* that happens
to spell a subcommand name (`--users capture-cohort`) is not a mode switch. One stated
exception: a leading `-h`/`--help` is *not* rewritten, so bare `--help` shows the root
help where the three subcommands are discoverable rather than `report --help`.

| Flag | Default | Description |
|------|---------|-------------|
| `--database-url` | configured app URL | SQLAlchemy DB URL (read-only by default) |
| `--min-observations` | 20 | Quality observations required to include a pair |
| `--users` | all | Comma-separated `user_id`s to restrict to |
| `--pairs` | all | Comma-separated `user_id:color` pairs to restrict to |
| `--limit` | none | Limit candidate pairs |
| `--report-fold-grid` | `0.25,0.5,0.75,1.0` | Comma-separated report-fold `p` values to sweep the arms over; domain `0 < p <= 1` |
| `--include-demo-diagnostics` | off | Add the diagnostics-only demo rows (gate + uniform fold) to a standalone run; never enters cohort scoring |
| `--json` | off | Emit the report as JSON |
| `--write-bench` | off | Time one isolated recompute + cache read (needs `--allow-writes` + guarded URL) |
| `--allow-writes` | off | Required acknowledgement alongside `--write-bench` |

### `capture-cohort`

Freezes, fences, and publishes a frozen-cohort artifact.

| Flag | Default | Description |
|------|---------|-------------|
| `--output` | **required** | Final artifact path. Must be ABSOLUTE and resolve OUTSIDE every git working tree of this repo |
| `--require-quiescent-epoch` | off | Promote global-epoch movement to a retry trigger (explicit maintenance window) |
| `--max-attempts` | 3 | Fence attempts before giving up (`>= 1`) |

There is **no `--min-observations`** here: the capture threshold is pinned release
policy (`DEFAULT_MIN_OBSERVATIONS`, stamped by the freeze), and `capture_cohort(...)`
exposes no threshold parameter. There is also **no CLI option that accepts a
release-guard user**, and there will not be one: the guard user comes only from the
`GHOSTREPLAY_RELEASE_GUARD_USER` environment variable, because a command-line argument
would put a production user id into shell history and into every process listing on the
capture host for the duration of the run. A missing or non-integer value refuses before
any DB work, and the value is never echoed, logged, or included in any diagnostic.

Run it through `capture_cohort.sh` **from the ORIGIN checkout** — never bare, and never
under the release launcher:

```bash
GHOSTREPLAY_RELEASE_GUARD_USER=<id> \
  backend/scripts/capture_cohort.sh --output /abs/private/store/cohort.json
```

A bare invocation refuses before any DB access (the launcher marker, the inherited
pre-exec digest, `-S`, and bytecode-writing-off are all absent). A capture hosted by the
*release* launcher refuses too: that launcher deletes its throwaway worktree on exit, so
the reviewable `cohort_provenance.json` diff would be written into a directory that no
longer exists.

Exit codes: `0` success, `1` any `CaptureError`, `2` an argparse usage error or a
missing/non-integer `GHOSTREPLAY_RELEASE_GUARD_USER`.

### `select-release`

Scores a frozen-cohort artifact, decides ship / no-ship, writes the **full**
`SelectionResult` to a private path, and prints **only** a redacted approval summary.

| Flag | Default | Description |
|------|---------|-------------|
| `--artifact` | **required** | ABSOLUTE path to the frozen-cohort artifact, OUTSIDE every worktree |
| `--result-output` | **required** | ABSOLUTE path the full result JSON is written to, OUTSIDE every worktree. Never overwritten — an existing file is a refusal, not a republish |

Both must be absolute because the launcher execs the child with `cwd=<tree>/backend`, so
a relative path would resolve against a directory the operator never chose.

```bash
backend/scripts/release_calibration.sh --mount-cohort-provenance -- \
  select-release --artifact /abs/private/store/cohort.json \
                 --result-output /abs/private/store/result.json
```

**What the mount is for.** Approval happens *before* the provenance record is committed,
so at selection time the candidate record exists only as an uncommitted working-tree diff
in the origin checkout — while the run itself executes from a worktree checked out from a
*revision*, where `COHORT_PROVENANCE_PATH` resolves to the old committed record (or to
nothing at all). `--mount-cohort-provenance` copies the origin working-tree bytes into the
checkout and hands the child their SHA-256 in `GHOSTREPLAY_COHORT_PROVENANCE_SHA256`,
computed before the child interpreter exists. `select-release` refuses without it. There
is deliberately no `--provenance-record <path>` option: a caller-supplied path would let a
caller pass an unapproved artifact plus a freshly generated record that matches it.

**Run it from the main checkout.** The origin working tree means git's *main* working tree,
and the launcher checks rather than assumes it: `--git-dir == --git-common-dir`. The record
being mounted is uncommitted, so it exists in exactly one checkout — the one the capture ran
in. Launched from a linked worktree, the mount would silently pick up whatever *that*
worktree has committed at `cohort_provenance.json`, and a stale record paired with its own
matching artifact passes every downstream trust gate: a clean-looking approval describing a
cohort nobody captured today. That is refused, not silently redirected — if the capture
record is not in the checkout you are standing in, the run is not the one you think it is.

**"Outside every worktree" is decided by filesystem identity**, not by comparing resolved
path strings. `Path.resolve()` follows symlinks but preserves the caller's *spelling*, and
this repo lives on a case-insensitive filesystem, so `/users/…/ghostreplay/result.json` is
the same file as `/Users/…/ghostreplay/result.json` while comparing unequal to every
forbidden root. `(st_dev, st_ino)` is the filesystem's own answer, and it closes hard links
and bind mounts in the same move. The leaf need not exist, so the walk covers the path and
every one of its parents. The worktree listing is read with `--porcelain -z`: git prints the
path raw, and a directory name may legally contain a newline that line-based parsing would
cut in half — recording a *prefix* of a real worktree as the forbidden root.

**Rejected arguments are never echoed.** Stock argparse prints the offending tokens
verbatim, which would put a production user id on stderr for `--release-guard-user 987654`
and a private store path there for a wrong-mode `--output /abs/private/...`. No message
argparse rendered is ever forwarded — sanitizing rendered text is unbounded work, and each
attempt at it leaked (a dash-prefix test read `-987654` as an option name; a single-quote
filter missed `"987654'x"`, because `repr` switches quote styles around an apostrophe; the
`--report-fold-grid` domain error was never quoted at all). Every usage error is instead
*assembled* from a closed vocabulary — the option names, subcommand names, and dests
registered by this CLI itself, plus the retired options it names deliberately — together
with fixed prose and counts of what was withheld. A token is echoed only if it is a member
of that vocabulary; anything else, flag-shaped or not, is counted. A message that cannot
be assembled that way collapses to a single fixed string, so an unanticipated phrasing
degrades to silence rather than to a leak.

The program name in the usage block and the `prog: error:` prefix is a pinned literal, not
`sys.argv[0]`. argv[0] is process-controlled — `exec -a`, or a copy of this script parked
in the private store — and argparse would otherwise print it on every usage error and on
`--help`, through a channel the message assembly cannot see.

**stdout / stderr split.** stdout carries the redacted summary JSON *and nothing else*, so
a failed run has empty stdout and a successful one can be piped straight into `jq`. Every
diagnostic goes to stderr, and every diagnostic is a fixed, data-free message plus the
exception class name — no exception text and no path is ever forwarded, because binding
errors embed real score operands and cohort pair identifiers. There is no debug-verbosity
switch: it would be a leak with a flag on it. The inputs are in the private store and the
failing condition is reproducible there.

The summary carries names, booleans, digests, and the winner's six cutoffs. It does not
carry the result filename (a content hash names the reviewed bytes instead), the B1 grade,
any distribution, any gate operand, or the free-form no-ship reason — all of which live in
the private file the approver reads.

| Exit | Meaning |
|------|---------|
| `0` | a valid result WITH a winner; full result published, summary printed |
| `1` | a valid result with `winner is None` — **no-ship, and nothing else** |
| `2` | CLI contract: argparse usage error, a relative or repo-interior path, a missing/non-regular artifact, a missing result-output parent |
| `3` | release-trust refusal: the provenance mount is absent, disagrees, or MOVED between the gate's read and the load guard's; `scorer_source_verified_preexec` is False; the running code is not the code the digest names |
| `4` | input rejection: the load guard refused the artifact/record pair, a fail-closed binding check refused the inputs, or the artifact could not be read |
| `5` | output failure: the result failed its own serialization or redaction schema, a result already exists at the requested path, or the write failed |
| `6` | unexpected internal error (the catch-all) |

## Release runs: use the launcher

A **release** calibration — one whose winner Phase 3 will apply — must be started
through `release_calibration_launcher.py`, never by invoking the scorer directly:

```bash
backend/scripts/release_calibration.sh --mount-cohort-provenance -- \
  select-release --artifact /abs/private/store/cohort.json \
                 --result-output /abs/private/store/result.json
```

Use the wrapper. It is not sugar: it starts the launcher itself under **`-I -S`**, and the
launcher **refuses to run** otherwise. It defaults to `backend/.venv/bin/python` and fails if
that is missing rather than falling back to `PATH` — an unactivated shell resolves `python3`
to the system interpreter (3.10 here, against the venv's 3.12), and since a non-venv
interpreter is legitimate in a CI image, nothing downstream would have objected to a release
scored against whatever versions the system happened to carry. Set `GHOSTREPLAY_PYTHON` to
override explicitly. The child inherits that interpreter, and its dependency paths are
derived from that interpreter's venv.

The launcher checks out `--rev` (default `HEAD`) into a private temp dir, stages the whole
execution input beside it, builds a disk image, and **attaches it read-only** — then unlinks
the backing file, because an attached `.dmg` is still writable by this uid and would be a
second, writable path to every sealed byte. Only then does it hash anything.

What is on the volume: the checkout, the interpreter, the standard library and `lib-dynload`,
the installed dependencies, the frozen cohort artifact, and every non-Apple dylib in the
closure. The last of those is not padding — `otool -L` shows the interpreter references
`libpython` and `libintl` by *absolute path* into `~/.pyenv` and `/opt/homebrew`, so copying
the prefix alone would still execute mutable host code until `DYLD_LIBRARY_PATH` redirects it.
The dependencies are on the volume for the reason that made this mandatory: a concurrent
`pip install` changes what runs without touching the tree, the manifest, or the digest.

The digests are then computed by a **second launcher process** running *from the volume, on
the volume's interpreter* (`--inner`). The outer process is host code on a host interpreter,
so a digest it computed would be a statement made by mutable code about bytes that were still
writable when it read them. Computed inside, the window between the hash and the child's
import is not narrowed — it is gone.

Everything the run may write is enumerated, and none of it is on the volume: the bytecode
cache (empty, write-disabled), the child's `TMPDIR`, and the operator's `--result-output`.
`backend/.opening_graph_cache` is **not** in that set: the cache is a pickle validated by a
version and two mtimes, i.e. a mutable scoring input and an unpickle of a file any same-uid
process can write, so a sealed run rebuilds the graph instead. That costs nothing new — a
release run always checked out a fresh worktree, where the gitignored cache was never present.

`--no-boundary` runs the old path (exclusive worktree, `0444` on the hashed files, no volume)
for dev and report runs, and on platforms with no mechanism. It stamps
`scorer_source_verified_preexec=False`, so no release will accept its output. The launcher
**refuses to start** a boundaryless run unless that flag is passed explicitly; only macOS has
a mechanism today.

Both interpreters run without site initialisation, and both halves are required:

* **The child runs under `-S`**, with its dependency directories passed explicitly on
  `PYTHONPATH`. Otherwise `site.py` executes every `.pth` import line in site-packages and
  imports `sitecustomize` before the scorer's first byte — and such a hook can import a
  manifest module from the correct tree and then rebind a function on it. The source bytes
  are never touched, so the digest still matches and the import-origin check still passes,
  while the code that actually runs is not the code the digest names.
* **The launcher runs under `-I -S`**, or it refuses. The child's `-S` is one interpreter
  too late on its own: whatever starts the launcher runs first, and a `.pth` there executes
  before the launcher imports `hashlib` — early enough to replace `sha256` in the very
  process that computes the digest, making it whatever the hook wants. The launcher cannot
  fix this itself (by the time its code runs, the hook has already run), so it fails closed
  and the entrypoint carries the flags.

`PYTHONNOUSERSITE` does not cover either case: it disables the *user* site directory, while
the live vector is the interpreter's own site-packages. If a future dependency needs a `.pth`
to be importable — an editable or namespace install — the run fails loudly at import rather
than silently degrading, which is the intended behaviour for a release path.

The child's environment is also scrubbed of **every** inherited `PYTHON*` variable, with only
the four the launcher chooses added back. This is an allowlist because the denylist was wrong:
`PYTHONWARNINGS` names its filter category as `module.Class` and the interpreter *imports that
module* to install the filter — before the script body, under `-S`. Measured against the real
venv, `PYTHONWARNINGS=default::sqlalchemy.exc.SAWarning` had SQLAlchemy imported before the
child's first line, through a variable that reads like a logging preference. Non-`PYTHON*`
variables (`DATABASE_URL` and friends) are inherited: they are the run's configuration.

All three halves matter and none can be replaced by in-process code:

* **The hash precedes the interpreter.** CPython compiles the scorer and its
  imports before any of its statements run, so an edit landing in that window
  leaves old code executing while every in-process read agrees on the new bytes.
  Only a hash taken before the process existed catches it.
* **The checkout is isolated from the shared tree**, which is what makes the digest a claim
  about a *tree* rather than a *moment*. The working tree — written continuously by editors,
  builds, and other agents — cannot give you this. *How* it is isolated is the difference
  between the two paths, and it is the whole of `g-release-os-boundary`: a release run puts
  it on a read-only volume, and `--no-boundary` falls back to `0444` on the hashed files.
* **Nothing auto-executes before the scorer.** The two points above are about bytes on
  disk; `-S` is about code that never touches the tree at all.

**Be precise about what the `--no-boundary` path is worth**, because it is weaker on every
axis and it is the reason the boundary was made mandatory. `0700` excludes other *users*, not
other processes running as you; `0444` stops an accidental write, not a deliberate one, since
this uid can chmod it back; and `git worktree list` publishes the path for the duration of the
run. The interpreter, the standard library, and every installed dependency are **unhashed** —
the digest binds `SCORER_SOURCE_FILES` and nothing else, so a concurrent `pip install` changes
the code that runs without moving the digest at all. `check_scorer_import_origins()` reads
`__file__`, which reports where a module was *loaded from*, not what its attributes hold now,
so it catches a misconfigured path or the wrong checkout — not a hostile loader. This is why
`--no-boundary` stamps `scorer_source_verified_preexec=False` and no release will take it. The
next section describes what a release run gets instead.

### What the boundary does and does not cover

A same-uid write to any sealed byte returns `EROFS` — and still returns `EROFS` after a
`chmod u+w` that appears to succeed, which is the clearest available statement that mode bits
were never the mechanism. The scorer does not take the launcher's word for any of this: it
**measures its own filesystem** (`check_execution_boundary()`), checking that every manifest
file, every imported module, the interpreter, the stdlib, and every native image `dyld`
actually loaded is on **the sealed volume** — not merely on some read-only mount, which any
other attached image would satisfy while sitting outside `runtime_image_sha256` entirely. The
volume is identified from the scorer's own `__file__`, so it cannot be aimed elsewhere.
Forging the attestation environment variables buys nothing. The measurement is taken again
after the last score, so a volume detached mid-run fails the run rather than passing quietly.

Two more refusals guard the run, one on the way in and one at the freeze.

`--artifact` is checked for the repo-interior governance rule **against the operator's own
path**, because sealing rewrites the argument and the scorer downstream would only ever see
the staged copy. The file is *opened before it is judged* and copied from that descriptor
afterwards: a path checked at one moment and reopened at another is not reliably the same
file, and the gap is wide — a worktree is added and a provenance record is mounted inside it.

And once the image is built, attached, and its backing file unlinked, **everything on the
volume is compared against the thing that says what it should be**. `cp -Rc` clones each file
atomically but still *walks*, so an install landing mid-walk yields a hybrid runtime that is
internally consistent per file and never existed as a whole; hashing it afterwards would name
the hybrid precisely, which is the trap. The comparison is of content, not metadata — a
same-size edit reverted with the mtime restored walks straight through anything weaker — and it
is of the frozen bytes, not of the writable stage, so there is no window left after it. Each
name is also checked to be a *file on the volume*: a symlink staged in place of one would be
followed off the volume to whatever it points at, and compared equal to it.

**What that comparison is worth depends on what it is compared against, and the two are not the
same strength.** The checkout is compared against the **commit** — content-addressed, already
written, out of reach of an editor saving into a working tree. The artifact is compared against
the **descriptor** held open since before it was judged. The mounted provenance record is
compared against a **digest of the bytes the launcher itself wrote**. Those three cover the
code, the data, and the record naming the cohort, and for them the comparison settles it.

The interpreter, the dependency roots and the dylib closure are compared against **live host
trees**, which *can* detect the concurrent `pip install` or `brew upgrade` this exists for —
whether it does depends on where the install lands relative to a walk that cannot be made
atomic. It does not prove the volume is any single instant of those trees: the comparison walks,
so a source that moves and moves back between two of its reads is not excluded. A whole-tree
snapshot needs privileges the launcher deliberately does not require. That limit is stated here
rather than worked around, and a test pins it so it cannot quietly become a claim again.

**And matching bytes are not a closed boundary.** A symlink's content *is* its target string, so
a link that matches its baseline exactly — committed, or copied out of the interpreter prefix —
passes every comparison above and still lands wherever it says, leaving the volume with a name
whose bytes are off it and stay writable for the whole run. So every link on the volume is
required to *land* on the volume, on its device; relative links that stay inside are the common
case and are untouched. One consequence is visible in the repository: a stray `.antigravitycli/`
config symlinked into a home directory was tracked, and a sealed run refuses while it is, so it
was removed and ignored.

The sealed `.git` is bound too, and it is the one entry here whose reach is *governance* rather
than bytes. It is a gitfile — it decides which repository git answers as from inside the
checkout — and the private-path rule that keeps production-derived output out of every checkout
was built from `git worktree list`. Repointed at another valid repository, the checkout still
verified clean while the origin checkout vanished from that listing, which would have made the
real working tree an acceptable destination. It is now required to name the run's own
administrative entry, compared by `(st_dev, st_ino)`.

**Binding the gitfile is not the same as trusting git, and it was not enough.** The
administrative directory it names stays writable and outside the boundary, so editing
`<admin>/commondir` repoints git with the sealed `.git` file and the admin inode both
unchanged — and the origin checkout leaves the forbidden set exactly as before. So the set is
no longer derived where it is used: the launcher measures every working tree on the **host**,
before the volume exists, and hands the child that set as a **floor**
(`GHOSTREPLAY_SEALED_FORBIDDEN_ROOTS`). The child still asks git and takes the union, so a
worktree registered after the seal is caught by the live answer while no mid-run edit can
shrink the set below what was measured. A sealed run that carries no floor refuses.

**And the destination is held, not re-resolved.** `--result-output` is judged before any
scoring work and was then reached by name — create the temp, link it, unlink it — minutes
later. Replacing the validated parent directory with a symlink into a checkout in between
published the private result inside the checkout. The parent is now opened when it is judged
(`O_DIRECTORY|O_NOFOLLOW`), judged again by descriptor, and every step of the publication
happens relative to it. What that does not cover, said plainly: if the judged directory is
itself *moved* into a checkout afterwards, the descriptor follows it — that is relocation of
the destination, not substitution of it, and no descriptor can prevent it.

Outside the boundary, named rather than waved away:

* **The signed system volume** (`/usr/lib`, `/System`) — sealed by the OS, not writable even
  by root, taken host-provided by decision. The OS build is recorded as `os_build`.
* **git's administrative directory**, which stays in the origin's mutable `.git`. Git keeps
  working from inside the volume, but nothing it says there is an attestation:
  `source_revision` and `source_dirty_paths` are audit fields. The sealed revision is the one
  the launcher resolved before the checkout existed (`sealed_revision`). The volume's `.git` is
  bound to this directory, so the *volume* cannot redirect git — and because the directory it
  names is still writable, the private-path rule stopped depending on git's answer alone (the
  host-measured floor above). What git says from inside can add to what is refused; it can no
  longer subtract.
* **`hdiutil detach`**, still available to this uid. It breaks the run loudly.
* **A hostile operator**, who can commit the change and have it sealed like anything else.
  This defends against accident and concurrency on a machine that also runs editors, agents,
  and package installs — never against the person running the release.

A run inside the boundary stamps `scorer_source_verified_preexec=True` on its cohort and
winner binding, and binds `runtime_image_sha256`: a digest over the actual bytes of the volume,
which is what `runtime_python` and `runtime_chess_version` could never be — two builds of
`3.12.7` agree on every character of both. Everything else stamps `False`: a bare
`python backend/scripts/calibrate_opening_scores_v2.py` run, and — since
`g-release-os-boundary` — a `--no-boundary` launcher run too, even though it still computes a
matching pre-exec digest. That is normal and correct for dev and test, and the script stays
fully usable that way. The refusal lives at the release boundary:
`require_preexec_verified_source()` rejects anything carrying `False`. Because the worktree
comes from a commit, uncommitted edits can never reach a release run.

## g-xnv7 calibration decision

The 2026-07-09 g-xnv7 final run chose `lcb_z=1.0`,
`coverage_fold="gate"`, and `coverage_live_threshold=1`. The chosen grid cell
reported pooled named-root stats mean `14.6`, p5 `0.4`, p25 `5.1`, p50 `9.8`,
p75 `20.8`, p95 `43.9` across 478 rows; all three diagnostics passed. Display
grades were recalibrated once from that combined distribution:
`A>=44`, `B>=29`, `C>=8`, `D>=2`, `F<2`; tones `alert<5`, `watch<29`.
