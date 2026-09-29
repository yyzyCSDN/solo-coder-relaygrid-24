# RelayGrid

RelayGrid is a deterministic Python toolkit for distribution-grid operations.
It covers feeder topology, protection, voltage assessment, DER islanding,
switching safety, outage accounting, crew logistics, asset thermal limits,
telemetry quality, storage dispatch, and restoration sequencing.

The package is self-contained and uses only the Python standard library.

## Install and run

```bash
python -m pip install -e ".[test]"
relaygrid demo
python -m pytest -q
```

The demo builds a four-bus feeder with two open ties, reports its energized
state, safely closes the sectionalizing branch, and emits a JSON summary.

## Public API

The top-level package exports the feeder data model and `GridSimulator`:

```python
from relaygrid import Branch, Bus, FeederSnapshot, GridSimulator, SwitchState
```

Domain modules remain independently importable so applications can combine
topology, protection, telemetry, restoration, reliability, planning, and field
operations without depending on a web framework or database.

## Incident review workspace

`relaygrid.review` freezes an incident timeline together with the operational
evidence of six kinds — switching orders (操作票), telecontrol commands
(遥控), crew records (班组), weather (天气), AMI readings, and outage
evidence (停电). Each raw record is sealed at capture time with a canonical
SHA-256 digest; after freezing the record book and timeline reject all writes.

Review conclusions, responsible-party assignments, and corrective actions
advance through append-only revisions: prior versions are never overwritten,
and the workspace never writes back to operational records. Missing evidence
is listed separately (declared requirements plus automatic checks such as
telecontrol without an order, outage without AMI corroboration, or dangling
timeline references). Exports are self-contained bundles whose snapshots can
be re-verified offline and optionally compared against current source records.

```python
from relaygrid.review import (
    RecordBook, TimelineEvent, build_timeline, open_review,
    ExpectedEvidence, write_bundle, verify_file, detect_missing,
    record_from_switching_order, record_from_command,
)

book = RecordBook()
book.append(record_from_switching_order("rec-order", order, frozen_at))
book.append(record_from_command("rec-cmd", command, frozen_at, order_id="SO-100"))
book.freeze()  # operational evidence is now sealed

timeline = build_timeline(window_start, window_end, [
    TimelineEvent(65.0, "feeder trip", refs=("rec-order", "rec-cmd")),
])
review = open_review("INC-001", "Feeder trip", frozen_at, timeline, book)
review.record_conclusion("c1", "root_cause", "...", ts, "lead", "initial")
review.update_action("a1", "Trim corridor trees", "open", ts, "lead", "opened")

gaps = detect_missing(timeline, book, [ExpectedEvidence("weather", "storm data")])
write_bundle("incident.json", review)
assert verify_file("incident.json").ok
```

## License

RelayGrid is released under the MIT License.
