import logging
import math
import os
import re
import time
from typing import Any, Callable, Dict, Iterable

from src.worker_agent.application.policies.runtime_command_policy import RuntimeCommandPolicy
from src.worker_agent.application.ports.runtime_port import RuntimePort

try:  # pragma: no cover - the unit tests inject fake APIs
    from kubernetes import client as kubernetes_client
    from kubernetes import config as kubernetes_config
except ModuleNotFoundError:
    kubernetes_client = None
    kubernetes_config = None

logger = logging.getLogger(__name__)


class KubernetesRuntimeAdapter(RuntimePort):
    runtime_kind = "kubernetes"
    MANAGED_BY_LABEL = "app.kubernetes.io/managed-by"
    MANAGED_BY_VALUE = "docker-volume-backup"
    WORKER_LABEL = "docker-volume-backup/worker-id"
    JOB_LABEL = "docker-volume-backup/job-id"
    TARGET_LABEL = "docker-volume-backup/target-id"
    RUNTIME_LABEL = "docker-volume-backup/runtime-kind"
    DEFAULT_RUNTIME_TIMEOUT_SECONDS = 1800.0
    MAX_RUNTIME_TIMEOUT_SECONDS = 24 * 60 * 60
    DEFAULT_POLL_INTERVAL_SECONDS = 1.0
    MAX_LOG_BYTES = 4 * 1024 * 1024
    MAX_DIAGNOSTICS = 20
    _NAME = re.compile(r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$")
    _SECRET_KEY = re.compile(r"^[A-Za-z0-9._-]+$")
    _ENV = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
    _LABEL = re.compile(r"^[A-Za-z0-9._-]{1,63}$")
    _SECRET_MARKERS = (
        "PASSWORD", "SECRET", "TOKEN", "PRIVATE_KEY", "ACCESS_KEY",
        "CREDENTIAL", "RCLONE_CONF", "RESTIC_REPOSITORY", "PLAINTEXT",
    )

    def __init__(
        self,
        namespace: str | None = None,
        worker_id: str | None = None,
        *,
        core_api: Any = None,
        apps_api: Any = None,
        batch_api: Any = None,
        config_loader: Callable[[], None] | None = None,
        timeout_seconds: float | None = None,
        poll_interval_seconds: float | None = None,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ):
        self.namespace = namespace or os.environ.get("WORKER_KUBERNETES_NAMESPACE")
        self.worker_id = worker_id or os.environ.get("WORKER_ID") or "unknown"
        self.timeout_seconds = self._timeout(
            timeout_seconds if timeout_seconds is not None else os.environ.get(
                "WORKER_RUNTIME_TIMEOUT_SECONDS", self.DEFAULT_RUNTIME_TIMEOUT_SECONDS
            )
        )
        self.poll_interval_seconds = self._poll_interval(
            poll_interval_seconds if poll_interval_seconds is not None else os.environ.get(
                "WORKER_KUBERNETES_POLL_INTERVAL_SECONDS", self.DEFAULT_POLL_INTERVAL_SECONDS
            )
        )
        self._sleep, self._monotonic = sleep, monotonic
        self._client_error = ""
        self.core_api, self.apps_api, self.batch_api = core_api, apps_api, batch_api
        if any(api is None for api in (core_api, apps_api, batch_api)):
            self._configure_in_cluster(config_loader)

    @classmethod
    def _timeout(cls, value: Any) -> float:
        if isinstance(value, bool):
            raise ValueError("Kubernetes runtime timeout_seconds must be a finite positive number")
        try:
            value = float(value)
        except (TypeError, ValueError):
            raise ValueError("Kubernetes runtime timeout_seconds must be a finite positive number") from None
        if not math.isfinite(value) or value <= 0 or value > cls.MAX_RUNTIME_TIMEOUT_SECONDS:
            raise ValueError("Kubernetes runtime timeout_seconds is outside the permitted bounds")
        return value

    @staticmethod
    def _poll_interval(value: Any) -> float:
        if isinstance(value, bool):
            raise ValueError("Kubernetes poll interval must be a finite positive number")
        try:
            value = float(value)
        except (TypeError, ValueError):
            raise ValueError("Kubernetes poll interval must be a finite positive number") from None
        if not math.isfinite(value) or value <= 0 or value > 60:
            raise ValueError("Kubernetes poll interval is outside the permitted bounds")
        return value

    def _configure_in_cluster(self, loader: Callable[[], None] | None) -> None:
        if kubernetes_client is None or kubernetes_config is None:
            self._client_error = "Kubernetes Python client is not installed"
            return
        try:
            (loader or kubernetes_config.load_incluster_config)()
            self.core_api = self.core_api or kubernetes_client.CoreV1Api()
            self.apps_api = self.apps_api or kubernetes_client.AppsV1Api()
            self.batch_api = self.batch_api or kubernetes_client.BatchV1Api()
        except Exception as exc:
            self._client_error = self._error("in-cluster Kubernetes configuration failed", exc)
            self.core_api = self.apps_api = self.batch_api = None

    @staticmethod
    def _get(value: Any, *path: str, default: Any = None) -> Any:
        for part in path:
            value = value.get(part) if isinstance(value, dict) else getattr(value, part, None)
            if value is None:
                return default
        return value

    @classmethod
    def _items(cls, response: Any) -> list[Any]:
        items = cls._get(response, "items", default=[])
        return list(items or []) if isinstance(items, (list, tuple)) else []

    @staticmethod
    def _error(prefix: str, error: Any) -> str:
        text = " ".join(str(error or "").split())
        text = re.sub(
            r"(?i)\b(?:authorization|bearer|token|secret|password|credential)\b\s*[:=]\s*[^\s,;]+",
            "<redacted>", text,
        )
        text = re.sub(r"https?://[^\s]+", "<url>", text)[:512]
        return f"{prefix}: {text}" if text else prefix

    @classmethod
    def _name(cls, value: Any, kind: str) -> str:
        if not isinstance(value, str) or not value or len(value) > 253 or not cls._NAME.fullmatch(value):
            raise ValueError(f"Kubernetes target {kind} is invalid")
        return value

    @classmethod
    def _pvcs(cls, value: Any) -> list[str]:
        if not isinstance(value, list) or not value:
            raise ValueError("Kubernetes target must specify one or more explicit PVC names")
        result = []
        for item in value:
            item = cls._name(item, "PVC name")
            if item in result:
                raise ValueError(f"duplicate Kubernetes PVC name: {item}")
            result.append(item)
        return result

    def _check_scope_inventory(self, namespace: str, pvc_names: list[str]) -> None:
        if self.core_api is None:
            raise RuntimeError(self._client_error or "Kubernetes client is unavailable")
        try:
            namespaces = {
                self._get(item, "metadata", "name")
                for item in self._items(self.core_api.list_namespace())
            }
        except Exception as exc:
            raise RuntimeError(self._error("Kubernetes namespace inventory failed", exc)) from None
        if namespace not in namespaces:
            raise ValueError(f"Kubernetes namespace '{namespace}' is not present or permitted")
        try:
            available = {
                self._get(item, "metadata", "name")
                for item in self._items(self.core_api.list_namespaced_persistent_volume_claim(namespace))
            }
        except Exception as exc:
            raise RuntimeError(self._error("Kubernetes PVC inventory failed", exc)) from None
        missing = [name for name in pvc_names if name not in available]
        if missing:
            raise ValueError("Kubernetes PVC(s) are not present or permitted: " + ", ".join(missing))

    def _validate_scope(self, payload: Dict[str, Any]) -> tuple[str, list[str]]:
        RuntimeCommandPolicy.validate_target_scope(payload)
        runtime = payload.get("runtime_type") or payload.get("runtime")
        if runtime is not None and str(runtime).strip().lower() not in {"kubernetes", "k8s"}:
            raise ValueError("Kubernetes runtime received a non-Kubernetes target")
        namespace = self._name(payload.get("namespace") or self.namespace, "namespace")
        if self.namespace and namespace != self.namespace:
            raise ValueError(f"Kubernetes namespace '{namespace}' is outside the worker namespace scope")
        pvc_names = self._pvcs(payload.get("pvc_names"))
        if payload.get("volume_targets"):
            raise ValueError("Kubernetes targets must use explicit pvc_names")
        self._check_scope_inventory(namespace, pvc_names)
        return namespace, pvc_names

    @classmethod
    def _workload_pvcs(cls, workload: Any) -> set[str]:
        names = set()
        for volume in cls._get(workload, "spec", "template", "spec", "volumes", default=[]) or []:
            name = cls._get(volume, "persistent_volume_claim", "claim_name")
            if name:
                names.add(str(name))
        for template in cls._get(workload, "spec", "volume_claim_templates", default=[]) or []:
            name = cls._get(template, "metadata", "name")
            if name:
                names.add(str(name))
        return names

    def _matching_workloads(self, namespace: str, pvc_names: Iterable[str]) -> list[tuple[str, Any]]:
        if self.apps_api is None:
            raise RuntimeError(self._client_error or "Kubernetes Apps API is unavailable")
        try:
            deployments = self._items(self.apps_api.list_namespaced_deployment(namespace))
            statefulsets = self._items(self.apps_api.list_namespaced_stateful_set(namespace))
        except Exception as exc:
            raise RuntimeError(self._error("Kubernetes workload inventory failed", exc)) from None
        selected = set(pvc_names)
        return [
            (kind, item)
            for kind, items in (("deployment", deployments), ("statefulset", statefulsets))
            for item in items
            if self._workload_pvcs(item) & selected
        ]

    @staticmethod
    def _patch_method(kind: str) -> str:
        return {"deployment": "patch_namespaced_deployment", "statefulset": "patch_namespaced_stateful_set"}[kind]

    def _resume(self, changed: list[dict[str, Any]]) -> list[str]:
        errors = []
        for item in reversed(changed):
            try:
                getattr(self.apps_api, self._patch_method(item["kind"]))(
                    item["name"], item["namespace"], {"spec": {"replicas": item["replicas"]}}
                )
            except Exception as exc:
                errors.append(self._error(f"{item['kind']} {item['name']} resume failed", exc))
        return errors

    def _quiesce(self, namespace: str, pvc_names: list[str]) -> list[dict[str, Any]]:
        changed = []
        try:
            for kind, workload in self._matching_workloads(namespace, pvc_names):
                item = {
                    "kind": kind,
                    "name": self._get(workload, "metadata", "name"),
                    "namespace": namespace,
                    "replicas": self._get(workload, "spec", "replicas", default=1),
                }
                getattr(self.apps_api, self._patch_method(kind))(
                    item["name"], namespace, {"spec": {"replicas": 0}}
                )
                changed.append(item)
        except Exception as exc:
            rollback = self._resume(changed)
            detail = self._error("Kubernetes workload quiesce failed", exc)
            if rollback:
                detail += "; rollback errors: " + ", ".join(rollback)
            raise RuntimeError(detail) from None
        return changed

    @classmethod
    def _secret_key(cls, value: Any, default: str) -> tuple[str, str]:
        if not isinstance(value, dict):
            raise ValueError("Kubernetes secret references must be objects")
        name, key = value.get("name") or value.get("secret_name"), value.get("key") or default
        if not isinstance(name, str) or len(name) > 253 or not cls._NAME.fullmatch(name):
            raise ValueError("Kubernetes Secret name is invalid")
        if not isinstance(key, str) or not cls._SECRET_KEY.fullmatch(key):
            raise ValueError("Kubernetes Secret key is invalid")
        return name, key

    @classmethod
    def _secret_refs(cls, payload: Dict[str, Any]) -> dict[str, tuple[str, str]]:
        raw = payload.get("secret_refs") or payload.get("kubernetes_secret_refs") or {}
        if isinstance(raw, dict):
            entries = raw.items()
        elif isinstance(raw, list):
            entries = ((item.get("env"), item) for item in raw if isinstance(item, dict))
        else:
            raise ValueError("Kubernetes secret_refs must be an object or list")
        result = {}
        for env, reference in entries:
            if not isinstance(env, str) or not cls._ENV.fullmatch(env):
                raise ValueError("Kubernetes Secret environment name is invalid")
            result[env] = cls._secret_key(reference, env.lower())
        return result

    @classmethod
    def _secret_files(cls, payload: Dict[str, Any]) -> list[dict[str, str]]:
        raw = payload.get("secret_files")
        if raw is None:
            raw = payload.get("resolved_files") or []
        if not isinstance(raw, list):
            raise ValueError("Kubernetes secret_files must be a list")
        result = []
        for item in raw:
            if not isinstance(item, dict):
                raise ValueError("Kubernetes secret file references must be objects")
            if (item.get("content") is not None or item.get("plaintext") is not None) and not (item.get("name") or item.get("secret_name")):
                raise ValueError("inline Secret values must be replaced with Kubernetes Secret references")
            path = item.get("mount_path") or item.get("container_path")
            if not isinstance(path, str) or not path.startswith("/") or ".." in path.split("/"):
                raise ValueError("Kubernetes Secret mount path is invalid")
            name, key = cls._secret_key(item, os.path.basename(path))
            result.append({"mount_path": path, "name": name, "key": key})
        return result

    @classmethod
    def _secret_name(cls, key: str) -> bool:
        return any(marker in key.upper() for marker in cls._SECRET_MARKERS)

    @classmethod
    def _redact(cls, value: Any, secrets: Iterable[str] = ()) -> str:
        text = value.decode("utf-8", errors="replace") if isinstance(value, bytes) else str(value or "")
        for secret in sorted((item for item in secrets if item), key=len, reverse=True):
            text = text.replace(secret, "<redacted>")
        text = re.sub(
            r"(?i)\b(?:password|secret|token|credential|private[_-]?key)\b\s*[:=]\s*[^\s,;]+",
            "<redacted>", text,
        )
        return text[: cls.MAX_LOG_BYTES]

    def _environment(self, payload: Dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
        refs = self._secret_refs(payload)
        raw = payload.get("environment") if isinstance(payload.get("environment"), dict) else {}
        env = []
        for key, value in raw.items():
            key = str(key)
            if not self._ENV.fullmatch(key):
                raise ValueError("runtime environment name is invalid")
            if self._secret_name(key):
                if key not in refs:
                    raise ValueError(f"Secret environment '{key}' requires a Kubernetes Secret reference")
                continue
            if isinstance(value, (str, int, float)) and not isinstance(value, bool):
                env.append({"name": key, "value": str(value)})
        env.extend({"name": key, "valueFrom": {"secretKeyRef": {"name": name, "key": secret_key}}} for key, (name, secret_key) in refs.items())

        volumes, mounts = [], []
        for index, item in enumerate(self._secret_files(payload)):
            volume = f"runtime-secret-{index}"
            volumes.append({"name": volume, "secret": {"secretName": item["name"], "items": [{"key": item["key"], "path": item["key"]}], "defaultMode": 0o400}})
            mounts.append({"name": volume, "mountPath": item["mount_path"], "subPath": item["key"], "readOnly": True})
        return env, volumes, mounts

    @classmethod
    def _job_name(cls, payload: Dict[str, Any]) -> str:
        raw = str(payload.get("_job_id") or payload.get("job_id") or "runtime")
        safe = re.sub(r"[^a-z0-9-]+", "-", raw.lower()).strip("-") or "runtime"
        return f"docker-volume-backup-{safe[:48]}"

    def _labels(self, payload: Dict[str, Any]) -> dict[str, str]:
        labels = {
            self.MANAGED_BY_LABEL: self.MANAGED_BY_VALUE,
            self.WORKER_LABEL: self._label(self.worker_id),
            self.JOB_LABEL: self._label(payload.get("_job_id") or payload.get("job_id")),
            self.TARGET_LABEL: self._label(payload.get("target_id")),
            self.RUNTIME_LABEL: self.runtime_kind,
        }
        for key, value in (payload.get("labels") or {}).items() if isinstance(payload.get("labels"), dict) else ():
            if isinstance(key, str) and isinstance(value, str) and self._LABEL.fullmatch(value):
                labels[key[:63]] = value
        labels.update({self.MANAGED_BY_LABEL: self.MANAGED_BY_VALUE, self.WORKER_LABEL: self._label(self.worker_id), self.JOB_LABEL: self._label(payload.get("_job_id") or payload.get("job_id")), self.TARGET_LABEL: self._label(payload.get("target_id")), self.RUNTIME_LABEL: self.runtime_kind})
        return labels

    @classmethod
    def _label(cls, value: Any) -> str:
        value = str(value or "")
        return value if cls._LABEL.fullmatch(value) else "unknown"

    def _job_manifest(self, image: str, payload: Dict[str, Any], namespace: str, pvc_names: list[str]) -> dict[str, Any]:
        argv = RuntimeCommandPolicy.validate(payload.get("command"))
        RuntimeCommandPolicy.validate_snapshot_scope(payload, argv)
        argv = RuntimeCommandPolicy.apply_lock_policy(argv, bool(payload.get("no_lock")))
        env, secret_volumes, secret_mounts = self._environment(payload)
        raw_environment = payload.get("environment") if isinstance(payload.get("environment"), dict) else {}
        restore = bool(payload.get("restore_mode")) or str(raw_environment.get("RESTORE_MODE", "")).lower() in {"1", "true", "yes", "on"} or "restore" in argv
        labels = self._labels(payload)
        pvc_volumes = [{"name": f"target-pvc-{i}", "persistentVolumeClaim": {"claimName": name}} for i, name in enumerate(pvc_names)]
        pvc_mounts = [{"name": f"target-pvc-{i}", "mountPath": f"/backup/{name}", "readOnly": not restore} for i, name in enumerate(pvc_names)]
        timeout = int(self._timeout(payload.get("timeout_seconds", self.timeout_seconds)))
        return {
            "apiVersion": "batch/v1", "kind": "Job",
            "metadata": {"name": self._job_name(payload), "namespace": namespace, "labels": labels},
            "spec": {
                "backoffLimit": 0, "activeDeadlineSeconds": max(1, timeout), "ttlSecondsAfterFinished": 3600,
                "template": {"metadata": {"labels": labels}, "spec": {
                    "restartPolicy": "Never", "automountServiceAccountToken": False,
                    "containers": [{"name": "backup-runtime", "image": str(image), "command": argv, "env": env, "volumeMounts": pvc_mounts + secret_mounts}],
                    "volumes": pvc_volumes + secret_volumes,
                }},
            },
        }

    def build_job_manifest(self, image: str, payload: Dict[str, Any]) -> dict[str, Any]:
        namespace, pvc_names = self._validate_scope(payload)
        return self._job_manifest(image, payload, namespace, pvc_names)

    def collect_inventory(self) -> Dict[str, Any]:
        result = {"runtime": self.runtime_kind, "runtime_type": self.runtime_kind, "kubernetes_available": False, "namespaces": []}
        if self.core_api is None or self.apps_api is None:
            result["error"] = self._client_error or "Kubernetes client is unavailable"
            return result
        try:
            for namespace in self._items(self.core_api.list_namespace()):
                name = self._get(namespace, "metadata", "name")
                if not name:
                    continue
                pvcs = self._items(self.core_api.list_namespaced_persistent_volume_claim(name))
                pvc_names = [self._get(item, "metadata", "name") for item in pvcs if self._get(item, "metadata", "name")]
                result["namespaces"].append({
                    "name": name, "pvc_names": pvc_names,
                    "workloads": [{"kind": kind, "name": self._get(item, "metadata", "name"), "replicas": self._get(item, "spec", "replicas", default=0), "pvc_names": sorted(self._workload_pvcs(item))} for kind, item in self._matching_workloads(name, pvc_names)],
                })
            result["kubernetes_available"] = True
        except Exception as exc:
            result["error"] = self._error("Kubernetes inventory failed", exc)
        return result

    def self_check(self) -> Dict[str, Any]:
        inventory = self.collect_inventory()
        result = {"kubernetes_available": inventory.get("kubernetes_available", False), "namespace_count": len(inventory.get("namespaces", [])), "pvc_count": sum(len(item.get("pvc_names", [])) for item in inventory.get("namespaces", []))}
        if inventory.get("error"):
            result["error"] = inventory["error"]
        return result

    def _pod_logs(self, namespace: str, job_name: str, secrets: Iterable[str] = ()) -> str:
        if self.core_api is None:
            return ""
        try:
            pods = self._items(self.core_api.list_namespaced_pod(namespace, label_selector=f"job-name={job_name}"))
        except Exception as exc:
            return self._error("pod log lookup failed", exc)
        logs = []
        for pod in pods[: self.MAX_DIAGNOSTICS]:
            name = self._get(pod, "metadata", "name")
            if not name:
                continue
            try:
                logs.append(str(self.core_api.read_namespaced_pod_log(name, namespace, follow=False, limit_bytes=self.MAX_LOG_BYTES)))
            except Exception as exc:
                logs.append(self._error("pod log read failed", exc))
        return self._redact("\n".join(logs), secrets)

    def _owned(self, manifest: dict[str, Any]) -> bool:
        labels = manifest.get("metadata", {}).get("labels", {})
        return labels.get(self.MANAGED_BY_LABEL) == self.MANAGED_BY_VALUE and labels.get(self.WORKER_LABEL) == self._label(self.worker_id) and labels.get(self.RUNTIME_LABEL) == self.runtime_kind

    def _delete_owned(self, manifest: dict[str, Any]) -> bool:
        if self.batch_api is None or not self._owned(manifest):
            return False
        try:
            self.batch_api.delete_namespaced_job(manifest["metadata"]["name"], manifest["metadata"]["namespace"], body={"propagationPolicy": "Foreground"})
            return True
        except Exception as exc:
            logger.warning("Kubernetes Job deletion failed (error_type=%s)", exc.__class__.__name__)
            return False

    def _poll_job(self, manifest: dict[str, Any], cancel_check: Callable[[], bool] | None, callback: Callable[[str], None] | None, secrets: set[str]) -> Dict[str, Any]:
        namespace, name = manifest["metadata"]["namespace"], manifest["metadata"]["name"]
        deadline, logs = self._monotonic() + manifest["spec"]["activeDeadlineSeconds"], ""
        while True:
            if cancel_check is not None:
                try:
                    canceled = bool(cancel_check())
                except Exception:
                    canceled = False
                if canceled:
                    return {"success": False, "canceled": True, "status_code": 130, "error": "Kubernetes runtime canceled", "logs": logs, "stderr": "", "job_deleted": self._delete_owned(manifest)}
            try:
                response = self.batch_api.read_namespaced_job_status(name, namespace)
                status = self._get(response, "status", default={})
                latest = self._pod_logs(namespace, name, secrets)
                if latest and latest != logs:
                    logs = latest
                    if callback is not None:
                        try:
                            callback(logs)
                        except Exception:
                            pass
                if int(self._get(status, "succeeded", default=0) or 0) > 0:
                    return {"success": True, "status_code": 0, "logs": logs, "stderr": ""}
                if int(self._get(status, "failed", default=0) or 0) > 0:
                    conditions = self._get(status, "conditions", default=[]) or []
                    reason = self._get(conditions[0], "reason", default="Job failed") if conditions else "Job failed"
                    return {"success": False, "status_code": 1, "error": self._redact(reason, secrets), "logs": logs, "stderr": ""}
            except Exception as exc:
                return {"success": False, "status_code": 1, "error": self._error("Kubernetes Job status polling failed", exc), "logs": logs, "stderr": ""}
            if self._monotonic() >= deadline:
                return {"success": False, "status_code": 124, "error": "Kubernetes runtime timed out", "logs": logs, "stderr": "", "job_deleted": self._delete_owned(manifest)}
            self._sleep(min(self.poll_interval_seconds, max(0.01, deadline - self._monotonic())))

    @classmethod
    def _payload_secrets(cls, payload: Dict[str, Any]) -> set[str]:
        environment = payload.get("environment") if isinstance(payload.get("environment"), dict) else {}
        values = {value for key, value in environment.items() if cls._secret_name(str(key)) and isinstance(value, str) and value}
        for item in payload.get("resolved_files") or []:
            if isinstance(item, dict) and isinstance(item.get("content"), str) and item["content"]:
                values.add(item["content"])
        return values

    def run_runtime_job(self, image: str, payload: Dict[str, Any], cancel_check: Callable[[], bool] | None = None, output_callback: Callable[[str], None] | None = None) -> Dict[str, Any]:
        secrets = self._payload_secrets(payload)
        try:
            if cancel_check is not None and cancel_check():
                return {"success": False, "canceled": True, "status_code": 130, "error": "Kubernetes runtime canceled", "logs": "", "stderr": ""}
            namespace, pvc_names = self._validate_scope(payload)
            manifest = self._job_manifest(image, payload, namespace, pvc_names)
            changed = self._quiesce(namespace, pvc_names)
            try:
                if cancel_check is not None and cancel_check():
                    result = {"success": False, "canceled": True, "status_code": 130, "error": "Kubernetes runtime canceled", "logs": "", "stderr": ""}
                else:
                    self.batch_api.create_namespaced_job(namespace, manifest)
                    result = self._poll_job(manifest, cancel_check, output_callback, secrets)
            finally:
                resume_errors = self._resume(changed)
                if resume_errors:
                    result = dict(result)
                    result.update(success=False, status_code=result.get("status_code") or 1, error="; ".join(resume_errors))
            return result
        except Exception as exc:
            return {"success": False, "status_code": 1, "error": self._redact(self._error("Kubernetes runtime failed", exc), secrets), "logs": "", "stderr": ""}

    def run_runtime_job_binary(self, image: str, payload: Dict[str, Any], cancel_check: Callable[[], bool] | None = None) -> Dict[str, Any]:
        result = self.run_runtime_job(image, payload, cancel_check=cancel_check)
        result["stdout_bytes"] = str(result.get("logs") or "").encode("utf-8")
        return result

    def cleanup_orphaned_runtime_jobs(self, recover_callback: Callable[[Any, Dict[str, Any]], str] | None = None) -> Dict[str, Any]:
        summary = {"inspected": 0, "removed": 0, "failed": 0, "skipped": 0, "retained": 0, "removed_ids": [], "retained_ids": [], "diagnostics": []}
        if self.batch_api is None:
            summary["error"] = self._client_error or "Kubernetes client is unavailable"
            return summary
        namespaces = [self.namespace] if self.namespace else []
        if not namespaces:
            try:
                namespaces = [self._get(item, "metadata", "name") for item in self._items(self.core_api.list_namespace())]
            except Exception as exc:
                summary["error"] = self._error("Kubernetes namespace reconciliation failed", exc)
                return summary
        for namespace in filter(None, namespaces):
            try:
                jobs = self._items(self.batch_api.list_namespaced_job(namespace, label_selector=f"{self.MANAGED_BY_LABEL}={self.MANAGED_BY_VALUE}"))
            except Exception as exc:
                summary["error"] = self._error("Kubernetes orphan reconciliation failed", exc)
                continue
            for job in jobs:
                summary["inspected"] += 1
                metadata, labels = self._get(job, "metadata", default={}), self._get(job, "metadata", "labels", default={}) or {}
                name = self._get(metadata, "name", default="unknown")
                if labels.get(self.WORKER_LABEL) != self._label(self.worker_id) or labels.get(self.RUNTIME_LABEL) != self.runtime_kind:
                    summary["skipped"] += 1
                    continue
                status = self._get(job, "status", default={})
                failed, succeeded, active = (int(self._get(status, key, default=0) or 0) for key in ("failed", "succeeded", "active"))
                if active and not failed and not succeeded:
                    summary["skipped"] += 1
                    continue
                reason = "job_failed" if failed else "job_succeeded" if succeeded else "job_orphaned"
                inspection = {"job_id": labels.get(self.JOB_LABEL), "reason": reason, "logs": self._pod_logs(namespace, name)}
                if len(summary["diagnostics"]) < self.MAX_DIAGNOSTICS:
                    summary["diagnostics"].append(inspection)
                action = "remove"
                if recover_callback is not None:
                    try:
                        action = recover_callback(labels.get(self.JOB_LABEL), inspection)
                    except Exception:
                        action = "retain"
                manifest = {"metadata": {"name": name, "namespace": namespace, "labels": labels}}
                if action != "remove" or not self._delete_owned(manifest):
                    summary["retained"] += 1
                    if len(summary["retained_ids"]) < self.MAX_DIAGNOSTICS:
                        summary["retained_ids"].append(name)
                    continue
                summary["removed"] += 1
                if len(summary["removed_ids"]) < self.MAX_DIAGNOSTICS:
                    summary["removed_ids"].append(name)
        return summary
