import unittest
import re
from pathlib import Path


UI_PATH = Path(__file__).resolve().parents[1] / "src" / "control_plane" / "ui" / "index.html"
UI_CSS_PATH = Path(__file__).resolve().parents[1] / "src" / "control_plane" / "ui" / "styles" / "components.css"


class SnapshotExplorerUiStateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = UI_PATH.read_text(encoding="utf-8")

    def test_snapshot_explorer_contains_v2_routes_and_legacy_fallbacks(self):
        for route in ("/api/v2/targets/", "/snapshots`,", "/api/v2/jobs/", "/cancel`,"):
            self.assertIn(route, self.source)
        for operation in ('"browse"', '"search"', '"dump"'):
            self.assertIn(operation, self.source)
        self.assertIn("/api/v1/targets/${encodeURIComponent(target.id)}/snapshots", self.source)
        self.assertIn("/snapshot-ls", self.source)
        self.assertIn("/snapshot-dump", self.source)
        self.assertIn("/api/v1/jobs/${encodeURIComponent(request.jobId)}", self.source)

    def test_snapshot_catalog_sync_runs_after_persisted_catalog_load_and_surfaces_failures(self):
        modal = re.search(r"async function openSnapshotsModal\(target\) \{.*?\n    \}", self.source, re.S)
        self.assertIsNotNone(modal)
        modal_source = modal.group(0)
        self.assertIn("void loadSnapshotsList(target).then", modal_source)
        self.assertIn("void refreshSnapshotCatalog();", modal_source)
        refresh = re.search(r"async function refreshSnapshotCatalog\(\) \{.*?\n    \}\n\n    function showSnapshotOperationError", self.source, re.S)
        self.assertIsNotNone(refresh)
        refresh_source = refresh.group(0)
        for marker in ("snapshot-sync-failed", "snapshot-sync-incomplete", "state.catalogError", "snapshot-poll-timeout"):
            self.assertIn(marker, refresh_source)

    def test_target_worker_controls_only_allow_eligible_workers_and_patch_reassignment(self):
        targets = re.search(r"function renderTargets\(content\) \{.*?\n    \}", self.source, re.S)
        self.assertIsNotNone(targets)
        targets_source = targets.group(0)
        self.assertIn('filter(w => w.status === "online")', targets_source)
        self.assertIn("No hay workers elegibles online", targets_source)
        edit = re.search(r"function openEditTargetModal\(target\) \{.*?\n    \}", self.source, re.S)
        self.assertIsNotNone(edit)
        edit_source = edit.group(0)
        self.assertIn('id="etWorkerId"', edit_source)
        self.assertIn('worker_id: document.getElementById("etWorkerId").value', edit_source)
        self.assertIn("no elegible", edit_source)
        self.assertIn("Error guardando target", edit_source)

    def test_snapshot_explorer_is_race_safe_and_cancellable(self):
        for marker in (
            "new AbortController()",
            "requestSequence",
            "request.controller.abort()",
            "isCurrentSnapshotRequest",
            "signal: request.controller.signal",
            "SNAPSHOT_POLL_ATTEMPTS",
            "SNAPSHOT_POLL_DELAYS",
        ):
            self.assertIn(marker, self.source)
        self.assertIn("setTimeout(() => {", self.source)
        self.assertIn("}, 250);", self.source)

    def test_snapshot_explorer_has_bounded_wall_clock_poll_budget(self):
        match = re.search(r"const SNAPSHOT_POLL_MAX_MS\s*=\s*(\d+)", self.source)
        self.assertIsNotNone(match)
        self.assertGreaterEqual(int(match.group(1)), 60_000)

    def test_backup_completion_refreshes_catalog_and_modal_close_cleans_up(self):
        self.assertIn("function scheduleSnapshotCatalogRefreshAfterBackup(job)", self.source)
        self.assertIn('job.command !== "backup.run"', self.source)
        self.assertIn("SNAPSHOT_CATALOG_REFRESH_DELAYS", self.source)
        self.assertIn("loadSnapshotsList(current.target, { background: true })", self.source)
        self.assertIn("isCurrentSnapshotCatalogRequest(request)", self.source)
        close_modal = re.search(r"function closeSnapshotsModal\(\) \{.*?\n    \}", self.source, re.S)
        self.assertIsNotNone(close_modal)
        self.assertIn("stopSnapshotCatalogRefresh();", close_modal.group(0))
        self.assertIn("abortSnapshotCatalogRequest();", close_modal.group(0))
        self.assertIn("abortSnapshotCatalogSync();", close_modal.group(0))

    def test_snapshot_explorer_manual_refresh_syncs_once_before_catalog_reload(self):
        refresh = re.search(r"async function refreshSnapshotCatalog\(\) \{.*?\n    \}\n\n    function showSnapshotOperationError", self.source, re.S)
        self.assertIsNotNone(refresh)
        refresh_source = refresh.group(0)
        self.assertIn("/api/v1/targets/${encodeURIComponent(state.target.id)}/snapshots-sync", refresh_source)
        self.assertEqual(refresh_source.count("snapshots-sync"), 1)
        self.assertIn("beginSnapshotCatalogSyncRequest()", refresh_source)
        self.assertIn("if (!syncRequest) return;", refresh_source)
        self.assertIn("pollSnapshotJob(syncRequest,", refresh_source)
        self.assertIn("SNAPSHOT_POLL_MAX_MS", self.source)
        self.assertIn("await loadSnapshotsList(state.target);", refresh_source)
        self.assertIn("isSnapshotAbortError(error)", refresh_source)
        self.assertIn("void refreshSnapshotCatalog();", self.source)
        self.assertIn("useLegacyJobPolling", self.source)

    def test_snapshot_refresh_button_loader_is_scoped_and_cleans_up(self):
        controls = re.search(r"function setSnapshotCatalogRefreshBusy\(.*?\n    \}\n\n    function beginSnapshotCatalogRequest", self.source, re.S)
        self.assertIsNotNone(controls)
        controls_source = controls.group(0)
        for marker in (
            'data-snapshot-refresh-spinner',
            '.snapshot-refresh-button[aria-busy="true"] .snapshot-refresh-spinner',
            'aria-busy="false"',
            'visibility: hidden',
            'visibility: visible',
            'button.setAttribute("aria-busy", isBusy ? "true" : "false")',
            'button.disabled = isBusy',
            'SNAPSHOT_REFRESH_LABEL = "Refrescar lista de snapshots"',
            'SNAPSHOT_REFRESH_BUSY_LABEL = "Sincronizando snapshots..."',
        ):
            self.assertIn(marker, self.source)

        refresh = re.search(r"async function refreshSnapshotCatalog\(\) \{.*?\n    \}\n\n    function showSnapshotOperationError", self.source, re.S)
        self.assertIsNotNone(refresh)
        refresh_source = refresh.group(0)
        self.assertIn("setSnapshotCatalogRefreshBusy(true, syncRequest);", refresh_source)
        self.assertIn("setSnapshotCatalogRefreshBusy(false, syncRequest);", refresh_source)
        self.assertNotIn("modalOverlay", refresh_source)
        self.assertNotIn("appLoader", refresh_source)

        close_modal = re.search(r"function closeSnapshotsModal\(\) \{.*?\n    \}", self.source, re.S)
        self.assertIsNotNone(close_modal)
        self.assertIn("setSnapshotCatalogRefreshBusy(false);", close_modal.group(0))

    def test_snapshot_explorer_has_bounded_navigation_and_safe_states(self):
        self.assertIn("const SNAPSHOT_CACHE_LIMIT = 8", self.source)
        self.assertIn("const SNAPSHOT_PREFETCH_LIMIT = 2", self.source)
        self.assertIn("while (state.directoryCache.size > SNAPSHOT_CACHE_LIMIT)", self.source)
        self.assertIn("state.prefetchControllers.size >= SNAPSHOT_PREFETCH_LIMIT", self.source)
        for state in ("indexing", "empty", "no-match", "error", "stale", "canceled", "deleted", "blocked", "oversized"):
            self.assertTrue(f'{state}:' in self.source or f'"{state}":' in self.source)
        self.assertIn('data-state="${escapeHtml(kind)}"', self.source)
        for key in ("ArrowUp", "ArrowDown", "Home", "End", "Enter", "Escape"):
            self.assertIn(key, self.source)

    def test_cold_browse_results_do_not_start_prefetch_jobs(self):
        browse = re.search(r"async function browseSnapshot\(.*?\n    \}\n\n    async function searchSnapshot", self.source, re.S)
        self.assertIsNotNone(browse)
        browse_source = browse.group(0)
        self.assertRegex(
            browse_source,
            r'if \(isWarmSnapshotResult\(result\)\) \{\s*prefetchSnapshotDirectories\(',
        )
        self.assertIn("result.cache_hit === true", self.source)
        self.assertIn('["redis", "local", "memory"]', self.source)

    def test_small_snapshot_search_uses_local_results_before_remote_dispatch(self):
        search = re.search(r"async function searchSnapshot\(.*?\n    \}\n\n    function classifySnapshotDownloadFailure", self.source, re.S)
        self.assertIsNotNone(search)
        search_source = search.group(0)
        self.assertIn("state.entries.length < SNAPSHOT_MAX_ENTRIES", search_source)
        self.assertIn("const useLocalSearch", search_source)
        self.assertIn("finishSnapshotRequest(request);\n        return;", search_source)
        self.assertIn("normalized.path.toLowerCase().includes(needle)", search_source)
        self.assertIn("normalized.name.toLowerCase().includes(needle)", search_source)

    def test_snapshot_explorer_escapes_server_content_and_never_previews_files(self):
        for marker in (
            "escapeHtml(state.target.name)",
            "escapeHtml(id)",
            "escapeHtml(entry.path)",
            "escapeHtml(message)",
            "escapeHtml(displayName)",
        ):
            self.assertIn(marker, self.source)
        self.assertIn("Los contenidos de archivos nunca se renderizan", self.source)
        self.assertNotIn("innerHTML = result.b64_content", self.source)

    def test_jobs_polling_renders_current_state_and_preserves_log_refresh_path(self):
        render_jobs = re.search(
            r"function renderJobs\(content\) \{.*?\n    let _jobsRenderTableBody",
            self.source,
            re.S,
        )
        self.assertIsNotNone(render_jobs)
        render_source = render_jobs.group(0)
        body_start = render_source.index("const renderTableBody = () => {")
        body_end = render_source.index("const renderTable = () => {", body_start)
        body_source = render_source[body_start:body_end]
        self.assertIn("const allJobs = state.jobs || [];", body_source)
        self.assertNotIn("const allJobs = state.jobs || [];", render_source.split("const renderTableBody", 1)[0])

        polling = re.search(
            r"function startJobsPolling\(\) \{.*?\n    function stopJobsPolling",
            self.source,
            re.S,
        )
        self.assertIsNotNone(polling)
        polling_source = polling.group(0)
        state_index = polling_source.index("state.jobs = newJobs;")
        renderer_index = polling_source.index("_jobsRenderTableBody();")
        self.assertLess(state_index, renderer_index)
        self.assertIn("if (_jobsRenderTableBody)", polling_source)
        self.assertIn("renderJobs(content);", polling_source)
        self.assertIn("renderJobLogs(job);", self.source)
        self.assertIn("await refreshAll();", self.source)
        self.assertIn('["succeeded", "failed", "canceled", "cancelled"]', self.source)

    def test_log_polling_surfaces_bounded_fetch_failures_in_both_panels(self):
        for marker in (
            "LOG_POLL_MAX_FAILURES = 5",
            "LOG_POLL_FAILURE_WINDOW_MS = 60000",
            "renderLogPollingError",
            "Reintentar",
            "La consulta se detuvo tras varios fallos.",
            "pollAccordionJobLogs",
        ):
            self.assertIn(marker, self.source)
        self.assertNotIn("} catch (error) { }", self.source)

    def test_jobs_polling_surfaces_bounded_refresh_failures_without_error_details(self):
        jobs = re.search(
            r"let jobsTableState = .*?\n    function stopJobsPolling",
            self.source,
            re.S,
        )
        self.assertIsNotNone(jobs)
        jobs_source = jobs.group(0)
        polling = re.search(
            r"function startJobsPolling\(\) \{.*?\n    function stopJobsPolling",
            self.source,
            re.S,
        )
        self.assertIsNotNone(polling)
        polling_source = polling.group(0)
        for marker in (
            'id="jobsRefreshNotice"',
            'role="status"',
            'aria-live="polite"',
            "renderJobsRefreshNotice",
            "clearJobsRefreshNotice",
            "retryJobsPolling",
            "data-jobs-refresh-retry",
        ):
            self.assertIn(marker, jobs_source)
        for marker in (
            "LOG_POLL_MAX_FAILURES",
            "LOG_POLL_FAILURE_WINDOW_MS",
            "No se pudo actualizar Jobs. Reintentando",
            "La actualizacion de Jobs se detuvo tras varios fallos.",
            "jobsPollFailures += 1;",
            "jobsPolling = false;",
        ):
            self.assertIn(marker, polling_source)
        self.assertNotIn("error.message", polling_source)
        self.assertNotIn("ignore polling errors", polling_source)

    def test_hidden_jobs_refresh_notice_overrides_snapshot_status_display(self):
        self.assertIn('.snapshot-status[hidden] { display: none !important; }', self.source)
        self.assertIn('id="jobsRefreshNotice"', self.source)
        self.assertIn("notice.hidden = false;", self.source)

    def test_cron_preview_uses_backend_effective_schedule_and_explicit_timezone(self):
        for marker in (
            "/api/v1/scheduler/preview",
            "effective_cron_expression",
            "cron_source",
            "scheduler_timezone",
            "next_scheduled_at",
            "target_id",
            "target_context",
            "timeZone,",
            "state.schedulerTimezone",
        ):
            self.assertIn(marker, self.source)
        target_schedule = re.search(r"function renderTargetCronLabel\(target\) \{.*?\n    \}", self.source, re.S)
        self.assertIsNotNone(target_schedule)
        self.assertIn("targetCronExpression(target)", target_schedule.group(0))
        self.assertIn("target.next_scheduled_at", target_schedule.group(0))
        preview = re.search(r"async function updateCronPreview\(inputId, previewId, targetId, targetContext\) \{.*?\n    \}", self.source, re.S)
        self.assertIsNotNone(preview)
        self.assertIn("api/v1/scheduler/preview", preview.group(0))
        self.assertNotIn("cronNextRun(expr)", preview.group(0))

    def test_cron_preview_has_no_unreachable_browser_schedule_helpers(self):
        for helper in (
            "function parseCronExpression",
            "function cronMatchesParts",
            "function zonedCronParts",
            "function cronNextRun",
        ):
            self.assertNotIn(helper, self.source)
        self.assertIn("function cronDescribe", self.source)
        self.assertIn("/api/v1/scheduler/preview", self.source)

    def test_jobs_history_renders_and_filters_job_origin(self):
        for marker in (
            "jobTriggerInfo",
            "fmtJobTrigger",
            'manual: { cls:',
            'schedule: { cls:',
            'automatic: { cls:',
            'interactive: { cls:',
            "Desconocido",
            'data-col="origin"',
            'sortAttrs("origin")',
            "j.trigger",
            'Origen${sortIcon("origin")}',
            'colspan="8"',
        ):
            self.assertIn(marker, self.source)
        trigger_info = re.search(r"function jobTriggerInfo\(trigger\) \{.*?\n    \}", self.source, re.S)
        self.assertIsNotNone(trigger_info)
        trigger_renderer = re.search(r"function fmtJobTrigger\(trigger\) \{.*?\n    \}", self.source, re.S)
        self.assertIsNotNone(trigger_renderer)
        self.assertIn("escapeHtml", trigger_renderer.group(0))


class StorageCardsUiStateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = UI_PATH.read_text(encoding="utf-8")
        cls.css = UI_CSS_PATH.read_text(encoding="utf-8")

    def test_storage_renders_card_grid_with_filters_sort_and_pagination(self):
        storage = re.search(r"function renderStorage\(content\) \{.*?\n    let storageModalState", self.source, re.S)
        self.assertIsNotNone(storage)
        storage_source = storage.group(0)
        for marker in (
            "storageTableState",
            "storage-grid",
            "storage-card",
            'data-storage-filter="name"',
            'data-storage-filter="backend"',
            "storageCardsContainer",
            "storageEmptyState",
            "Sin storage profiles",
            "Sin profiles que coincidan",
            "Mostrando ",
        ):
            self.assertIn(marker, storage_source)
        self.assertNotIn('<tbody id="storageTbody"', storage_source)

    def test_storage_pagination_resets_page_on_filter_change_and_sorts(self):
        storage = re.search(r"function renderStorage\(content\) \{.*?\n    let storageModalState", self.source, re.S)
        self.assertIsNotNone(storage)
        storage_source = storage.group(0)
        for marker in (
            'storageTableState.page = 1',
            "storageTableState.filters[input.dataset.storageFilter] = input.value",
            "storageTableState.sortKey",
            "storageTableState.sortDir",
            "storagePageSize",
            "renderStorageCards()",
        ):
            self.assertIn(marker, storage_source)
        self.assertIn("storageTableState.page = parseInt(btn.dataset.storagePage, 10)", self.source)

    def test_storage_cards_have_independent_about_state_and_no_polling(self):
        storage = re.search(r"function renderStorage\(content\) \{.*?\n    let storageModalState", self.source, re.S)
        self.assertIsNotNone(storage)
        storage_source = storage.group(0)
        for marker in (
            "storageAboutState",
            "about-unsupported",
            "transient-failure",
            "not-configured",
            "About not supported",
            "No configurado",
            "Refrescar",
            "storage-profiles/${encodeURIComponent(profileId)}/about",
        ):
            self.assertIn(marker, storage_source)
        self.assertNotIn("setInterval", storage_source)
        self.assertNotIn("setTimeout", storage_source)

    def test_storage_cards_escape_displayed_values_and_never_render_secrets(self):
        card = re.search(r"function renderStorageCard\(profile\) \{.*?\n    \}", self.source, re.S)
        self.assertIsNotNone(card)
        card_source = card.group(0)
        for marker in (
            "escapeHtml(profile.name)",
            "escapeHtml(profile.backend_type)",
            "escapeHtml(about.error)",
        ):
            self.assertIn(marker, card_source)
        self.assertNotIn("environment", card_source)
        self.assertNotIn("secret_refs", card_source)
        self.assertNotIn("RCLONE_REMOTE", card_source)

    def test_storage_card_grid_css_is_responsive_and_reuses_pagination(self):
        css = self.css
        for marker in (
            ".storage-grid",
            ".storage-card",
            "repeat(auto-fill, minmax(",
        ):
            self.assertIn(marker, css)
        self.assertIn("@media (max-width: 768px)", css)
        self.assertNotIn("storage-grid", self.source.split("renderStorage(content)", 1)[0])


if __name__ == "__main__":
    unittest.main()
