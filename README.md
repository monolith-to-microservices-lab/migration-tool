# migration-tool

A standalone **coordinator** for the *initial data snapshot* from the legacy
monolith into the already-extracted **User Service** and **Sales Service**.

It is an independent, fourth repository. It does **not** live inside, and does
**not** modify, `legacy-monolith`, `user-service`, or `sales-service`.

```
                    migration-tool  (this repo - a CLI)
                          |
                  READ ONLY (SELECT only, session pinned read-only)
                          v
                 Legacy PostgreSQL   localhost:5432 / monolith
                   users / sales   <-- still the SOURCE OF TRUTH

                          |
               +----------+----------+
               |  HTTP               |  HTTP
               v                     v
          User Service          Sales Service
          localhost:8001        localhost:8080
               |                     |
               v                     v
        User PostgreSQL        Sales PostgreSQL
        localhost:5433         localhost:5434
        (tool NEVER connects   (tool NEVER connects
         here directly)         here directly)
```

**Ownership rule (why the architecture is shaped this way).**
The User Service is the *only* writer of User PostgreSQL; the Sales Service is
the *only* writer of Sales PostgreSQL. The migration-tool is a coordinator: it
reads business data straight from the legacy DB (read-only) and performs every
write to the new world **through the services' HTTP APIs** (`POST
/internal/users/import`, `POST /internal/sales/import`, and for rollback
`DELETE /users/{id}` / `DELETE /sales/{id}`).

---

## What it does

| # | Step                                   | How |
|---|----------------------------------------|-----|
| 1 | Read legacy data                       | `SELECT` from `legacy.users` / `legacy.sales`, in batches |
| 2 | Migrate Users                          | `POST http://localhost:8001/internal/users/import` (id + name + created_at preserved) |
| 3 | Validate Users                         | `GET /users/{id}` per legacy row, field-by-field |
| 4 | Migrate Sales                          | `POST http://localhost:8080/internal/sales/import` (id + user_id + item_name + quantity + created_at preserved) |
| 5 | Validate Sales                         | `GET /sales/{id}` per legacy row, field-by-field |
| 6 | Logical referential integrity Sales→User | every `sale.user_id` must resolve via `GET /users/{user_id}` |
| 7 | Produce a migration report             | text + JSON in `./reports/<run_id>.{txt,json}` |
| 8 | Record exactly what was migrated       | SQLite: `migration_runs` + `migration_items` (with payload snapshot) |
| 9 | Safe rollback of *this run's* data     | delete-by-id via APIs, Sales before Users, drift-checked |
| 10| Never touch the new services' DBs directly | all writes go through the APIs |

---

## Install

```bash
python -m venv .venv
.venv/Scripts/activate          # Windows;  source .venv/bin/activate on POSIX
pip install -e ".[dev]"
cp .env.example .env            # then edit if needed
```

Requires Python 3.12. Dependencies: SQLAlchemy 2, psycopg 3, httpx, Pydantic v2,
pydantic-settings, Typer.

---

## CLI commands

```bash
# Full initial migration: Users -> validate -> Sales -> validate -> integrity
python -m migration_tool snapshot
python -m migration_tool snapshot --dry-run           # read + preflight only, no writes
python -m migration_tool snapshot --batch-size 200

# Only reconciliation / validation against the services (no writes)
python -m migration_tool validate

# Continue an interrupted run from its last known state (idempotent)
python -m migration_tool resume --run-id mig_20260908_143000_a8f2

# Inspect a recorded run
python -m migration_tool status --run-id mig_20260908_143000_a8f2
python -m migration_tool runs

# Roll back ONLY the rows a run created (Sales first, then Users)
python -m migration_tool rollback --run-id <RUN_ID> --dry-run
ALLOW_DESTRUCTIVE_ROLLBACK=true python -m migration_tool rollback --run-id <RUN_ID> --confirm
```

Exit codes: `0` success, `1` finished with failures/divergences, `2` bad
arguments / unknown run, `3` rollback refused by a safety gate.

---

## Migration Run model

Every `snapshot` gets a unique id: `mig_<YYYYMMDD>_<HHMMSS>_<4 hex>`
(e.g. `mig_20260908_143000_a8f2`).

**`migration_runs`** (operational only - never a copy of business data)

| column | meaning |
|--------|---------|
| `run_id` | primary key |
| `mode` | `snapshot` |
| `dry_run` | bool |
| `status` | see state machine below |
| `started_at` / `finished_at` | timestamps |
| `legacy_source` | masked legacy URL used |
| `stats_json` | last computed report envelope (counters, divergences, orphans, reasons) |
| `error` | set if the run aborted on an exception |

**`migration_items`** - one row per legacy record per run

| column | meaning |
|--------|---------|
| `run_id` | FK |
| `entity_type` | `user` / `sale` |
| `legacy_id` | the legacy PK |
| `destination` | `user-service` / `sales-service` |
| `action` | `created` / `unchanged` / `conflict` / `failed` |
| `status` | `imported` / `validated` / `failed` / `rolled_back` / `rollback_conflict` / `rollback_failed` |
| `payload_hash` | sha256 of the canonical payload sent |
| `payload_json` | the exact payload sent (the rollback snapshot) |
| `error` | conflict / transport detail |

`(run_id, entity_type, legacy_id)` is unique - re-running is idempotent.

**`action` semantics that matter for rollback**

* `created` — the service returned **201**. This row was *effectively created by
  this run* → it is **owned by this run** and is eligible for rollback.
* `unchanged` — the service returned **200**. The row already existed identically
  *before* this run → **not owned**, never deleted by rollback.
* `conflict` — **409**. Same id, different data. Deterministic error, not retried,
  fails the run.
* `failed` — transport/5xx error after retries.

### Run state machine

```
STARTED
  -> USERS_MIGRATED
  -> USERS_VALIDATED
  -> SALES_MIGRATED
  -> SALES_VALIDATED
  -> VALIDATED            (referential integrity passed)
  -> COMPLETED

any phase can go -> FAILED
rollback:  -> ROLLED_BACK  |  -> ROLLBACK_FAILED
```

A run is **never `COMPLETED`** if there are conflicts, failed imports,
validation divergences, or orphan sales.

---

## Algorithms

### snapshot

```
run_id = new id  (or the given one, for `resume`)
create/load migration_runs row  (STATUS=STARTED)

PHASE 1  Users
  users_found = COUNT(legacy.users)
  for each legacy user, ordered by id, streamed in batches of BATCH_SIZE:
      if an item for (run, user, id) is already imported/validated -> skip (resume)
      POST /internal/users/import {id, name, created_at}
        201 -> action=created     record payload snapshot
        200 -> action=unchanged   record payload snapshot
        409 -> action=conflict    (deterministic, not retried)
        5xx/timeout -> retried (bounded); still failing -> action=failed
      write migration_items row + one structured log line
  STATUS = USERS_MIGRATED
  if any conflict or failed -> STATUS=FAILED, write report, STOP

PHASE 2  Validate Users
  for each legacy user: GET /users/{id}; compare id, name, created_at (1s tolerance)
  missing row or any mismatch -> divergence
  count(legacy) must equal count(reachable in service)
  if any divergence -> STATUS=FAILED, write report, STOP  ("MIGRATION VALIDATION FAILED")
  STATUS = USERS_VALIDATED

PHASE 3  Sales      (same shape as Phase 1, POST /internal/sales/import)
  STATUS = SALES_MIGRATED ; stop on conflict/failed

PHASE 4  Validate Sales   (GET /sales/{id}; compare id, user_id, item_name, quantity, created_at)
  STATUS = SALES_VALIDATED ; stop on divergence

PHASE 5  Logical referential integrity  Sales -> User
  for each distinct legacy sale.user_id: GET /users/{user_id}
  for each legacy sale whose user_id did not resolve:
      ORPHAN SALE {sale_id, user_id, reason: USER_NOT_FOUND}
  if any orphan -> STATUS=FAILED (cannot be COMPLETED)
  STATUS = VALIDATED

STATUS = COMPLETED ; write reports/<run_id>.{txt,json}
```

**Idempotency / resume.** The import endpoints are idempotent, and every item is
recorded. `resume --run-id X` re-drives run X: already-imported items are skipped,
so a second pass produces `created = 0, unchanged = everything` and never
duplicates. A run that ended `FAILED` is resumable; `COMPLETED` / rolled-back
runs are not.

**Dry-run.** Reads the legacy DB, checks both services' `/health`, counts what
*would* migrate, flags basic problems (unreachable service, legacy sales with a
missing legacy user). Makes **no** write calls and persists **no** run.

### validate

Runs Phases 2, 4 and 5 above on their own, comparing the current legacy contents
to the current service contents. Independent of any run (pass `--run-id` to also
stamp that run's items as `validated`).

### rollback

```
load run;  refuse if already ROLLED_BACK
if not --dry-run:
    require ALLOW_DESTRUCTIVE_ROLLBACK=true  AND  --confirm    (else: refuse, delete nothing)

PHASE A  Sales   (reverse of migration order: Sales before Users)
  for each migration_items row with action=created, entity=sale:
      GET /sales/{id}
        404            -> already gone; mark rolled_back (idempotent)
        present +       -> compare current record to the stored payload snapshot
          equal         -> DELETE /sales/{id}; GET again to confirm 404; mark rolled_back
          different     -> ROLLBACK CONFLICT: do NOT delete; mark rollback_conflict
  if any sale unresolved (conflict/failed) -> STATUS=ROLLBACK_FAILED, STOP
                                              (User rollback is not attempted)

PHASE B  Users   (same logic, DELETE /users/{id})

all created rows removed -> STATUS = ROLLED_BACK
otherwise                -> STATUS = ROLLBACK_FAILED
```

The Legacy DB is **never written to** during rollback - it never lost the
original rows. Rollback only removes from the *new* world what this run created.

`rollback --dry-run` performs the GET + snapshot comparison and prints:

```
Would delete:
  Sales: 1001
  Users: 100
Conflicts: 0
```

without issuing a single DELETE.

---

## Rollback limitations - READ THIS

This tool implements exactly one kind of rollback, and only under narrow
conditions. Four different concepts are often confused:

### 1. Data Migration Rollback  ← *the only thing this project does*

Undo the data this migration run copied into the new services, **while the
legacy monolith is still the source of truth**. It deletes, by explicit id and
through the service APIs, only rows with `action=created`, after checking they
still match what the migration produced.

It is only safe **before** all of:

* write cutover to the new services,
* the new services receiving real traffic,
* users being created *only* in the User Service,
* sales being created *only* in the Sales Service,
* events / async flows mutating the new data,
* the legacy DB ceasing to be the source of truth.

After any of those, deleting rows would destroy real business data. A different
strategy is required then, and it is **not** implemented here.

### 2. Traffic Rollback — *not this project*

Repointing a gateway/router from a new service back to the monolith. An
infrastructure concern, separate lifecycle.

### 3. Application Rollback — *not this project*

Redeploying a previous version/image of a service. A deploy concern.

### 4. Business Data Recovery — *not this project, not now*

Reconciling/repairing data after the new services have already taken exclusive
transactions. Cannot be solved by deleting rows; needs backups, event replay, or
manual reconciliation.

---

## Configuration (`.env`)

| key | default | notes |
|-----|---------|-------|
| `LEGACY_DATABASE_URL` | `postgresql+psycopg://postgres:postgres@localhost:5432/monolith` | opened read-only |
| `USER_SERVICE_URL` | `http://localhost:8001` | |
| `SALES_SERVICE_URL` | `http://localhost:8080` | |
| `BATCH_SIZE` | `100` | legacy read + push batch |
| `HTTP_TIMEOUT_SECONDS` | `5` | per request |
| `HTTP_MAX_RETRIES` | `3` | only transient errors (timeout, connect, 429, 5xx) |
| `HTTP_BACKOFF_BASE_SECONDS` | `0.2` | small exponential, capped at 2s |
| `MIGRATION_STATE_DATABASE_URL` | `sqlite:///migration_state.db` | operational metadata only |
| `ALLOW_DESTRUCTIVE_ROLLBACK` | `false` | must be `true` (plus `--confirm`) to delete |
| `REPORT_DIR` | `./reports` | |
| `LOG_FORMAT` / `LOG_LEVEL` | `json` / `INFO` | structured logs to stderr |

No secrets are hardcoded. `.env` is git-ignored; `.env.example` is committed.

### Retry policy

* **Retried** (transient): connection errors, timeouts, HTTP 429, HTTP ≥ 500.
  Import endpoints are idempotent, so retrying a `POST /import` is safe.
* **Not retried** (deterministic): every other 4xx — notably **409** (conflict)
  and **422** (validation). Retrying cannot change the answer; they fail fast.
* Retries are bounded by `HTTP_MAX_RETRIES`; every attempt is logged.

### Structured logs

One line per item, e.g.:

```json
{"event":"migration.item","run_id":"mig_20260908_143000_a8f2","entity":"user","legacy_id":37,"operation":"import","result":"created"}
```

---

## Project structure

```
migration-tool/
    migration_tool/
        __init__.py
        __main__.py            # python -m migration_tool
        cli.py                 # Typer commands
        config.py              # pydantic-settings, .env
        logging_config.py      # JSON / console structured logging
        legacy.py              # READ-ONLY legacy PostgreSQL access
        http.py                # httpx client: timeout + bounded retry + backoff
        clients/
            user_service.py    # HTTP client for the User Service
            sales_service.py   # HTTP client for the Sales Service
        models.py              # domain models, enums, comparison helpers
        state.py               # SQLite operational store (runs + items)
        runtime.py             # wires config -> legacy + clients + state
        migration.py           # snapshot / resume orchestration
        validation.py          # reconciliation + logical referential integrity
        rollback.py            # controlled, drift-checked rollback
        report.py              # text + JSON report rendering
    tests/                     # pytest; HTTP mocked via httpx.MockTransport
    pyproject.toml
    .env.example
    README.md
```

---

## Timeline

```
Snapshot
T0  Legacy active, source of truth, no traffic to new services
 |
T1  snapshot started        -> STARTED
 |
T2  Users migrated          -> USERS_MIGRATED
 |
T3  Users validated         -> USERS_VALIDATED
 |
T4  Sales migrated          -> SALES_MIGRATED
 |
T5  Sales validated         -> SALES_VALIDATED
 |
T6  referential integrity   -> VALIDATED
 |
T7  done                    -> COMPLETED   (report written)

Rollback
T0  run COMPLETED
 |
T1  rollback --run-id X --confirm requested
 |
T2  safety gates checked (ALLOW_DESTRUCTIVE_ROLLBACK + --confirm)
 |
T3  Sales created by the run deleted (via API), each confirmed gone
 |
T4  Users created by the run deleted (via API), each confirmed gone
 |
T5  validated                -> (unchanged rows still present, Legacy intact)
 |
T6  done                     -> ROLLED_BACK
```

---

## Not implemented (intentionally, this phase)

Kafka / RabbitMQ / Debezium / CDC / Outbox / API Gateway / frontend changes /
dual-write / continuous sync / cutover / Kubernetes / service mesh.

This tool does only: **SNAPSHOT · VALIDATION · RECONCILIATION · CONTROLLED
MIGRATION ROLLBACK**.

---

## Tests

```bash
pytest
```

HTTP is mocked with `httpx.MockTransport`; the legacy DB and the state store run
as in-memory SQLite. Coverage includes: user/sale snapshot, ordering
(Users before Sales), id preservation, `created` ownership vs `unchanged`,
409 handling, transient retry vs. no-retry-on-409/422, user & sale validation,
orphan-sale detection, idempotent re-run, resume after failure, rollback
ordering (Sales before Users), rollback of only `created` rows, drift refusal,
the confirmation gate, and dry-run making no writes.

An optional integration suite (`tests/integration/`, deselected by default)
exercises the real services when they are running locally.
