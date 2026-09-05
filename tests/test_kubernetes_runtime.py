import json
import unittest
from types import SimpleNamespace

from src.worker_agent.infrastructure.adapters.kubernetes_runtime import KubernetesRuntimeAdapter


def resource(name, **kwargs):
    kwargs.setdefault("metadata", SimpleNamespace(name=name, labels={}))
    return SimpleNamespace(**kwargs)


def workload(name, claim, replicas=1):
    volume = SimpleNamespace(persistent_volume_claim=SimpleNamespace(claim_name=claim))
    pod = SimpleNamespace(volumes=[volume])
    return resource(name, spec=SimpleNamespace(replicas=replicas, template=SimpleNamespace(spec=pod), volume_claim_templates=[]))


class FakeCoreApi:
    def __init__(self, namespaces=None, pvcs=None, pods=None, logs=None, namespace_error=None):
        self.namespaces, self.pvcs, self.pods, self.logs = namespaces or [], pvcs or {}, pods or {}, logs or {}
        self.namespace_error = namespace_error

    def list_namespace(self):
        if self.namespace_error:
            raise self.namespace_error
        return SimpleNamespace(items=self.namespaces)

    def list_namespaced_persistent_volume_claim(self, namespace):
        return SimpleNamespace(items=self.pvcs.get(namespace, []))

    def list_namespaced_pod(self, namespace, label_selector=None):
        return SimpleNamespace(items=self.pods.get(namespace, []))

    def read_namespaced_pod_log(self, name, namespace, **kwargs):
        return self.logs.get((namespace, name), "")


class FakeAppsApi:
    def __init__(self, deployments=None, statefulsets=None, failing_patch_names=None):
        self.deployments, self.statefulsets = deployments or {}, statefulsets or {}
        self.failing_patch_names, self.patches = set(failing_patch_names or ()), []

    def list_namespaced_deployment(self, namespace):
        return SimpleNamespace(items=self.deployments.get(namespace, []))

    def list_namespaced_stateful_set(self, namespace):
        return SimpleNamespace(items=self.statefulsets.get(namespace, []))

    def patch_namespaced_deployment(self, name, namespace, body):
        self.patches.append(("deployment", name, namespace, body))
        if name in self.failing_patch_names:
            raise PermissionError("forbidden deployment patch")

    def patch_namespaced_stateful_set(self, name, namespace, body):
        self.patches.append(("statefulset", name, namespace, body))
        if name in self.failing_patch_names:
            raise PermissionError("forbidden statefulset patch")


class FakeBatchApi:
    def __init__(self, status_sequence=None, jobs=None):
        self.status_sequence, self.jobs, self.created, self.deleted = list(status_sequence or []), list(jobs or []), [], []

    def create_namespaced_job(self, namespace, body):
        self.created.append((namespace, body))
        return resource(body["metadata"]["name"], metadata=SimpleNamespace(**body["metadata"]))

    def read_namespaced_job_status(self, name, namespace):
        status = self.status_sequence.pop(0) if self.status_sequence else SimpleNamespace(active=1)
        return SimpleNamespace(status=status)

    def list_namespaced_job(self, namespace, label_selector=None):
        return SimpleNamespace(items=self.jobs)

    def delete_namespaced_job(self, name, namespace, body=None):
        self.deleted.append((name, namespace, body))


class KubernetesRuntimeAdapterTests(unittest.TestCase):
    def adapter(self, core=None, apps=None, batch=None):
        return KubernetesRuntimeAdapter(
            core_api=core or FakeCoreApi(namespaces=[resource("backup")], pvcs={"backup": [resource("data")]}),
            apps_api=apps or FakeAppsApi(), batch_api=batch or FakeBatchApi(status_sequence=[SimpleNamespace(succeeded=1)]),
            namespace="backup", worker_id="worker-1", sleep=lambda _seconds: None,
        )

    @staticmethod
    def payload(**overrides):
        payload = {"_job_id": "job-1", "runtime_type": "kubernetes", "namespace": "backup", "pvc_names": ["data"], "command": ["/root/backup.sh"]}
        payload.update(overrides)
        return payload

    def test_namespace_rbac_failure_creates_no_data_job(self):
        batch = FakeBatchApi()
        result = self.adapter(core=FakeCoreApi(namespace_error=PermissionError("namespaces forbidden")), batch=batch).run_runtime_job("backup-image:dev", self.payload())
        self.assertFalse(result["success"])
        self.assertIn("namespace", result["error"].lower())
        self.assertEqual(batch.created, [])

    def test_missing_pvc_creates_no_data_job(self):
        batch = FakeBatchApi()
        result = self.adapter(core=FakeCoreApi(namespaces=[resource("backup")], pvcs={"backup": [resource("other")]}), batch=batch).run_runtime_job("backup-image:dev", self.payload())
        self.assertFalse(result["success"])
        self.assertIn("pvc", result["error"].lower())
        self.assertEqual(batch.created, [])

    def test_partial_quiesce_restores_prior_replicas_and_creates_no_job(self):
        batch = FakeBatchApi()
        apps = FakeAppsApi(deployments={"backup": [workload("web", "data", 3)]}, statefulsets={"backup": [workload("db", "data", 2)]}, failing_patch_names={"db"})
        result = self.adapter(apps=apps, batch=batch).run_runtime_job("backup-image:dev", self.payload())
        self.assertFalse(result["success"])
        self.assertIn("quiesce", result["error"].lower())
        self.assertEqual(batch.created, [])
        self.assertEqual([patch[3]["spec"]["replicas"] for patch in apps.patches if patch[1] == "web"], [0, 3])

    def test_timeout_and_cancel_delete_only_owned_jobs(self):
        batch = FakeBatchApi(status_sequence=[SimpleNamespace(active=1)])
        checks = iter([False, False, True])
        result = self.adapter(batch=batch).run_runtime_job("backup-image:dev", self.payload(), cancel_check=lambda: next(checks))
        self.assertFalse(result["success"])
        self.assertTrue(result["canceled"])
        self.assertEqual([item[0] for item in batch.deleted], ["docker-volume-backup-job-1"])
        timeout_batch = FakeBatchApi(status_sequence=[SimpleNamespace(active=1)])
        timeout_adapter = self.adapter(batch=timeout_batch); timeout_adapter.timeout_seconds = 1; clock = iter([0, 2]); timeout_adapter._monotonic = lambda: next(clock)
        timeout_result = timeout_adapter.run_runtime_job("backup-image:dev", self.payload())
        self.assertEqual(timeout_result["status_code"], 124)
        self.assertEqual([item[0] for item in timeout_batch.deleted], ["docker-volume-backup-job-1"])

    def test_orphan_reconciliation_reports_reason_and_available_logs(self):
        labels = {"app.kubernetes.io/managed-by": "docker-volume-backup", "docker-volume-backup/worker-id": "worker-1", "docker-volume-backup/job-id": "job-orphan", "docker-volume-backup/runtime-kind": "kubernetes"}
        job = resource("orphan-job", metadata=SimpleNamespace(name="orphan-job", labels=labels), status=SimpleNamespace(succeeded=0, failed=1, active=0))
        pod = resource("orphan-pod", metadata=SimpleNamespace(name="orphan-pod", labels={"job-name": "orphan-job"}))
        core = FakeCoreApi(namespaces=[resource("backup")], pvcs={"backup": [resource("data")]}, pods={"backup": [pod]}, logs={("backup", "orphan-pod"): "backup failed: permission denied"})
        result = self.adapter(core=core, batch=FakeBatchApi(jobs=[job])).cleanup_orphaned_runtime_jobs()
        self.assertEqual(result["removed"], 1)
        self.assertEqual(result["diagnostics"][0]["reason"], "job_failed")
        self.assertIn("permission denied", result["diagnostics"][0]["logs"])

    def test_serialized_job_manifest_contains_secret_references_but_no_literals(self):
        batch = FakeBatchApi(status_sequence=[SimpleNamespace(succeeded=1)])
        secret_literal = "never-serialize-this-password"
        result = self.adapter(batch=batch).run_runtime_job("backup-image:dev", self.payload(
            environment={"RESTIC_PASSWORD": secret_literal, "BACKUP_STRATEGY": "restic"},
            secret_refs={"RESTIC_PASSWORD": {"name": "backup-credentials", "key": "password"}},
            secret_files=[{"mount_path": "/run/secrets/rclone.conf", "name": "backup-credentials", "key": "rclone.conf"}],
        ))
        self.assertTrue(result["success"])
        serialized = json.dumps(batch.created[0][1], sort_keys=True)
        self.assertNotIn(secret_literal, serialized)
        self.assertIn("secretKeyRef", serialized)
        self.assertIn("backup-credentials", serialized)


if __name__ == "__main__":
    unittest.main()
