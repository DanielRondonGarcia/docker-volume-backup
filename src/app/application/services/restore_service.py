import logging
import copy
import os
import stat, errno
import tempfile
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, List, Optional

from src.app.application.ports.ports import ContainerPort, RestoreStrategy, StoragePort
from src.app.domain.models import RestoreCandidate, RestoreConfig, RestoreResult
from src.app.domain.restore_ownership import RestoreOwnershipPolicy, parse_uid_gid, resolve_restore_ownership

logger = logging.getLogger(__name__)

class RestoreService:
    def __init__(self,
                 storage_port: StoragePort,
                 container_port: ContainerPort,
                 restore_strategy: RestoreStrategy,
                 restore_config: RestoreConfig,
                 volume_scopes=None,
                 metadata_inspector=None,
                 capability_probe: Optional[Callable[..., dict[str, Any]]] = None):
        self.storage_port = storage_port
        self.container_port = container_port
        self.restore_strategy = restore_strategy
        self.restore_config = restore_config
        self.volume_scopes = volume_scopes if volume_scopes is not None else getattr(restore_config, "volume_scopes", None)
        self.metadata_inspector = metadata_inspector
        self.capability_probe = capability_probe
        self._restore_policy = RestoreOwnershipPolicy()
        self._metadata = {"state": "not_inspected"}

    def _failure(self, message: str, planned_actions: Optional[List[str]] = None, category: str = "restore_failed", evidence=None) -> RestoreResult:
        logger.error(message)
        result = RestoreResult(
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
        result.category = category
        result.capability = evidence or {}
        result.metadata = self._metadata
        result.policy = self._policy_payload(self._restore_policy)
        result.destructive_state = "none"
        result.partial = False
        return result

    @staticmethod
    def _policy_payload(policy: RestoreOwnershipPolicy) -> dict[str, Any]:
        return {**policy.to_dict(), **({"source": policy.source} if policy.source else {}), **({"default_mapping": policy.default_mapping} if policy.default_mapping else {})}

    def _resolve_policy(self) -> RestoreOwnershipPolicy:
        policy = resolve_restore_ownership(
            request=getattr(self.restore_config, "restore_ownership", None),
            target_defaults=getattr(self.restore_config, "target_defaults", None),
            legacy_chown=self.restore_config.chown,
            volume_scopes=self.volume_scopes,
        ).require_confirmation()
        self._restore_policy = policy
        return policy

    def _inspect_target(self) -> None:
        target = Path(self.restore_config.target_path)
        try:
            target_stat = os.lstat(target)
        except FileNotFoundError:
            return
        if stat.S_ISLNK(target_stat.st_mode) or os.path.abspath(str(target)) == os.path.abspath(os.sep):
            error = ValueError("unsafe restore target path"); error.category = "unsafe_target_path"; raise error

    def _inspect_candidate(self, candidate: RestoreCandidate, policy: RestoreOwnershipPolicy) -> None:
        inspector = self.metadata_inspector
        if inspector is not None:
            evidence = inspector.inspect(candidate.source, self.restore_config, self.volume_scopes, policy)
        else:
            inspect_method = getattr(type(self.restore_strategy), "inspect_metadata", None)
            evidence = inspect_method(self.restore_strategy, candidate.source, self.restore_config, policy, self.volume_scopes) if callable(inspect_method) else None
        if evidence is None:
            self._metadata = {"state": "not_available", "strategy": candidate.strategy}
            return
        self._metadata = evidence
        if not getattr(evidence, "success", True):
            error = ValueError(getattr(evidence, "detail", None) or "restore inspection failed"); error.category = getattr(evidence, "category", "restore_inspection_failed"); raise error

    def _capability_gate(self, policy: RestoreOwnershipPolicy) -> dict[str, Any]:
        if policy.mode != "map" or (not policy.mappings and not policy.default_mapping):
            return {"state": "not_requested", "category": "not_requested", "mount_mode": "preserve", "writable": None}
        config = copy.copy(self.restore_config)
        config.restore_ownership = policy
        if self.volume_scopes is not None:
            config.volume_scopes = self.volume_scopes
        probe = self.capability_probe or getattr(self.restore_strategy, "inspect_ownership_capability", None)
        if not callable(probe):
            target = Path(config.target_path)
            mapping = policy.default_mapping or next(iter(policy.mappings.values()), None)
            try:
                target_stat = os.lstat(target)
                if stat.S_ISLNK(target_stat.st_mode):
                    return {"state": "unknown", "category": "unsafe_target_path", "mount_mode": "unknown", "writable": None, "userns_mode": "unknown"}
                write_dir = target if stat.S_ISDIR(target_stat.st_mode) else target.parent
                if not write_dir.is_dir() or not os.access(write_dir, os.W_OK):
                    return {"state": "readonly", "category": "readonly_target", "mount_mode": "readonly", "writable": False, "userns_mode": "unknown"}
                lchown = getattr(os, "lchown", None)
                if not callable(lchown) or not mapping:
                    return {"state": "unknown", "category": "chown_capability_unknown", "mount_mode": "unknown", "writable": None, "userns_mode": "unknown"}
                descriptor, marker = tempfile.mkstemp(prefix=".restore-ownership-probe-", dir=str(write_dir))
                os.close(descriptor)
                try:
                    lchown(marker, *parse_uid_gid(mapping))
                finally:
                    Path(marker).unlink(missing_ok=True)
                return {"state": "ready", "category": "ok", "mount_mode": "rw", "writable": True, "rootless": None, "userns_mode": "unknown"}
            except PermissionError as exc:
                return {"state": "unknown", "category": "chown_capability_unavailable", "mount_mode": "unknown", "writable": None, "userns_mode": "unknown", "detail": str(exc)}
            except (OSError, ValueError) as exc:
                category = "readonly_target" if getattr(exc, "errno", None) == errno.EROFS else "ownership_capability_unknown"; return {"state": "unknown", "category": category, "mount_mode": "readonly" if category == "readonly_target" else "unknown", "writable": None, "userns_mode": "unknown", "detail": str(exc)}
        try:
            evidence = probe(config, policy)
        except Exception as exc:
            return {"state": "unknown", "category": "ownership_capability_unknown", "mount_mode": "unknown", "writable": None, "detail": str(exc)}
        return evidence if isinstance(evidence, dict) else {"state": "unknown", "category": "ownership_capability_unknown", "mount_mode": "unknown", "writable": None}

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
        logger.info("Finding containers that mount the same volumes as the runtime container")
        containers = self.container_port.find_containers_using_runtime_volumes()
        logger.info(f"Found {len(containers)} container(s) sharing runtime volumes: {containers}")
        if containers:
            return containers
        if self.restore_config.stop_label:
            labels = [self.restore_config.stop_label]
            if self.restore_config.custom_label:
                labels.append(self.restore_config.custom_label)
            logger.info(f"Fallback: finding containers with labels: {labels}")
            containers = self.container_port.get_containers_by_labels(labels)
            logger.info(f"Found {len(containers)} container(s) by labels: {containers}")
            if containers:
                return containers
        logger.info(f"Fallback: finding containers by volume mount: {self.restore_config.target_path}")
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
        if self._restore_policy.mode == "map":
            actions.append("Require confirmed ownership mapping and capability verification before clearing")
        else:
            actions.append("Preserve restored ownership; no inferred runtime mapping will be applied")
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
            policy = self._resolve_policy()
            self._inspect_target()
            self._inspect_candidate(candidate, policy)
            affected_containers = self._find_affected_containers()
            planned_actions = self._planned_actions(candidate, affected_containers)
            result = RestoreResult(
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
            result.category = "ok"
            result.policy = self._policy_payload(policy)
            result.metadata = self._metadata
            result.capability = {"state": "deferred" if policy.mode == "map" else "not_requested"}
            result.destructive_state = "none"
            result.partial = False
            return result
        except Exception as e:
            return self._failure(str(e), category=getattr(e, "category", "restore_failed"))

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

        policy = self._restore_policy
        capability = self._capability_gate(policy)
        if capability.get("state") not in {"ready", "not_requested"}:
            return self._failure(
                f"Restore blocked before clearing: {capability.get('category', 'ownership capability unavailable')}",
                plan.planned_actions,
                category=capability.get("category", "ownership_capability_unknown"),
                evidence=capability,
            )

        started_at = time.time()
        logger.warning("FORCE OVERWRITE enabled: target contents will be replaced before restore")
        candidate = RestoreCandidate(plan.source or "", self.restore_config.backup_strategy)
        stopped_containers: List[str] = []
        downloaded_path: Optional[str] = None
        cleanup_error = None
        restart_error = None
        execution_config = copy.copy(self.restore_config)
        execution_config.restore_ownership = policy
        if self.volume_scopes is not None:
            execution_config.volume_scopes = self.volume_scopes
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

            downloaded_path = self.storage_port.download_restore_candidate(candidate, execution_config)
            result = self.restore_strategy.restore(downloaded_path, execution_config)
        except Exception as e:
            result = self._failure(f"Restore failed: {e}", category=getattr(e, "category", "restore_failed"), evidence=capability)
        finally:
            if downloaded_path and candidate.source.startswith(("s3://", "scp://", "rclone://")):
                try:
                    self.storage_port.cleanup(downloaded_path)
                except Exception as e:
                    cleanup_error = f"Restore download cleanup failed for {downloaded_path}: {e}"
                    logger.error(cleanup_error)
            if stopped_containers:
                logger.info(f"Cold restore: restarting {len(stopped_containers)} container(s): {stopped_containers}")
                try:
                    self.container_port.start_containers(stopped_containers)
                    logger.info("Cold restore: containers restarted")
                except Exception as e:
                    restart_error = f"Cold restore container restart failed for {stopped_containers}: {e}"
                    logger.error(restart_error)

        lifecycle_errors = [error for error in (cleanup_error, restart_error) if error]
        if lifecycle_errors:
            result.success = False
            result.error = "; ".join([error for error in (result.error, *lifecycle_errors) if error])
        result.capability = capability
        result.metadata = self._metadata
        result.policy = self._policy_payload(policy)
        result.restart = {"requested": bool(stopped_containers), "state": "failed" if restart_error else ("succeeded" if stopped_containers else "not_requested"), "detail": restart_error}
        if restart_error:
            result.category = "restart_failed"
        if not hasattr(result, "destructive_state"):
            result.destructive_state = "unknown" if not result.success else "complete"
        result.partial = result.destructive_state in {"partial", "unknown"}
        if not hasattr(result, "normalization"):
            result.normalization = {"state": "unknown" if not result.success else "preserved", "changed": False, "unsupported_metadata": []}
        result.unsupported_metadata = getattr(result, "unsupported_metadata", result.normalization.get("unsupported_metadata", []))

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
