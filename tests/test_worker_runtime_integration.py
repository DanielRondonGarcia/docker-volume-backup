import json
import os
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from src.worker_agent import main as worker_main
from src.worker_agent.application.services.worker_agent_service import WorkerAgentService
from src.worker_agent.domain.models import WorkerAgentConfig
from src.worker_agent.infrastructure.adapters import kubernetes_runtime


ROOT = Path(__file__).resolve().parents[1]


class WorkerRuntimeSelectionTests(unittest.TestCase):
    def build_service(self, environment, docker_runtime, kubernetes_runtime):
        credential_store = Mock()
        credential_store.load.return_value = None
        with patch.object(worker_main, "WorkerCredentialStore", return_value=credential_store), patch.object(
            worker_main.RedisSnapshotCache, "from_env", return_value=None
        ), patch.dict(os.environ, environment, clear=True):
            return worker_main.build_service(), docker_runtime, kubernetes_runtime

    def test_docker_is_the_default_runtime_and_identity_is_advertised(self):
        docker = Mock(runtime_kind="docker")
        with patch.object(worker_main, "DockerRuntimeAdapter", return_value=docker) as docker_factory, patch.object(
            worker_main, "KubernetesRuntimeAdapter"
        ) as kubernetes_factory:
            service, _, _ = self.build_service(
                {"WORKER_LABELS": '{"lane":"batch"}'}, docker, kubernetes_factory
            )

        docker_factory.assert_called_once_with()
        kubernetes_factory.assert_not_called()
        self.assertIs(service.runtime, docker)
        self.assertEqual(service.config.labels["runtime_kind"], "docker")
        self.assertEqual(json.loads(service.config.labels["capabilities"]), ["docker"])
        self.assertEqual(service.config.labels["lane"], "batch")

    def test_kubernetes_runtime_is_selected_without_allowing_label_spoofing(self):
        kubernetes = Mock(runtime_kind="kubernetes")
        with patch.object(worker_main, "DockerRuntimeAdapter") as docker_factory, patch.object(
            worker_main, "KubernetesRuntimeAdapter", return_value=kubernetes
        ) as kubernetes_factory:
            service, _, _ = self.build_service(
                {
                    "WORKER_RUNTIME": "kubernetes",
                    "WORKER_KUBERNETES_NAMESPACE": "backups",
                    "WORKER_LABELS": json.dumps(
                        {"runtime_kind": "docker", "capabilities": ["docker"], "lane": "batch"}
                    ),
                },
                docker_factory,
                kubernetes,
            )

        docker_factory.assert_not_called()
        kubernetes_factory.assert_called_once_with(namespace="backups", worker_id=None)
        self.assertIs(service.runtime, kubernetes)
        self.assertEqual(service.config.labels["runtime_kind"], "kubernetes")
        self.assertEqual(service.config.labels["runtime_type"], "kubernetes")
        self.assertEqual(service.config.labels["supported_runtimes"], "kubernetes")
        self.assertEqual(json.loads(service.config.labels["capabilities"]), ["kubernetes"])
        self.assertEqual(service.config.labels["lane"], "batch")

    def test_invalid_runtime_fails_safe_to_docker(self):
        docker = Mock(runtime_kind="docker")
        with patch.object(worker_main, "DockerRuntimeAdapter", return_value=docker) as docker_factory, patch.object(
            worker_main, "KubernetesRuntimeAdapter"
        ) as kubernetes_factory, self.assertLogs(worker_main.logger, level="WARNING") as logs:
            service, _, _ = self.build_service({"WORKER_RUNTIME": "shell"}, docker, kubernetes_factory)

        docker_factory.assert_called_once_with()
        kubernetes_factory.assert_not_called()
        self.assertIs(service.runtime, docker)
        self.assertTrue(any("unsupported" in record.getMessage() for record in logs.records))

    def test_runtime_worker_id_is_updated_after_enrollment(self):
        runtime = SimpleNamespace(runtime_kind="kubernetes", worker_id=None)
        client = Mock(credential_store=None)
        client.register_worker.return_value = {"worker_id": "worker-k8s"}
        service = WorkerAgentService(
            WorkerAgentConfig("http://control-plane", "worker", "host"), client, runtime
        )

        self.assertEqual(service.ensure_registered(), "worker-k8s")
        self.assertEqual(runtime.worker_id, "worker-k8s")

    def test_health_snapshot_reports_runtime_kind_and_capabilities(self):
        state = worker_main.WorkerHealthState("http://control-plane", "worker")
        state.set_runtime(True, 0.5, runtime_kind="kubernetes", capabilities=["kubernetes"])

        snapshot = state.snapshot()
        self.assertEqual(snapshot["runtime_kind"], "kubernetes")
        self.assertEqual(snapshot["capabilities"], ["kubernetes"])


class KubernetesDependencyAndReleaseTests(unittest.TestCase):
    def test_adapter_imports_without_the_optional_sdk_and_reports_unavailable(self):
        with patch.object(kubernetes_runtime, "kubernetes_client", None), patch.object(
            kubernetes_runtime, "kubernetes_config", None
        ):
            adapter = kubernetes_runtime.KubernetesRuntimeAdapter(
                namespace="backup", worker_id="worker", sleep=lambda _seconds: None
            )

        check = adapter.self_check()
        self.assertEqual(adapter.runtime_kind, "kubernetes")
        self.assertFalse(check["kubernetes_available"])
        self.assertIn("not installed", check["error"])

    def test_dependency_and_conditional_docker_cli_are_wired(self):
        requirements = (ROOT / "requirements.txt").read_text(encoding="utf-8")
        dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")

        self.assertRegex(requirements, r"(?m)^kubernetes>=29,<35$")
        self.assertIn("ARG INSTALL_DOCKER_CLI=true", dockerfile)
        self.assertIn('if [ "${INSTALL_DOCKER_CLI}" = "true" ]', dockerfile)
        self.assertIn("apt-get install -y --no-install-recommends docker.io", dockerfile)

    def test_release_and_kubernetes_manifests_keep_architecture_and_runtime_wiring(self):
        workflow = (ROOT / ".github" / "workflows" / "release-dispatch.yml").read_text(encoding="utf-8")
        manifest = (ROOT / "deploy" / "worker" / "k8s" / "worker.yaml").read_text(encoding="utf-8")

        self.assertGreaterEqual(workflow.count("platforms: linux/amd64,linux/arm64"), 3)
        self.assertIn("target: backup-runtime", workflow)
        self.assertIn("target: control-plane", workflow)
        self.assertIn("target: worker", workflow)
        self.assertIn("INSTALL_DOCKER_CLI=true", workflow)
        self.assertIn("docker-volume-backup-worker:latest", manifest)
        self.assertIn("name: WORKER_RUNTIME\n              value: kubernetes", manifest)
        self.assertIn("name: BACKUP_RUNTIME_IMAGE", manifest)


if __name__ == "__main__":
    unittest.main()
