# Smart Queue storage and recovery

The durable queue database, candidate-memory database, active intake, and the
database sibling lease all remain beneath `jobapply_agent/private/`. They are
candidate data or runtime state and must not be committed or included in
diagnostic reports.

Each persistent monitor owns the exact sibling lease for its queue database.
The standalone daemon obtains it before a browser preflight or queue
construction and keeps it for the full monitor run. Concurrent monitor,
admission, or candidate-outcome operations therefore fail closed instead of
mutating the same queue at the same time.

If a process terminates unexpectedly, the operating system releases its open
lease descriptor. On the next run, the first complete URL snapshot remains the
recovery boundary: visible stale reservations become open, absent ones become
`open_failed`, and neither condition records an application outcome. A later
candidate-confirmed outcome still requires the explicit outcome and `--vacated`.

Do not delete, replace, symlink, or hand-edit the lease file. It is a stable
private rendezvous file; safe lease acquisition validates it and rejects unsafe
objects. If a new monitor cannot obtain the lease, leave the active runtime in
place or wait for its verified termination rather than forcing takeover.

The explicit broker-recovery command is an operator-only repair path. Before
using it, confirm the old browser connection is quiesced and the retained owner
process has been terminated; process exit alone is not proof that an in-flight
browser operation was cancelled. Recovery accepts only regular database and
marker paths beneath this repository's `jobapply_agent/private/` directory,
rejects symlinks and traversal, acquires ownership before clearing a marker, and
fails closed for all other paths. Never use recovery to bypass a live owner or
to operate on an arbitrary SQLite file.
