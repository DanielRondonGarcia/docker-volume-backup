import logging
import time
from datetime import datetime
from typing import List, Optional

from src.app.application.ports.ports import ContainerPort, RestoreStrategy, StoragePort
from src.app.domain.models import RestoreCandidate, RestoreConfig, RestoreResult

logger = logging.getLogger(__name__)

class RestoreService:
    def __init__(self,
                 storage_port: StoragePort,
                 container_port: ContainerPort,
                 restore_strategy: RestoreStrategy,
                 restore_config: RestoreConfig):
        self.storage_port = storage_port
        self.container_port = container_port
        self.restore_strategy = restore_strategy
        self.restore_config = restore_config

    def _failure(self, message: str, planned_actions: Optional[List[str]] = None) -> RestoreResult:
        logger.error(message)
        return RestoreResult(
            timestamp=datetime.now(),
            duration=0,
            success=False,
            source=self.restore_config.source,
            target_path=self.restore_config.target_path,
            dry_run=self.restore_config.dry_run,
            force_overwrite=self.restore_config.force_overwrite,
            affected_containers=[],
            planned_actions=planned_actions or [],
            permission_warnings=[],
            error=message
        )

    def _select_candidate(self) -> RestoreCandidate:
        if self.restore_config.source:
            return RestoreCandidate(
                source=self.restore_config.source,
                strategy=self.restore_config.backup_strategy,
                available=True
            )

        candidates = self.storage_port.list_restore_candidates(self.restore_config)
        available = [candidate for candidate in candidates if candidate.available]
        if not available:
            unavailable = [candidate.unavailable_reason for candidate in candidates if candidate.unavailable_reason]
            reason = "; ".join(unavailable) if unavailable else "no available restore candidates found"
            raise ValueError(reason)

        return sorted(available, key=lambda candidate: candidate.created_at or datetime.min, reverse=True)[0]

    def _validate_chown(self) -> Optional[str]:
        chown = self.restore_config.chown
        if not chown:
            return None
        parts = chown.split(":", 1)
        if len(parts) != 2 or not parts[0].isdigit() or not parts[1].isdigit():
            return "RESTORE_CHOWN must be numeric uid:gid, for example 1000:1000"
        return None

    def _validate_layout(self) -> Optional[str]:
        layout = (self.restore_config.layout or "auto").lower()
        if layout not in ("auto", "direct", "backup-dir"):
            return "RESTORE_LAYOUT must be one of: auto, direct, backup-dir"
        return None

    def _find_affected_containers(self) -> List[str]:
        if self.restore_config.stop_label:
            labels = [self.restore_config.stop_label]
            if self.restore_config.custom_label:
                labels.append(self.restore_config.custom_label)
            logger.info(f"Finding containers with labels: {labels}")
            containers = self.container_port.get_containers_by_labels(labels)
            logger.info(f"Found {len(containers)} container(s) by labels: {containers}")
            if containers:
                return containers
        logger.info(f"Finding containers by volume mount: {self.restore_config.target_path}")
        containers = self.container_port.find_containers_using_volume(self.restore_config.target_path)
        logger.info(f"Found {len(containers)} container(s) by volume: {containers}")
        return containers

    def _planned_actions(self, candidate: RestoreCandidate, affected_containers: List[str]) -> List[str]:
        actions = [
            f"Select restore source: {candidate.source}",
            f"Target path: {self.restore_config.target_path}",
            f"Restore layout: {self.restore_config.layout}",
            "Replace target contents before restore (no merge)",
            f"Restore strategy: {candidate.strategy}",
        ]
        if (self.restore_config.layout or "auto") in ("auto", "backup-dir"):
            actions.append("When target points to /backup, match backup subdirectories by name and restore them in place")
        if affected_containers:
            actions.append(f"Affected containers: {', '.join(affected_containers)}")
        else:
            actions.append("Affected containers: none detected")
        if self.restore_config.stop_containers:
            actions.append("Stop affected containers before restore and restart them afterwards")
        if self.restore_config.chown:
            actions.append(f"Apply ownership after restore: {self.restore_config.chown}")
        else:
            actions.append("No RESTORE_CHOWN configured; archive/root ownership may not match the runtime app user")
        return actions

    def plan_restore(self) -> RestoreResult:
        started_at = time.time()
        if not self.restore_config.target_path:
            return self._failure("RESTORE_TARGET_PATH is required before restore can run")
        chown_error = self._validate_chown()
        if chown_error:
            return self._failure(chown_error)
        layout_error = self._validate_layout()
        if layout_error:
            return self._failure(layout_error)

        try:
            candidate = self._select_candidate()
            affected_containers = self._find_affected_containers()
            planned_actions = self._planned_actions(candidate, affected_containers)
            return RestoreResult(
                timestamp=datetime.now(),
                duration=time.time() - started_at,
                success=True,
                source=candidate.source,
                target_path=self.restore_config.target_path,
                dry_run=self.restore_config.dry_run,
                force_overwrite=self.restore_config.force_overwrite,
                affected_containers=affected_containers,
                planned_actions=planned_actions,
            )
        except Exception as e:
            return self._failure(str(e))

    def execute_restore(self) -> RestoreResult:
        plan = self.plan_restore()
        if not plan.success:
            return plan

        for action in plan.planned_actions or []:
            logger.info(action)

        if self.restore_config.dry_run:
            logger.info("Restore dry-run complete; no files, containers, or backup objects were modified")
            return plan

        if not self.restore_config.force_overwrite:
            return self._failure(
                "Actual restore replaces target contents and requires RESTORE_FORCE_OVERWRITE=true. "
                "Run with RESTORE_DRY_RUN=true to preview or set force only after verifying the target.",
                plan.planned_actions
            )

        started_at = time.time()
        logger.warning("FORCE OVERWRITE enabled: target contents will be replaced before restore")
        candidate = RestoreCandidate(plan.source or "", self.restore_config.backup_strategy)
        stopped_containers: List[str] = []
        try:
            if self.restore_config.stop_containers:
                if plan.affected_containers:
                    logger.info(f"Cold restore: stopping {len(plan.affected_containers)} container(s): {plan.affected_containers}")
                    stopped_containers = self.container_port.stop_containers(plan.affected_containers)
                    logger.info(f"Cold restore: {len(stopped_containers)} container(s) stopped successfully")
                else:
                    logger.warning("Cold restore: stop_containers=true but no affected containers found (check labels or volume mounts)")
            else:
                logger.info("Hot restore: containers will not be stopped")

            downloaded_path = self.storage_port.download_restore_candidate(candidate, self.restore_config)
            result = self.restore_strategy.restore(downloaded_path, self.restore_config)
        finally:
            if stopped_containers:
                logger.info(f"Cold restore: restarting {len(stopped_containers)} container(s): {stopped_containers}")
                self.container_port.start_containers(stopped_containers)
                logger.info("Cold restore: containers restarted")

        result.duration = time.time() - started_at
        result.source = plan.source
        result.target_path = self.restore_config.target_path
        result.dry_run = False
        result.force_overwrite = True
        result.affected_containers = plan.affected_containers
        result.planned_actions = (plan.planned_actions or []) + (result.planned_actions or [])
        for action in result.planned_actions or []:
            logger.info(action)
        if result.success:
            logger.info(f"Restore completed successfully in {result.duration:.1f}s")
        else:
            logger.error(f"Restore failed: {result.error}")
        return result
