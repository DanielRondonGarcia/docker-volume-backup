# Jobs Filter Controls

## Goal

Make the Jobs history filters match the data types and values shown in the table so operators can filter dates, commands, origins, and statuses reliably.

## Problem

The Jobs table currently renders every filter as a free-text input. Date columns need date/time controls, while command, origin, and status are finite values that should be selected from the current job set instead of typed manually.

## Scope

- Use `datetime-local` controls for Inicio and Fin.
- Use dynamically populated select lists for Comando, Origen, and Status.
- Preserve ID and Target free-text filters, sorting, pagination, polling, and current selections.
- Apply intuitive date bounds: Inicio is a lower bound and Fin is an upper bound.
- Keep labels, accessible names, and empty/default options clear in the existing Spanish UI.
- Add source-level UI regression assertions for control types, option generation, and filter semantics.

## Constraints

- Preserve existing job data, API contracts, compatibility identifiers, and polling behavior.
- Do not change backend filtering or pagination; this table currently filters the loaded job projection client-side.
- Preserve unrelated `.atl/`, `.codegraph/`, `.playwright-mcp/`, and `__pycache__/` workspace changes.
- Do not commit, push, deploy, or publish until the parent receives explicit delivery authorization.

## Tasks

### JFC-1 — Replace free-text filters with typed controls

- Status: complete.
- Route: delegated direct implementation with focused UI regression coverage.
- Scope: Jobs table filter state, rendering, option synchronization, and date-bound comparisons.
- Acceptance: Comando, Origen, and Status are selects with unique current values; Inicio and Fin are datetime pickers; selecting a value filters rows and resets to page one.
- Evidence: command/origin/status selects use deduplicated escaped options; datetime-local controls apply inclusive Inicio/Fin bounds and exclude missing/invalid timestamps. Valid selections survive polling and stale selections are cleared.

### JFC-2 — Verify and close

- Status: complete.
- Route: fresh verification worker plus parent structural readback.
- Scope: focused UI tests, full unittest discovery, and `git diff --check`.
- Acceptance: controls remain accessible, polling does not lose selections, and all applicable checks pass or are honestly recorded.
- Evidence: focused UI suite passed 77 tests; full discovery passed 522 tests with 2 skips; `git diff --check` passed with line-ending warnings only. Browser/CI/runtime verification was not run.

## Authorized edit surfaces

- `odd/tasks/jobs-filter-controls.md`
- `src/control_plane/ui/index.html`
- `tests/test_ui_states.py`

## Checks

- `PYTHONDONTWRITEBYTECODE=1 python -m unittest tests.test_ui_states`.
- `PYTHONDONTWRITEBYTECODE=1 python -m unittest discover -s tests -p 'test_*.py'`.
- `git diff --check`.

## Progress

- The previous Jobs implementation used free-text inputs for all columns.
- Jobs filters now use typed controls while preserving client-side filtering, sorting, pagination, row actions, and polling.

## Next step

Commit only the authorized Jobs UI/test/task paths as one work unit; preserve unrelated workspace artifacts and decide separately whether to publish.
