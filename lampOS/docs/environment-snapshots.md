# Environmental snapshot contract

`lamp-environment` is an I/O-free Rust state component, not a sensor driver or
integrated runtime. It has three fixed source slots and nine fixed fields.
There is no bus probing, scheduler, recovery loop, lock, queue, heap allocation
in ingest/snapshot, GPIO access, movement, speech or environmental safety policy.
Configuration is explicit; an empty configuration is disabled. Supported sensor
profiles do not establish what is installed on a Lamp.

## Reuse decision and scope

The source contract is V1 commit `d5efe9d7b73cc529b34cd4abe97624682a82ca94`:
`hal/drivers/environment/{registry,service,group,scd41,sen55,sen63c}.py`,
`hal/test/test_environment_readiness.py`, and the
[hardware contract](hardware-contract.md#environmental-sensors-and-shared-i2c).
Reuse V1's explicit source selection, conflict rejection, latest samples,
per-field timestamps, sample-driven continuity, status faults and partial warmup.
The readiness invariant is usable fresh fields, not an initialized thread or a
completed poll. Retrying or receiving a not-ready response adds no valid sample.

The shared `lamp-interaction::ObservationTracker`, `BootId` and `MonoTime` provide
ordering, worker identity and monotonic time semantics without a new common
framework. This component has no new external dependency. Its local dependency
on `lamp-interaction` transitively retains that crate's existing serde dependency.

| Explicit profile | Exported fields | Inherited freshness window, caller supplies |
|---|---|---:|
| SCD41 | CO2, ppm | 15,000,000 microseconds |
| SEN55 | Temperature, Celsius; RH, percent; PM1/2.5/4/10, micrograms/m3; VOC/NOx indices | 5,000,000 microseconds |
| SEN63C | Temperature, Celsius; RH, percent; PM1/2.5/4/10, micrograms/m3; CO2, ppm | 5,000,000 microseconds |

SCD41 + SEN55 cover all nine fields. SEN63C conflicts with either profile and
cannot be silently preferred or merged. The three-slot bound does not imply
three simultaneously nonconflicting enabled profiles. VOC/NOx are indices, not
gas concentrations; ambient Celsius is distinct from SoC thermal monitoring.
No inherited polling cadence or numeric alert threshold is an implementation
default or a validated safety limit.

## API and readiness

Construct `Environment::new(&[SourceConfig])` with an explicit profile, worker
boot and nonzero freshness window for each source. `ingest(received_at, Report)`
accepts one complete sample, not a patch. `Readings` contains typed finite
engineering units or `None`. A new `None` replaces a previous value for that
field; warmup CO2 never borrows freshness from ready temperature or humidity.
SEN63C can therefore supply six ready fields while CO2 is unavailable.

`DeviceStatus::Reported(0)` is required for SEN55/SEN63C. Missing status or any
nonzero status clears source values and exposes a fault. SCD41 allows
`NotReported`, matching its V1 export; this does not assert that all internal
hardware status is known. An explicit acquisition fault also clears values.
The ordering high-water mark survives these faults. A later valid report may
restore usable data, but this component does not schedule or initiate recovery.

`Event::NoNewData` records the check's ordering and receipt only. It does not
renew sample timestamps, extend continuity, or clear a fault. V1 SCD41 raw CO2
zero produces no new data; a future driver must translate that to `NoNewData`.
Sentinel fields in an otherwise valid SEN sample become `None`.

`snapshot(now)` returns fixed arrays with per-field availability, current value,
selected source, original acquisition/receipt times, boot, sequence and age.
Values exist only for `Ready` fields. Aggregate `Partial` means at least one
configured field is ready but not all are ready. This is a deliberate refinement
of V1's component-level partial flag, which could mark an internally partial
sensor ready without setting the group's partial flag. Unconfigured fields do
not count against readiness. A window is inclusive: age equal to the window is
current; greater age is stale.

Each unit constructor rejects NaN/infinity but does not clamp finite values or
invent plausible ranges. Future drivers still own raw length/CRC checks,
sentinels, signedness, scaling, protocol-defined ranges and calibration. A
representation-valid value is not proof of physical plausibility or accuracy.
In particular, SEN63C CO2 signed-value interpretation above 32767 remains
unqualified. No raw register decoder is part of this slice.

## Ordering, continuity and restarts

Every report has a nonzero, increasing per-source sequence and an original
monotonic event timestamp. For a sample that timestamp is acquisition time;
for a fault/not-ready check it is the event time. Receipt/check timestamps are
provided independently by the trusted caller, in the same OS monotonic domain.
Future acquisitions, old boots, duplicates and regressing sequence/acquisition
times are rejected without replacing newer state. Receipt cannot make old
sample bytes fresh. Sequence gaps are allowed by this latest-only contract;
it does not prove every physical sample was acquired or delivered.

The inherited observation confidence remains `Confidence::UNKNOWN`; readiness
uses explicitly present data, status and age. It does not manufacture a score
or claim sensor accuracy. `observed_continuity_us` is the last valid acquisition
minus the first in an observed, unexpired per-field run. Waiting, repeated
snapshots and not-ready checks cannot increase it. Absence, expiry, fault,
stop and restart break the run; its value is not proof of continuous sensing.

`restart_worker(now, old_boot, new_boot)` invalidates **all** source slots using
that worker boot, including retained values and continuity. Old reports cannot
repopulate them. A fresh boot must be unique and never reused; this fixed store
rejects current collisions but cannot retain an unbounded history of retired
IDs. A supervisor must perform this invalidation when it learns of a restart
or failure; the cache cannot detect a dead worker until freshness expires.
An explicit `Stopped` report clears the source and rejects further reports
until this supervisor restart operation. Clock regression latches the entire
store unusable; reconstruct it from explicit configuration after resolving the
clock fault, with fresh worker identity as appropriate.

## Processing target and verification boundary

Path: **completed decoded sensor sample → validated latest snapshot**. Initial
component target: **p99 ≤100 microseconds**, for fixed three slots/nine fields.
No I/O, lock or wait occurs in the measured functions. This target excludes
sensor integration time, acquisition cadence, bus transport, worker/IPC queues,
and voice or acoustic latency. Target-device qualification is pending.

`cargo run --release --offline --locked -p lamp-environment --example snapshot_latency`
runs 4,096 synthetic completed-sample ingests followed by full snapshots, with
two configured sources covering nine fields. It reports first-call, p50, p99,
maximum host timings and state sizes. Timing includes clock-read overhead and
is host evidence only; it does not measure driver performance or establish a
before/after speedup. Inputs and timing storage are fixed-size and prebuilt.

Focused tests cover conflict rejection, complete-sample replacement, warmup,
per-field provenance, acquisition-based expiry, not-ready checks, fault
ordering, restarts, late reports, timestamp/sequence bounds and fixed state size.
No sensor was opened or observed. Raw drivers, shared I2C ownership and clock
configuration, startup/stop commands, real installed-device identification,
calibration and fault/latency measurements remain future work.
