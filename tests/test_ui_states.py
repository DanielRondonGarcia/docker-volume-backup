import json
import shutil
import subprocess
import unittest
import re
from pathlib import Path


UI_PATH = Path(__file__).resolve().parents[1] / "src" / "control_plane" / "ui" / "index.html"
UI_CSS_PATH = Path(__file__).resolve().parents[1] / "src" / "control_plane" / "ui" / "styles" / "components.css"
UI_APP_CSS_PATH = Path(__file__).resolve().parents[1] / "src" / "control_plane" / "ui" / "styles" / "app.css"


class SnapshotExplorerUiStateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = UI_PATH.read_text(encoding="utf-8")
        cls.css = UI_CSS_PATH.read_text(encoding="utf-8")
        cls.app_css = UI_APP_CSS_PATH.read_text(encoding="utf-8")

    def test_worker_commands_modal_removes_http_warning_and_keeps_renewal_compose_markers(self):
        self.assertNotIn("HTTP no es confidencial", self.source)
        for marker in (
            "data-renew-enrollment=",
            "Renovar enrollment",
            "Renovacion de enrollment",
            "docker-compose.yml",
            "/tmp:/tmp",
        ):
            self.assertIn(marker, self.source)

    def test_snapshot_explorer_contains_v2_routes_and_legacy_fallbacks(self):
        for route in ("/api/v2/targets/", "/snapshots`,", "/api/v2/jobs/", "/cancel`,"):
            self.assertIn(route, self.source)
        for operation in ('"browse"', '"search"', '"dump"'):
            self.assertIn(operation, self.source)
        self.assertIn("/api/v1/targets/${encodeURIComponent(target.id)}/snapshots", self.source)
        self.assertIn("/snapshot-ls", self.source)
        self.assertIn("/snapshot-dump", self.source)
        self.assertIn("/api/v1/jobs/${encodeURIComponent(request.jobId)}", self.source)

    def test_snapshot_about_action_uses_v2_polling_and_renders_safe_logical_details(self):
        for marker in (
            'data-snapshot-about',
            '>Información</button>',
            'requestSnapshotOperation(request, "about"',
            '/api/v2/targets/${encodeURIComponent(request.targetId)}/${operation}',
            'Tamaño lógico/restaurable',
            'total_file_count',
            'snapshots_count',
            'Redis (cache hit)',
            'renderSnapshotAboutLoading',
            'data-snapshot-about-open-browser',
            'La consulta fue cancelada.',
        ):
            self.assertIn(marker, self.source)
        about = re.search(r"async function loadSnapshotAbout\(.*?\n    \}", self.source, re.S)
        self.assertIsNotNone(about)
        about_source = about.group(0)
        for marker in (
            'beginSnapshotRequest("about", selectedId, null)',
            'request_id: `ui-${request.seq}`',
            'isCurrentSnapshotRequest(request)',
            'finishSnapshotRequest(request)',
        ):
            self.assertIn(marker, about_source)
        self.assertIn('snapshot-poll-timeout', self.source)
        navigation = re.search(r"function handleSnapshotNavigationClick\(event\) \{.*?\n    \}", self.source, re.S)
        self.assertIsNotNone(navigation)
        self.assertIn('void loadSnapshotAbout(about.dataset.snapshotId)', navigation.group(0))
        self.assertIn('!event.target.closest("[data-snapshot-restore], [data-snapshot-about]")', navigation.group(0))
        self.assertIn('escapeHtml(source)', self.source)
        self.assertIn('metadataValue(normalized.created_at ? fmtDate(normalized.created_at) : "")', self.source)

    def test_snapshot_about_uses_bounded_page_session_cache_and_honest_source(self):
        for marker in (
            "SNAPSHOT_ABOUT_SESSION_CACHE_LIMIT = 24",
            "const snapshotAboutSessionCache = new Map()",
            "snapshotAboutSessionCacheKey(targetId, snapshotId)",
            "getSnapshotAboutSessionCache(state.target.id, selectedId)",
            "setSnapshotAboutSessionCache(state.target.id, selectedId, state.about)",
            "snapshotAboutSessionCache.size > SNAPSHOT_ABOUT_SESSION_CACHE_LIMIT",
            'source: "browser-session-cache"',
            'normalized.source === "browser-session-cache"',
            "if (result && result.changed) clearSnapshotAboutSessionCache(current.target.id)",
            "if (result && result.changed) clearSnapshotAboutSessionCache(state.target.id)",
        ):
            self.assertIn(marker, self.source)
        about = re.search(r"async function loadSnapshotAbout\(.*?\n    \}", self.source, re.S)
        self.assertIsNotNone(about)
        about_source = about.group(0)
        self.assertIn("invalidateSnapshotRequest(false);", about_source)
        self.assertIn("const cachedAbout = bypassSessionCache ? null : getSnapshotAboutSessionCache(state.target.id, selectedId);", about_source)
        self.assertIn("if (cachedAbout)", about_source)
        self.assertLess(about_source.index("getSnapshotAboutSessionCache"), about_source.index("requestSnapshotOperation"))

    def test_snapshot_about_retry_bypasses_page_session_cache(self):
        retry = re.search(r"function retrySnapshotAction\(\) \{.*?\n    \}", self.source, re.S)
        self.assertIsNotNone(retry)
        self.assertIn('loadSnapshotAbout(state.currentSnapshot, { bypassSessionCache: true });', retry.group(0))
        about = re.search(r"async function loadSnapshotAbout\(.*?\n    \}", self.source, re.S)
        self.assertIsNotNone(about)
        self.assertIn("const bypassSessionCache = options.bypassSessionCache === true;", about.group(0))
        self.assertIn("const cachedAbout = bypassSessionCache ? null", about.group(0))

    def test_target_stats_action_is_restic_only_and_cache_first_with_bounded_job_refresh(self):
        for marker in (
            'data-action="target-stats"',
            ">Ver stats</button>",
            'api/v1/targets/${encodeURIComponent(request.targetId)}/stats',
            'api/v1/targets/${encodeURIComponent(request.targetId)}/stats-sync',
            'TARGET_STATS_MODES = ["raw-data", "blobs-per-file"]',
            'Fuente:</strong> Registro persistido',
            'Última actualización:</strong>',
            'raw-data representa almacenamiento físico/deduplicado',
            'blobs-per-file',
            'connectJobEvents(request.jobId',
            'Actualizando por polling',
            'state.currentRole === "admin" || state.currentRole === "operator"',
        ):
            self.assertIn(marker, self.source)
        actions = re.search(r"function bindTargetActions\(\).*?\n    \}", self.source, re.S)
        self.assertIsNotNone(actions)
        self.assertIn('else if (action === "target-stats") { await openTargetStats(target, e.currentTarget); }', actions.group(0))
        self.assertNotIn('data-action="target-stats"', re.search(r"function renderSnapshotList\(\).*?\n    \}", self.source, re.S).group(0))

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

    def test_target_path_storage_control_loads_edit_value_and_is_sent_on_create_and_update(self):
        targets = re.search(r"function renderTargets\(content\) \{.*?\n    \}", self.source, re.S)
        self.assertIsNotNone(targets)
        targets_source = targets.group(0)
        for marker in (
            'id="targetPathStorage"',
            "Ruta remota del target (opcional)",
        ):
            self.assertIn(marker, targets_source)
        self.assertIn(
            'path_storage: emptyToNull(document.getElementById("targetPathStorage").value)',
            self.source,
        )
        edit = re.search(r"function openEditTargetModal\(target\) \{.*?\n    \}", self.source, re.S)
        self.assertIsNotNone(edit)
        edit_source = edit.group(0)
        for marker in (
            'id="etPathStorage"',
            'value="${escapeHtml(target.path_storage || "")}"',
            'path_storage: emptyToNull(document.getElementById("etPathStorage").value)',
        ):
            self.assertIn(marker, edit_source)

    def test_target_volume_selector_uses_source_candidates_and_sends_source_identity(self):
        for marker in (
            "Array.isArray(project.volume_candidates)",
            "candidate.anonymous !== true",
            "data-source=\"${escapeHtml(candidate.source || \"\")}\"",
            "Servicio:",
            "Contenedor:",
            "volume_sources: hasExplicitVolumeSources ? selectedSources : null",
            "function getSelectedVolumeSources()",
        ):
            self.assertIn(marker, self.source)
        self.assertNotIn("Object.keys(runtimeVols).find(k => runtimeVols[k].bind === v)", self.source)

        volume_cell = re.search(r"function renderVolumeTargetsCell\(target\) \{.*?\n    \}", self.source, re.S)
        self.assertIsNotNone(volume_cell)
        volume_cell_source = volume_cell.group(0)
        self.assertIn("Object.entries(runtimeVols)", volume_cell_source)
        self.assertIn(".filter(([_, v]) => v && v.bind === p)", volume_cell_source)
        self.assertIn(".map(([name]) => name)", volume_cell_source)

    def test_edit_target_volume_selector_loads_inventory_preselects_and_preserves_unavailable_state(self):
        edit = re.search(r"function openEditTargetModal\(target\) \{.*?\n    \}", self.source, re.S)
        self.assertIsNotNone(edit)
        edit_source = edit.group(0)
        for marker in (
            "Volúmenes incluidos en el backup",
            'id="editTargetVolumeList"',
            'id="editTargetVolumeSummary"',
            "fetchWorkerInventory(workerId)",
            "getTargetVolumeSelectionKeys(target, candidates)",
            'targetUpdate.volume_sources = getSelectedVolumeSourcesFor("editTargetVolumeList")',
            "editVolumeSelectionReady",
            "No se pudo cargar el inventario del worker",
            "La selección actual se conservará",
        ):
            self.assertIn(marker, edit_source)
        for marker in (
            "target.runtime_volumes",
            "target.volume_targets",
            '<input type="checkbox" class="volume-checkbox"',
            'aria-expanded="${String(isExpanded)}"',
            "volume-inline-warning",
        ):
            self.assertIn(marker, self.source)

        if not shutil.which("node"):
            self.skipTest("Node.js is not installed")
        helper = re.search(
            r"(function getTargetVolumeSelectionKeys\(target, candidates\) \{.*?\n    \})\n\n    function bindTargetForm",
            self.source,
            re.S,
        )
        self.assertIsNotNone(helper)
        script = """
const volumeCandidateKey = candidate => candidate.source || candidate.bind;
""" + helper.group(1) + r'''
const candidates = [
  { source: "source-a", bind: "/shared" },
  { source: "source-b", bind: "/shared" },
  { source: "source-c", bind: "/other" },
];
const bySource = [...getTargetVolumeSelectionKeys({
  volume_targets: ["/shared"],
  runtime_volumes: { "source-a": { bind: "/shared", mode: "rw" } },
}, candidates)];
const byLegacyPath = [...getTargetVolumeSelectionKeys({
  volume_targets: ["/shared"],
  runtime_volumes: {},
}, candidates)];
const byEmptyLegacy = [...getTargetVolumeSelectionKeys({ volume_targets: [], runtime_volumes: {} }, candidates)];
process.stdout.write(JSON.stringify({ bySource, byLegacyPath, byEmptyLegacy }));
'''
        result = subprocess.run(
            ["node", "--input-type=commonjs", "--eval", script],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        output = json.loads(result.stdout)
        self.assertEqual(output["bySource"], ["source-a"])
        self.assertEqual(set(output["byLegacyPath"]), {"source-a", "source-b"})
        self.assertEqual(set(output["byEmptyLegacy"]), {"source-a", "source-b", "source-c"})

    def test_blocked_targets_are_opaque_and_cannot_run_or_enable_on_ineligible_workers(self):
        for marker in (
            "target-execution-blocked",
            "execution_blocked",
            "worker_revoked",
            "worker_missing",
            "target_disabled",
            "enableBlocked",
            "toggleDisabled ? \"disabled\" : \"\"",
            "aria-label=\"${escapeHtml(toggleTitle)}\"",
            "state.currentRole === \"admin\"",
            "state.currentRole === \"admin\" || state.currentRole === \"operator\"",
            "startTargetsPolling();",
            "apiGet(\"/api/v1/targets\")",
        ):
            self.assertIn(marker, self.source)

    def test_targets_table_has_fluid_desktop_columns_and_responsive_scroll_contract(self):
        table = re.search(r'<table class="table datatable targets-table">.*?</table>', self.source, re.S)
        self.assertIsNotNone(table)
        table_source = table.group(0)
        for marker in (
            "<colgroup>",
            'class="target-col-name"',
            'class="target-col-worker"',
            'class="target-col-compose"',
            'class="target-col-mode"',
            'class="target-col-volumes"',
            'class="target-col-retention"',
            'class="target-col-actions"',
            'class="target-col-cron"',
            'class="target-col-toggle"',
        ):
            self.assertIn(marker, table_source)
        self.assertRegex(
            self.css,
            r"\.targets-table \{\s*width: 100%;\s*min-width: 0;\s*table-layout: fixed;\s*\}",
        )
        column_widths = dict(
            re.findall(r"\.targets-table col\.(target-col-[\w-]+) \{ width: (\d+)%; \}", self.css)
        )
        self.assertEqual(
            set(column_widths),
            {
                "target-col-name",
                "target-col-worker",
                "target-col-compose",
                "target-col-mode",
                "target-col-volumes",
                "target-col-retention",
                "target-col-cron",
                "target-col-toggle",
                "target-col-actions",
            },
        )
        self.assertEqual(sum(map(int, column_widths.values())), 100)
        self.assertEqual(int(column_widths["target-col-actions"]), 10)
        self.assertNotIn("min-width: 1280px", self.css)
        for marker in (
            ".targets-table th.col-actions,",
            ".targets-table td.action-cell { display: table-cell;",
            ".targets-table .target-cron-cell",
            ".targets-table .vol-cell code",
            ".targets-table th, .targets-table td { padding-inline: 8px; }",
            ".targets-table .toggle-cell { white-space: normal; }",
        ):
            self.assertIn(marker, self.css)
        self.assertIn("overflow-x: auto", self.app_css)
        self.assertIn("min-width: 0", self.app_css)

    def test_targets_mobile_layout_keeps_cards_labels_filters_actions_and_logs_visible(self):
        table = re.search(r'<table class="table datatable targets-table">.*?</table>', self.source, re.S)
        self.assertIsNotNone(table)
        table_source = table.group(0)
        for marker in (
            'data-label="Nombre"',
            'data-label="Worker"',
            'data-label="Compose project"',
            'data-label="Modo"',
            'data-label="Vol&uacute;menes"',
            'data-label="Retenci&oacute;n"',
            'data-label="Cron"',
            'data-label="Activo"',
            'data-label="Acciones"',
            'class="empty-targets-row"',
        ):
            self.assertIn(marker, self.source)
        for marker in (
            'aria-label="Filtrar nombre"',
            'aria-label="Filtrar worker"',
            'aria-label="Filtrar compose"',
            'aria-label="Filtrar modo"',
            'aria-label="Filtrar retencion"',
            'aria-label="Filtrar cron"',
        ):
            self.assertIn(marker, table_source)
        mobile = re.search(r'@media \(max-width: 1200px\) \{.*?\n\}', self.css, re.S)
        self.assertIsNotNone(mobile)
        mobile_css = mobile.group(0)
        self.assertIn("@media (max-width: 1200px)", mobile_css)
        for marker in (
            ".targets-table {",
            "min-width: 0",
            ".targets-table > thead > tr.filter-row",
            "grid-template-columns: repeat(auto-fit",
            ".targets-table > tbody > tr:not(.target-log-row):not(.empty-targets-row)",
            "content: attr(data-label)",
            ".targets-table > tbody > tr.target-log-row",
            ".targets-table > tbody > tr.target-log-row > td::before { display: none;",
            ".action-cell .dropdown-trigger",
            "min-height: 44px",
            "overflow-wrap: anywhere",
        ):
            self.assertIn(marker, mobile_css)
        for marker in (
            ".targets-table > thead > tr.filter-row > th:nth-child(5)",
            ".targets-table > thead > tr.filter-row > th:nth-child(8)",
            ".targets-table > thead > tr.filter-row > th:nth-child(9)",
        ):
            self.assertIn(marker, mobile_css)
        self.assertIn("overflow-x: hidden", self.app_css)
        self.assertIn("min-width: 0", self.app_css)
        for marker in ("renderTargetLogDetailRow(t)", "positionTargetDropdown(trigger, menu)", "overflow-y: auto"):
            self.assertIn(marker, self.source if marker != "overflow-y: auto" else self.css)

    def test_target_action_cell_keeps_table_layout_and_non_target_tables_keep_flex_actions(self):
        self.assertIsNone(re.search(r"(?m)^\.action-cell\s*\{\s*display:\s*flex;", self.css))
        self.assertIn(".targets-table td.action-cell { display: table-cell;", self.css)
        self.assertIn(".targets-table .action-cell > .dropdown { display: flex;", self.css)
        self.assertIn(".table:not(.targets-table) .action-cell { display: flex;", self.css)

    def test_target_polling_keeps_worker_display_state_without_heartbeat_rerenders(self):
        fingerprint = re.search(
            r"function workerFingerprint\(worker\) \{.*?\n    \}\n\n    async function revokeWorker",
            self.source,
            re.S,
        )
        self.assertIsNotNone(fingerprint)
        fingerprint_source = fingerprint.group(0)
        self.assertNotIn("last_seen_at", fingerprint_source)
        self.assertIn("worker.status", fingerprint_source)
        self.assertIn("worker.version", fingerprint_source)
        self.assertIn("worker.labels[key]", fingerprint_source)

        polling = re.search(r"function startTargetsPolling\(\) \{.*?\n    \}\n\n    function stopTargetsPolling", self.source, re.S)
        self.assertIsNotNone(polling)
        polling_source = polling.group(0)
        self.assertIn("state.workers = newWorkers;", polling_source)
        self.assertIn("newWorkers.map(workerFingerprint)", polling_source)
        self.assertIn("if ((targetsChanged || workersChanged) && state.currentView === \"targets\") renderTargetsTable();", polling_source)

    def test_target_action_dropdown_flips_clamps_and_scrolls_without_losing_button_semantics(self):
        position = re.search(r"function positionTargetDropdown\(trigger, menu\) \{.*?\n    \}\n\n    function bindTargetActions", self.source, re.S)
        self.assertIsNotNone(position)
        position_source = position.group(0)
        for marker in (
            "trigger.getBoundingClientRect()",
            "menu.getBoundingClientRect()",
            "window.innerHeight",
            "window.innerWidth",
            "aboveTop",
            "Math.max",
            "Math.min",
            "menu.style.top",
            "menu.style.left",
        ):
            self.assertIn(marker, position_source)
        for marker in (
            'aria-haspopup="menu"',
            'aria-expanded="false"',
            'role="menu"',
            "setTargetDropdownOpen(menu, false)",
            'target.closest(".dropdown")',
        ):
            self.assertIn(marker, self.source)
        for marker in ("max-height: calc(100vh - 16px)", "max-width: calc(100vw - 16px)", "overflow-y: auto"):
            self.assertIn(marker, self.css)

    def test_target_action_dropdown_survives_rerenders_and_keeps_keyboard_focus(self):
        body = re.search(
            r"function renderTargetsTableBody\(preservedDropdown = captureTargetDropdownState\(\)\) \{.*?\n    function renderVolumeTargetsCell",
            self.source,
            re.S,
        )
        self.assertIsNotNone(body)
        body_source = body.group(0)
        self.assertIn("restoreTargetDropdownState(preservedDropdown);", body_source)
        self.assertNotIn("closeTargetDropdowns()", body_source)

        view = re.search(r"function setView\(view\) \{.*?\n    function route", self.source, re.S)
        self.assertIsNotNone(view)
        view_source = view.group(0)
        for marker in ("preservedTargetDropdown", "captureTargetDropdownState()", "restoreTargetDropdownState(preservedTargetDropdown)"):
            self.assertIn(marker, view_source)
        self.assertIn("if (state.currentView !== view && currentLogJobId) closeLogPanel();", view_source)
        self.assertNotIn("reopenLogPanel(currentLogJobId", view_source)

        polling = re.search(r"function startTargetsPolling\(\) \{.*?\n    \}\n\n    function stopTargetsPolling", self.source, re.S)
        self.assertIsNotNone(polling)
        self.assertNotIn("closeTargetDropdowns", polling.group(0))
        for marker in ("ArrowDown", "ArrowUp", "Home", "End", "Escape", "focusTargetDropdownItem", 'role="menuitem"'):
            self.assertIn(marker, self.source)

    def test_target_logs_use_exact_label_and_inline_detail_rows(self):
        target_body = re.search(
            r"function renderTargetsTableBody\(.*?\) \{.*?\n    function renderVolumeTargetsCell",
            self.source,
            re.S,
        )
        self.assertIsNotNone(target_body)
        target_source = target_body.group(0)
        self.assertIn(">Ver ultimos logs</button>", target_source)
        self.assertNotIn(">Ver logs</button>", target_source)
        for marker in (
            "renderTargetLogDetailRow(t)",
            "return targetRow +",
            'data-target-log-row',
            '<td colspan="9">',
            'role="region"',
            'data-target-log-close',
            "function targetLogContentId",
            "function closeLogPanel()",
        ):
            self.assertIn(marker, self.source)

    def test_target_log_selection_is_singleton_and_reopens_without_stale_poll_updates(self):
        for marker in (
            "let currentLogTargetId = null;",
            "let currentLogJobSnapshot = null;",
            "if (currentLogTargetId && !selectedTarget) closeLogPanel();",
            "currentLogTargetId = null;",
            "currentLogJobSnapshot = job;",
            "reopenLogPanel(currentLogJobId, currentLogTargetId ||",
            "if (currentLogJobId !== jobId) return;",
            "renderTargetLogPanelInto(host, jobId, tid);",
            "ensureTargetLogFallback",
        ):
            self.assertIn(marker, self.source)
        self.assertGreaterEqual(self.source.count("if (currentLogJobId !== jobId) return;"), 2)

    def test_jobs_log_accordion_markers_remain_separate(self):
        for marker in (
            "function toggleJobLogAccordion(jobId, content)",
            "log-accordion-",
            "function closeJobLogAccordion()",
            "function pollAccordionJobLogs(jobId)",
            "async function loadAccordionJobLogs(jobId)",
            'data-view-logs',
            "renderJobLogsInto(job, container)",
        ):
            self.assertIn(marker, self.source)

    def test_initial_job_log_loaders_fetch_encoded_finite_detail_for_both_panels(self):
        target_loader = re.search(r"async function loadTargetJobLogs\(jobId\) \{.*?\n    \}", self.source, re.S)
        accordion_loader = re.search(r"async function loadAccordionJobLogs\(jobId\) \{.*?\n    \}", self.source, re.S)
        self.assertIsNotNone(target_loader)
        self.assertIsNotNone(accordion_loader)
        for loader in (target_loader.group(0), accordion_loader.group(0)):
            self.assertIn("apiGet(`/api/v1/jobs/${encodeURIComponent(jobId)}/logs`)", loader)
            self.assertIn("if (!isActive()) return;", loader)
            self.assertIn("mergeJobProjection(job);", loader)
            self.assertIn("isTerminalJobStatus(job.status)", loader)
            self.assertIn("Finalizado", loader)

        target_open = re.search(r"function openLogPanel\(jobId, targetId\) \{.*?\n    \}\n\n    function closeLogPanel", self.source, re.S)
        accordion_open = re.search(r"function toggleJobLogAccordion\(jobId, content\) \{.*?\n    \}\n\n    function closeJobLogAccordion", self.source, re.S)
        self.assertIsNotNone(target_open)
        self.assertIsNotNone(accordion_open)
        self.assertIn("void loadTargetJobLogs(jobId);", target_open.group(0))
        self.assertIn("void loadAccordionJobLogs(jobId);", accordion_open.group(0))
        self.assertNotIn("if (existingJob && isTerminalJobStatus(existingJob.status))", target_open.group(0))
        self.assertNotIn("if (existingJob && isTerminalJobStatus(existingJob.status))", accordion_open.group(0))

    def test_manual_target_backup_uses_accessible_hot_cold_dialog_and_override_payload(self):
        for marker in (
            "function openBackupRunModal",
            'role="dialog" aria-modal="true"',
            'name="targetBackupMode"',
            'value="hot"',
            'value="cold"',
            'aria-live="polite"',
            "targetRunCancel",
            "targetRunExecute",
            "backup_mode: selectedMode",
            "El modo seleccionado aplica solo a esta ejecucion",
        ):
            self.assertIn(marker, self.source)

    def test_inactive_targets_keep_manual_backup_and_snapshot_restore_actions(self):
        for marker in (
            "function targetExecutionBlocked(target)",
            'target.blocked_reason !== "target_disabled"',
            "const schedulingDisabled = Boolean(t.scheduling_disabled ?? !t.enabled);",
            "const runDisabled = !canRunTargets || executionBlocked;",
            "Target inactivo: el cron está deshabilitado",
            "acciones manuales disponibles",
            'data-action="snapshots"',
            "if (targetExecutionBlocked(target) || ![\"admin\", \"operator\"].includes(state.currentRole)) return;",
        ):
            self.assertIn(marker, self.source)
        self.assertNotIn("const runDisabled = !canRunTargets || executionBlocked || !t.enabled;", self.source)

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

    def test_snapshot_directory_pagination_has_accessible_bounded_control(self):
        for marker in (
            "const SNAPSHOT_PAGE_SIZE = 200",
            "const SNAPSHOT_MAX_DIRECTORY_ENTRIES = 10_000",
            "directoryLoadedLimit: SNAPSHOT_PAGE_SIZE",
            "directoryHasMore: false",
            "loadingMore: false",
            "data-snapshot-load-more",
            "Cargar más",
            'aria-busy="${state.loadingMore ? "true" : "false"}"',
            '${state.loadingMore ? "disabled" : ""}',
            ".snapshot-pagination-control",
            "justify-content: center",
        ):
            self.assertIn(marker, self.source)

        render = re.search(r"function renderSnapshotBrowser\(.*?\n    \}\n\n    function selectSnapshot", self.source, re.S)
        self.assertIsNotNone(render)
        render_source = render.group(0)
        self.assertIn("!state.searchActive && state.directoryHasMore", render_source)
        self.assertIn("state.loadingMore", render_source)
        self.assertIn("${loadMoreMarkup}", render_source)

    def test_snapshot_directory_load_more_increases_limit_replaces_response_and_stops_at_cap(self):
        browse = re.search(r"async function browseSnapshot\(.*?\n    \}\n\n    async function loadMoreSnapshotDirectory", self.source, re.S)
        self.assertIsNotNone(browse)
        browse_source = browse.group(0)
        for marker in (
            "const requestedLimit = cachedDirectory ? cachedDirectory.loadedLimit : SNAPSHOT_PAGE_SIZE",
            "max_entries: requestedLimit",
            "state.entries = resultEntries.slice(0, SNAPSHOT_MAX_DIRECTORY_ENTRIES)",
            "state.directoryHasMore = resultEntries.length >= requestedLimit && requestedLimit < SNAPSHOT_MAX_DIRECTORY_ENTRIES",
            "cacheSnapshotDirectory(state.currentSnapshot, normalizedPath, state.entries, requestedLimit, state.directoryHasMore, state.listingComplete)",
        ):
            self.assertIn(marker, browse_source)

        load_more = re.search(r"async function loadMoreSnapshotDirectory\(.*?\n    \}\n\n    async function searchSnapshot", self.source, re.S)
        self.assertIsNotNone(load_more)
        load_more_source = load_more.group(0)
        for marker in (
            "const nextLimit = Math.min(currentLimit + SNAPSHOT_PAGE_SIZE, SNAPSHOT_MAX_DIRECTORY_ENTRIES)",
            "state.currentPath",
            "state.loadingMore = true",
            "max_entries: nextLimit",
            "state.entries = resultEntries.slice(0, SNAPSHOT_MAX_DIRECTORY_ENTRIES)",
            "state.directoryHasMore = resultEntries.length >= nextLimit && nextLimit < SNAPSHOT_MAX_DIRECTORY_ENTRIES",
            "cacheSnapshotDirectory(snapshotId, normalizedPath, state.entries, nextLimit, state.directoryHasMore, state.listingComplete)",
            "state.loadingMore = false",
        ):
            self.assertIn(marker, load_more_source)

    def test_snapshot_directory_pagination_resets_on_navigation_and_search_and_prefetch_stays_small(self):
        for function_name, end_marker in (
            ("function selectSnapshot", "function moveSnapshotFocus"),
            ("async function browseSnapshot", "async function loadMoreSnapshotDirectory"),
            ("async function searchSnapshot", "function classifySnapshotDownloadFailure"),
        ):
            block = re.search(rf"{re.escape(function_name)}\(.*?\n    \}}\n\n    {re.escape(end_marker)}", self.source, re.S)
            self.assertIsNotNone(block)
            self.assertIn("resetSnapshotDirectoryState(state)", block.group(0))

        prefetch = re.search(r"function prefetchSnapshotDirectories\(.*?\n    \}\n\n    function openRestoreFromSnapshotModal", self.source, re.S)
        self.assertIsNotNone(prefetch)
        prefetch_source = prefetch.group(0)
        self.assertIn("max_entries: SNAPSHOT_PAGE_SIZE", prefetch_source)
        self.assertIn("resultEntries.length >= SNAPSHOT_PAGE_SIZE", prefetch_source)
        self.assertIn("loadedLimit: normalizedLimit", self.source)

    def test_snapshot_browser_distinguishes_confirmed_empty_from_failed_listing(self):
        success = re.search(r"function isSuccessfulSnapshotResult\(.*?\n    \}", self.source, re.S)
        self.assertIsNotNone(success)
        self.assertIn("if (!normalized || normalized.error) return false", success.group(0))
        self.assertIn("listing_complete", success.group(0))

        render = re.search(r"function renderSnapshotBrowser\(.*?\n    \}", self.source, re.S)
        self.assertIsNotNone(render)
        render_source = render.group(0)
        self.assertIn("!visibleEntries.length && !notice", render_source)
        self.assertIn("Listado confirmado", render_source)
        self.assertIn("El listado quedó incompleto y no se puede confirmar que la carpeta esté vacía.", render_source)

        for marker in (
            "classifySnapshotListingFailure",
            "El listado supera el límite configurado.",
            "El listado tardó demasiado porque contiene muchos elementos.",
            'kind: "incomplete"',
            "state.listingComplete = false",
        ):
            self.assertIn(marker, self.source)

    def test_snapshot_authentication_failure_explains_secret_action_without_init_guidance(self):
        for marker in (
            "SNAPSHOT_AUTHENTICATION_ERROR_MESSAGE",
            "No se pudo desbloquear el repositorio Restic.",
            "contraseña o clave de Restic",
            "diferente de la usada para crear el repositorio",
            "secreto de Restic configurado para el target en Settings",
            "No ejecutes restic init a menos que confirmes que el repositorio no existe.",
            'code === "authentication"',
            "wrong\\s+\\S+\\s+or\\s+no\\s+key\\s+found",
            "failure.listing_error_code = normalizedSyncResult.listing_error_code;",
            "classifySnapshotListingFailure(error)",
            "retry: true",
        ):
            self.assertIn(marker, self.source)

        classifier = re.search(r"function classifySnapshotListingFailure\(result\) \{.*?\n    \}", self.source, re.S)
        self.assertIsNotNone(classifier)
        classifier_source = classifier.group(0)
        self.assertIn('code === "authentication"', classifier_source)
        self.assertIn("SNAPSHOT_AUTHENTICATION_ERROR_MESSAGE", classifier_source)

        refresh = re.search(r"async function refreshSnapshotCatalog\(\) \{.*?\n    \}\n\n    function showSnapshotOperationError", self.source, re.S)
        self.assertIsNotNone(refresh)
        refresh_source = refresh.group(0)
        self.assertIn("normalizeSnapshotContract(syncResult)", refresh_source)
        self.assertIn("listingFailure ?", refresh_source)
        self.assertIn("renderSnapshotState(state.catalogStatus, state.catalogError, { retry: true })", refresh_source)

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
        state_index = polling_source.index("state.jobs = mergedJobs;")
        renderer_index = polling_source.index("_jobsRenderTableBody();")
        self.assertLess(state_index, renderer_index)
        self.assertIn("if (_jobsRenderTableBody)", polling_source)
        self.assertIn("renderJobs(content);", polling_source)
        self.assertIn("renderJobLogs(job);", self.source)
        self.assertIn("await refreshAll();", self.source)
        self.assertIn('["succeeded", "failed", "canceled", "cancelled"]', self.source)

    def test_jobs_list_refresh_uses_metadata_and_preserves_loaded_detail_projection(self):
        refresh = re.search(r"async function refreshAll\(\) \{.*", self.source, re.S)
        self.assertIsNotNone(refresh)
        refresh_source = refresh.group(0)
        self.assertNotIn("include_logs=true", self.source)
        self.assertIn('apiGet("/api/v1/jobs")', refresh_source)
        self.assertIn("state.jobs = mergeJobList(jobs.items || []);", refresh_source)
        self.assertIn("function mergeJobList(items)", self.source)
        self.assertIn("currentLogJobSnapshot", self.source)
        self.assertIn('for (const key of ["result_summary", "storage_context", "progress", "log_lines"])', self.source)
        self.assertIn("mergeJobProjection(job);", self.source)
        self.assertIn("const mergedJobs = mergeJobList(newJobs);", self.source)
        self.assertIn("state.jobs = mergedJobs;", self.source)

    def test_job_logs_render_progress_storage_context_and_normalize_in_progress(self):
        for marker in (
            "function normalizeJobStatus(status)",
            'if (value === "running") return "in_progress"',
            "function renderJobProgress(job)",
            "role=\"progressbar\"",
            "job-progress-fill indeterminate",
            "function renderJobStorageContext(job)",
            "function jobPhaseLabel(phase)",
            'initializing: "Inicializando"',
            'finalizing: "Finalizando"',
            'phaseLabel ? `Estado: ${phaseLabel}`',
            "Storage no configurado",
            "Repositorio no configurado",
            "j.updated_at",
            "j.progress",
            "renderJobProgress(job)",
            "renderJobStorageContext(job)",
            "function pollJobLogs(jobId)",
            "apiGet(`/api/v1/jobs/${encodeURIComponent(jobId)}/logs`)",
            "function pollAccordionJobLogs(jobId)",
            "apiGet(`/api/v1/jobs/${encodeURIComponent(jobId)}/logs`)",
            "setInterval(fetchJob, 2000)",
            "setInterval(fetchJobLogs, 3000)",
        ):
            self.assertIn(marker, self.source)
        self.assertIn(".job-progress-fill.indeterminate", self.css)
        self.assertIn("prefers-reduced-motion", self.css)

    def test_job_detail_keeps_summary_in_header_outside_scrolling_log_list(self):
        for marker in (
            'class="job-detail-summary" data-job-detail-summary',
            "function renderJobDetailSummary(job, root)",
            "renderJobDetailSummary(job, root);",
            'renderJobDetailSummary(job, container.closest(".log-panel"));',
        ):
            self.assertIn(marker, self.source)
        for marker in (
            ".job-detail-summary",
            ".job-detail-summary > .job-progress",
            ".job-detail-summary > .job-storage-context",
            "align-items: stretch",
            "width: 100%",
            ".log-panel .log-container",
        ):
            self.assertIn(marker, self.css)

        target_renderer = re.search(
            r"function renderTargetJobLogsInto\(job, root\) \{.*?\n    \}",
            self.source,
            re.S,
        )
        accordion_renderer = re.search(
            r"function renderJobLogsInto\(job, container\) \{.*?\n    \}",
            self.source,
            re.S,
        )
        self.assertIsNotNone(target_renderer)
        self.assertIsNotNone(accordion_renderer)
        for renderer in (target_renderer.group(0), accordion_renderer.group(0)):
            self.assertIn("renderJobDetailSummary", renderer)
            self.assertNotIn("renderJobProgress(job)", renderer)
            self.assertNotIn("renderJobStorageContext(job)", renderer)

        for markup in (
            re.search(r"function targetLogPanelMarkup\(jobId, targetId\) \{.*?\n    \}", self.source, re.S),
            re.search(r"contentDiv\.innerHTML = `.*?data-job-detail-summary.*?`;", self.source, re.S),
        ):
            self.assertIsNotNone(markup)
            self.assertIn("data-job-detail-summary", markup.group(0))

    def test_retention_guide_explains_union_prune_and_updates_draft_or_selected_policy_summary(self):
        retention = re.search(r"function renderRetention\(content\) \{.*?\n    \}\n\n    function renderSecrets", self.source, re.S)
        self.assertIsNotNone(retention)
        retention_source = retention.group(0)
        for marker in (
            "retention-layout",
            "retention-guide",
            "Las reglas se combinan como una <strong>unión</strong>",
            "no crean backups ni prometen snapshots",
            "<strong>prune</strong> libera datos sin referencias",
            "data-retention-policy-id",
            "is-selected",
            'addEventListener("input", showDraftSummary)',
            "selectRetentionPolicy",
        ):
            self.assertIn(marker, retention_source)
        for marker in (
            "function retentionPolicySummary(policy)",
            "Conservará los últimos",
            "cubrirá hasta",
            "snapshots disponibles",
            "retentionPolicyFromForm",
            "renderRetentionGuideSummary",
        ):
            self.assertIn(marker, self.source)
        for marker in (
            ".retention-layout",
            "grid-template-columns: minmax(0, 1fr) minmax(260px, 340px)",
            ".retention-guide { position: static; }",
            "@media (max-width: 960px)",
            ".retention-policy-row.is-selected",
        ):
            self.assertIn(marker, self.css)

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

    def test_open_job_logs_use_sse_with_bounded_polling_fallback_for_both_panels(self):
        for marker in (
            "function connectJobEvents(jobId",
            "new EventSource(`/api/v1/jobs/${encodeURIComponent(jobId)}/events`",
            "source.onmessage",
            "JSON.parse(event.data)",
            "source.onerror",
            "source.close()",
            "function mergeJobProjection(job)",
            "data-job-live-state",
            "En vivo",
            "Actualizando por polling",
            "function startTargetJobEvents(jobId)",
            "function startAccordionJobEvents(jobId)",
            "pollJobLogs(jobId)",
            "pollAccordionJobLogs(jobId)",
            "stopTargetJobEvents();",
            "stopAccordionJobEvents();",
            "if (isTerminalJobStatus(job.status))",
        ):
            self.assertIn(marker, self.source)

        target = re.search(r"function startTargetJobEvents\(jobId\) \{.*?\n    \}\n\n    function pollJobLogs", self.source, re.S)
        self.assertIsNotNone(target)
        self.assertIn("renderJobLogs(job)", target.group(0))
        self.assertIn("void refreshAll();", target.group(0))

        accordion = re.search(r"function startAccordionJobEvents\(jobId\) \{.*?\n    \}\n\n    function toggleJobLogAccordion", self.source, re.S)
        self.assertIsNotNone(accordion)
        self.assertIn("renderJobLogsInto(job, container)", accordion.group(0))
        self.assertIn("mergeJobProjection(job)", accordion.group(0))

    def test_job_live_state_uses_accessible_dot_without_visible_status_text(self):
        indicators = re.findall(
            r'<span class="job-live-indicator"[^>]*data-job-live-state[^>]*></span>',
            self.source,
        )
        self.assertEqual(len(indicators), 2)
        for indicator in indicators:
            self.assertIn('data-state="connecting"', indicator)
            self.assertIn('role="status"', indicator)
            self.assertIn('aria-live="polite"', indicator)
            self.assertIn('aria-label="Conectando…"', indicator)
            self.assertIn('title="Conectando…"', indicator)

        live_state = re.search(r"function setJobLiveState\(root, text, stateName\) \{.*?\n    \}", self.source, re.S)
        self.assertIsNotNone(live_state)
        self.assertNotIn("textContent", live_state.group(0))
        for marker in ('status.dataset.state = stateName || "idle"', 'status.setAttribute("aria-label", label)', "status.title = label"):
            self.assertIn(marker, live_state.group(0))
        for marker in (
            ".job-live-indicator",
            '.job-live-indicator[data-state="connecting"]',
            '.job-live-indicator[data-state="fallback"]',
            '.job-live-indicator[data-state="live"]',
            '.job-live-indicator[data-state="terminal"]',
            '.job-live-indicator[data-state="error"]',
            "@keyframes job-live-pulse",
            "prefers-reduced-motion",
            "width: 8px",
            "height: 8px",
        ):
            self.assertIn(marker, self.css)
        self.assertNotIn('data-job-live-state role="status" aria-live="polite">Conectando…</span>', self.source)


    def test_live_file_browser_is_opt_in_read_only_safe_and_bounded(self):
        for marker in ("live_access_enabled", "Habilitar acceso live", "Ver archivos en vivo", "function openLiveBrowser(target)", "/live/entries", "/live/file", "URL.createObjectURL", "Solo lectura", 'role="tree"', 'role="treeitem"', 'source.addEventListener("resync_required"', "browser.reconnects > 4", "escapeHtml(entry.name)", "status.textContent = text", "await resp.json()", "error.code", "error.reason", "function liveBrowserErrorMessage", "function preflightLiveBrowserEvents", "Accept: \"text/event-stream\"", "source_unavailable", "helper_start_failed", "Acceso restringido: este volumen está protegido por permisos del sistema y no se puede leer en modo seguro."):
            self.assertIn(marker, self.source)
        self.assertNotIn("innerHTML = result.b64_content", self.source)
        self.assertNotIn("contenteditable", self.source.lower())
        for marker in (".live-browser-status[data-state=\"connected\"]", ".live-browser-status[data-state=\"resync\"]", ".live-browser-status[data-state=\"restricted\"]", ".live-browser-status[data-state=\"error\"]", ".live-browser-entry:focus-visible"):
            self.assertIn(marker, self.css)

    def test_live_file_browser_entries_are_sequenced_cancellable_and_path_safe(self):
        entries = re.search(r"async function loadLiveBrowserEntries\(.*?\n    \}\n\n    async function preflightLiveBrowserEvents", self.source, re.S)
        self.assertIsNotNone(entries)
        entries_source = entries.group(0)
        for marker in (
            "normalizeLiveBrowserPath(path)",
            "browser.requestedPath = normalizedPath;",
            "browser.entriesRequest?.controller.abort();",
            "new AbortController()",
            "sequence: ++browser.entriesSequence",
            "signal: request.controller.signal",
            "isCurrentLiveBrowserEntriesRequest(browser, request)",
            "browser.path = normalizedPath;",
            "browser.nextCursor = result.next_cursor || null;",
        ):
            self.assertIn(marker, entries_source)

        guard = re.search(r"function isCurrentLiveBrowserEntriesRequest\(.*?\n    \}", self.source, re.S)
        self.assertIsNotNone(guard)
        for marker in ("browser === liveBrowserState", "browser.entriesRequest === request", "browser.entriesSequence === request.sequence", "browser.requestedPath === request.path"):
            self.assertIn(marker, guard.group(0))
        self.assertIn("if (!browser || browser.entriesRequest || !browser.nextCursor) return;", self.source)

    def test_live_file_browser_events_are_coalesced_and_resync_requires_manual_refresh(self):
        refresh = re.search(r"function scheduleLiveBrowserRefresh\(.*?\n    \}\n\n    function isCurrentLiveBrowserEntriesRequest", self.source, re.S)
        self.assertIsNotNone(refresh)
        refresh_source = refresh.group(0)
        for marker in ("browser.refreshPending = true", "if (browser.refreshTimer) return", "if (browser.entriesRequest) return", "browser.requestedPath", "setTimeout", "}, 250);"):
            self.assertIn(marker, refresh_source)

        changed = re.search(r'source.addEventListener\("changed".*?\n      \}\);', self.source, re.S)
        self.assertIsNotNone(changed)
        self.assertIn("scheduleLiveBrowserRefresh(browser);", changed.group(0))
        resync = re.search(r'source.addEventListener\("resync_required".*?\n      \}\);', self.source, re.S)
        self.assertIsNotNone(resync)
        resync_source = resync.group(0)
        self.assertIn("clearLiveBrowserRefresh(browser);", resync_source)
        self.assertIn("browser.degraded = true;", resync_source)
        self.assertNotIn("loadLiveBrowserEntries", resync_source)
        self.assertIn('id="liveBrowserRefresh"', self.source)
        self.assertIn(">Actualizar</button>", self.source)
        self.assertIn("loadLiveBrowserEntries(browser.requestedPath)", self.source)
        self.assertIn("Cambios live no sincronizados; la lista se mantiene estable. Actualiza manualmente.", self.source)

    def test_live_file_browser_cleanup_aborts_entries_and_clears_refresh(self):
        close = re.search(r"function closeLiveBrowser\(\) \{.*?\n    \}", self.source, re.S)
        self.assertIsNotNone(close)
        close_source = close.group(0)
        for marker in ("browser.closed = true", "clearLiveBrowserRefresh(browser);", "browser.entriesRequest?.controller.abort();", "browser.entriesRequest = null;", "browser.entriesSequence += 1", "liveBrowserState = null;"):
            self.assertIn(marker, close_source)
        cleanup = re.search(r"function clearLiveBrowserRefresh\(.*?\n    \}", self.source, re.S)
        self.assertIsNotNone(cleanup)
        self.assertIn("clearTimeout(browser.refreshTimer)", cleanup.group(0))
        self.assertIn("browser.refreshPending = false", cleanup.group(0))


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


class WorkerUiStateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = UI_PATH.read_text(encoding="utf-8")
        cls.css = UI_CSS_PATH.read_text(encoding="utf-8")

    def test_worker_labels_use_key_value_rows_with_validation_and_clear_action(self):
        for marker in (
            "renderWorkerLabelRows",
            "data-label-key",
            "data-label-value",
            "data-add-label",
            "data-remove-label",
            "data-clear-labels",
            "repetida",
            "La clave de cada label es obligatoria",
            "aria-invalid",
            "role=\"dialog\"",
            "aria-modal=\"true\"",
        ):
            self.assertIn(marker, self.source)
        self.assertNotIn("editLabelsTextarea", self.source)
        self.assertNotIn("JSON invalido:", self.source)

    def test_worker_labels_are_sorted_and_polling_detects_heartbeat_label_changes(self):
        self.assertIn('Object.keys(labels).sort((a, b) => a.localeCompare(b, "es"))', self.source)
        self.assertIn("function workerFingerprint(worker)", self.source)
        self.assertIn("worker.labels[key]", self.source)
        self.assertIn("newWorkers.map(workerFingerprint)", self.source)

    def test_enrollment_compose_contains_shared_tmp_mount(self):
        compose = re.search(r"const composeYml = `.*?`;", self.source, re.S)
        self.assertIsNotNone(compose)
        compose_source = compose.group(0)
        self.assertIn("- /var/run/docker.sock:/var/run/docker.sock", compose_source)
        self.assertIn("- /tmp:/tmp", compose_source)
        self.assertIn("- worker_state:/data", compose_source)
        self.assertIn("    volumes:\n      - /var/run/docker.sock:/var/run/docker.sock\n      - /tmp:/tmp\n      - worker_state:/data", compose_source)

    def test_worker_admin_actions_are_confirmed_and_have_refresh_feedback(self):
        for marker in (
            "data-revoke-worker",
            "data-delete-worker",
            "window.confirm",
            "/api/v1/admin/workers/${encodeURIComponent(workerId)}/revoke",
            "apiDelete(`/api/v1/workers/${encodeURIComponent(workerId)}`)",
            "workersNotice",
            "await refreshAll();",
            "Worker revocado",
            "worker-disabled-row",
        ):
            self.assertIn(marker, self.source)
        self.assertIn(".worker-notice", self.css)
        self.assertIn(".pill.danger", self.css)

    def test_worker_enrollment_renewal_preserves_stable_id_and_reuses_compose_flow(self):
        for marker in (
            "data-renew-enrollment",
            "Renovar enrollment",
            "/api/v1/admin/workers/${encodeURIComponent(workerId)}/enrollment",
            "worker_id",
            "readonly",
            "TTL (minutos)",
            "Si el worker ya existe",
            "Este enrollment conserva el mismo",
            "El token pendiente anterior se invalida",
            "- /tmp:/tmp",
        ):
            self.assertIn(marker, self.source)
        workers = re.search(r"function renderWorkers\(content\) \{.*?\n    let _editLabelsWorkerId", self.source, re.S)
        self.assertIsNotNone(workers)
        self.assertIn("openRenewWorkerModal(btn.dataset.renewEnrollment)", workers.group(0))

    def test_settings_exposes_snapshot_listing_limit_in_mib_and_wires_bytes_patch(self):
        settings = re.search(
            r"function renderSettings\(content\) \{.*?\n    \}\n\n    async function refreshAll",
            self.source,
            re.S,
        )
        self.assertIsNotNone(settings)
        settings_source = settings.group(0)
        for marker in (
            "snapshot_explorer_listing_max_output_bytes",
            "snapshotListingMaxMiB",
            "Number(settings.snapshot_explorer_listing_max_output_bytes",
            'id="settingsSnapshotListingMaxOutput"',
            'type="number" min="1" max="16" step="1"',
            "restic ls --json",
            "No cambia el límite de 8 MiB para descargar archivos",
            'role="alert"',
            "listingLimitMiB * 1024 * 1024",
            'apiPatch("/api/v1/settings"',
            "El límite del listado debe ser un número entero entre 1 y 16 MiB.",
        ):
            self.assertIn(marker, settings_source)

    def test_restore_ownership_ui_uses_simple_spanish_choices_and_preserves_payload_contract(self):
        restore = re.search(r"function openRestoreFromSnapshotModal\(.*?\n    \}", self.source, re.S)
        self.assertIsNotNone(restore)
        restore_source = restore.group(0)
        self.assertIn('spec.stable_key || spec.volume_key || ""', self.source)
        for marker in (
            'getRestoreOwnershipVolumeCandidates(state.target)',
            'name="restore_overwrite"',
            'name="restore_mode"',
            'Conservar archivos existentes',
            'Reemplazar archivos existentes',
            'Detener servicios antes de restaurar',
            'Mantener servicios activos',
            'Conservar permisos del backup',
            'Aplicar permisos manuales',
            'data-restore-mapping',
            'id="rsOwnershipAdvanced"',
            'Configuración avanzada',
            'id="rsOwnershipStatus"',
            'aria-live="polite"',
            'id="rsOwnershipConfirm"',
            'Confirmo que revisé las opciones y acepto reemplazar los datos del target.',
            'Acción destructiva:',
            'id="rsConfirm" aria-describedby="rsOwnershipStatus rsRestoreWarning" disabled',
            'const syncRestoreButton = policy =>',
            'const ready = Boolean(policy) && Boolean(confirmation?.checked);',
            'restoreButton.disabled = !ready;',
            'if (confirmation) confirmation.addEventListener("change", preview);',
            'const firstInvalid = overlay.querySelector(\'[aria-invalid="true"]\');',
            'Para continuar, valida la configuración y confirma que aceptas reemplazar los datos.',
            'La configuración es válida, pero debes confirmar que aceptas reemplazar los datos para continuar.',
            'Corrige la sintaxis del JSON y vuelve a validar la configuración.',
            'restore_ownership: policy',
            'policy.confirmation = "confirmed"',
            'force_overwrite: forceOverwrite',
            'stop_containers: stopContainers',
            'if (policy.mode === "map" && values.length === 1) request.chown = values[0];',
            'Los volúmenes restaurados se detectaron automáticamente.',
            'Ruta restaurada:',
            'data-mapping-key="${escapeHtml(candidate.key)}"',
            'data-mapping-label="${escapeHtml(candidate.name)}"',
            'placeholder="1000:1000…"',
            'aria-describedby="rsMappingHint${index} rsMappingError${index}"',
            'class="restore-mapping-hint"',
            'Formato: UID:GID',
            'Este target no expone claves estables de volúmenes.',
            'no generaremos claves a partir de rutas del host',
        ):
            self.assertIn(marker, restore_source)
        self.assertIn('data-restore-mapping', restore_source)
        self.assertNotIn('class="restore-mapping-key"', restore_source)
        manual = re.search(r'<div id="rsOwnershipManual".*?</div>`\n        :', restore_source, re.S)
        self.assertIsNotNone(manual)
        self.assertNotIn('<code', manual.group(0))
        for marker in (
            'Restore ownership request JSON',
            'Use preserve or explicit numeric UID:GID mappings',
            'Validated request; confirmation required',
            'I confirm this preserve or numeric UID:GID request',
            'Decline',
            'Preview request',
            'Confirm restore',
            'Cold restore',
            'Hot restore',
        ):
            self.assertNotIn(marker, restore_source)
        self.assertIn('"schema_version":1,"mode":"preserve","mappings":{}', restore_source)
        self.assertIn('compose:proyecto:volumen', restore_source)
        self.assertIn('UID:GID', restore_source)
        self.assertIn('renderRestoreOwnershipEvidence(job)', self.source)

    def test_restore_ownership_candidate_helper_creates_one_mapping_row_per_stable_volume(self):
        helper = re.search(
            r"(function getRestoreOwnershipVolumeCandidates\(target\) \{.*?\n    \})\n\n    function openRestoreFromSnapshotModal",
            self.source,
            re.S,
        )
        self.assertIsNotNone(helper)
        restore = re.search(r"function openRestoreFromSnapshotModal\(.*?\n    \}", self.source, re.S)
        self.assertIsNotNone(restore)
        restore_source = restore.group(0)
        self.assertIn('volumeCandidates.map((candidate, index) => `<div class="restore-mapping-row">', restore_source)
        self.assertIn('data-restore-mapping', restore_source)

        if not shutil.which("node"):
            self.skipTest("Node.js is not installed")
        script = helper.group(1) + r'''
const target = {
  volume_targets: ["/restore/one", "/restore/two"],
  runtime_volumes: {
    first_source: { bind: "/restore/one", name: "first", stable_key: "compose:project:first" },
    second_source: { bind: "/restore/two", name: "second", stable_key: "compose:project:second" },
    ignored_source: { bind: "/ignored", name: "ignored", stable_key: "compose:project:ignored" },
  },
};
const candidates = getRestoreOwnershipVolumeCandidates(target);
const rows = candidates.map((candidate, index) => ({ id: `rsMapping${index}`, key: candidate.key }));
process.stdout.write(JSON.stringify({ candidates, rows }));
'''
        result = subprocess.run(
            ["node", "--input-type=commonjs", "--eval", script],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        output = json.loads(result.stdout)
        self.assertEqual([candidate["key"] for candidate in output["candidates"]], [
            "compose:project:first",
            "compose:project:second",
        ])
        self.assertEqual(len(output["rows"]), 2)
        self.assertEqual([row["id"] for row in output["rows"]], ["rsMapping0", "rsMapping1"])

    def test_restore_snapshot_modal_keeps_scrollable_body_and_usable_footer_focus(self):
        styles_start = self.css.index(".restore-snapshot-modal {")
        styles_end = self.css.index("@media (max-width: 560px)", styles_start)
        restore_styles = self.css[styles_start:styles_end]
        for marker in (
            '.restore-snapshot-modal .modal-body { min-height: 0; overflow-y: auto;',
            '.restore-snapshot-modal .modal-footer { flex-shrink: 0;',
            '.restore-snapshot-modal :is(input, select, textarea, button):focus-visible',
            '.restore-snapshot-confirmation { display: flex;',
            '.restore-choice:focus-within',
            '.restore-mapping-row {',
            'grid-template-columns: minmax(0, 1fr) minmax(170px, .45fr);',
            '.restore-mapping-input input {',
            'min-height: 40px;',
            'font: 600 14px/1.2 var(--font-mono);',
            '.restore-mapping-input input:hover',
            '.restore-mapping-input input:focus-visible',
            '.restore-mapping-input input[aria-invalid="true"]',
            '.restore-mapping-hint {',
        ):
            self.assertIn(marker, self.css)
        self.assertNotIn('transition: all', restore_styles)

    def test_volume_selector_has_keyboard_focus_and_visible_inventory_warning_styles(self):
        for marker in (
            ".volume-group-header:focus-visible",
            ".volume-card:focus-within",
            ".volume-card .volume-checkbox",
            ".volume-inline-warning",
            ".volume-selection-count",
        ):
            self.assertIn(marker, self.css)

    def test_edit_target_modal_uses_responsive_two_column_layout_and_keeps_create_scoped(self):
        for marker in (
            '<div class="modal edit-target-modal" role="dialog"',
            'id="editTargetForm" class="edit-target-form"',
            'class="edit-target-layout"',
            'class="edit-target-settings"',
            'class="edit-target-volume-panel"',
            'class="modal-footer edit-target-footer"',
            'form="editTargetForm"',
        ):
            self.assertIn(marker, self.source)

        modal = re.search(r"\.edit-target-modal \{.*?\n\}", self.css, re.S)
        self.assertIsNotNone(modal)
        modal_source = modal.group(0)
        for marker in (
            "width: min(1080px, calc(100vw - 32px));",
            "max-width: 1080px;",
            "max-height: min(900px, calc(100vh - 32px));",
            "overflow: hidden;",
        ):
            self.assertIn(marker, modal_source)

        layout = re.search(r"\.edit-target-layout \{.*?\n\}", self.css, re.S)
        self.assertIsNotNone(layout)
        layout_source = layout.group(0)
        for marker in (
            "display: grid;",
            "grid-template-columns: minmax(0, 1.1fr) minmax(340px, .9fr);",
            "min-width: 0;",
        ):
            self.assertIn(marker, layout_source)
        self.assertRegex(self.css, r"(?s)@media \(max-width: 900px\) \{.*?\.edit-target-layout \{ grid-template-columns: 1fr;")

        for marker in (
            ".edit-target-modal .modal-body",
            "overflow-y: auto;",
            ".edit-target-modal .modal-footer",
            "position: sticky;",
            ".edit-target-volume-panel .volume-list",
            "overflow-x: hidden;",
        ):
            self.assertIn(marker, self.css)

    def test_volume_selector_uses_one_responsive_scroll_container(self):
        for marker in (
            'id="targetVolumeList" class="volume-list"',
            'id="editTargetVolumeList" class="volume-list"',
        ):
            self.assertIn(marker, self.source)

        volume_list = re.search(r"^\.volume-list \{.*?\n\}", self.css, re.M | re.S)
        self.assertIsNotNone(volume_list)
        volume_list_source = volume_list.group(0)
        for marker in (
            "height: clamp(240px, 42vh, 420px);",
            "min-height: 0;",
            "max-height: none;",
            "overflow-y: auto;",
            "scrollbar-gutter: stable;",
            "overscroll-behavior: contain;",
        ):
            self.assertIn(marker, volume_list_source)

        volume_group = re.search(r"\.volume-group \{.*?\n\}", self.css, re.S)
        self.assertIsNotNone(volume_group)
        self.assertIn("flex: 0 0 auto;", volume_group.group(0))

        volume_group_body = re.search(r"\.volume-group-body \{.*?\n\}", self.css, re.S)
        self.assertIsNotNone(volume_group_body)
        volume_group_body_source = volume_group_body.group(0)
        self.assertIn("grid-template-columns: repeat(auto-fill, minmax(260px, 1fr));", volume_group_body_source)
        self.assertIn("gap: 8px;", volume_group_body_source)
        self.assertIn("max-height: none;", volume_group_body_source)
        self.assertIn("overflow: visible;", volume_group_body_source)
        self.assertNotIn("overflow-y", volume_group_body_source)
        self.assertRegex(
            self.css,
            r"@media \(max-width: 640px\) \{\s*\.volume-group-body \{ grid-template-columns: 1fr; \}\s*\}",
        )


if __name__ == "__main__":
    unittest.main()
