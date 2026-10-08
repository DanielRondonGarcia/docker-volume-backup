import json
import re
import unittest
from urllib.error import URLError
from unittest.mock import patch

from src.control_plane.main import _latest_release_payload


OFFICIAL_BASE = "https://github.com/DanielRondonGarcia/docker-volume-backup/releases/download/1.2.3"


class LatestReleaseAssetAPITests(unittest.TestCase):
    def asset(self, name, digest=None, url_base=OFFICIAL_BASE):
        payload = {"name": name, "browser_download_url": f"{url_base}/{name}"}
        if digest is not None:
            payload["digest"] = digest
        return payload

    def test_valid_assets_and_digests_are_allowlisted_by_platform(self):
        digest = "sha256:" + "a" * 64
        data = {
            "tag_name": "1.2.3",
            "html_url": "https://github.com/DanielRondonGarcia/docker-volume-backup/releases/tag/1.2.3",
            "assets": [
                self.asset("vaultline-worker_1.2.3_amd64.deb", digest),
                self.asset("vaultline-worker_1.2.3_arm64.deb", "sha256:" + "b" * 64),
                self.asset("vaultline-worker_1.2.3_windows-amd64.exe", "sha256:" + "c" * 64),
                self.asset("vaultline-worker_1.2.3_SHA256SUMS"),
            ],
        }

        payload = _latest_release_payload(data)

        self.assertEqual(payload["tag_name"], "1.2.3")
        self.assertEqual(payload["html_url"], data["html_url"])
        self.assertEqual(set(payload["assets"]), {"linux-amd64", "linux-arm64", "windows-amd64", "checksums"})
        self.assertEqual(payload["assets"]["linux-amd64"]["name"], "vaultline-worker_1.2.3_amd64.deb")
        self.assertEqual(payload["assets"]["linux-amd64"]["url"], f"{OFFICIAL_BASE}/vaultline-worker_1.2.3_amd64.deb")
        self.assertEqual(payload["assets"]["linux-amd64"]["sha256"], "a" * 64)
        self.assertNotIn("sha256", payload["assets"]["checksums"])

    def test_old_releases_without_assets_return_empty_assets_object(self):
        payload = _latest_release_payload({"tag_name": "1.2.3", "html_url": "https://example.test/release"})

        self.assertEqual(payload["tag_name"], "1.2.3")
        self.assertEqual(payload["assets"], {})

    def test_unknown_filenames_and_external_urls_are_ignored(self):
        data = {
            "tag_name": "1.2.3",
            "html_url": "https://github.com/DanielRondonGarcia/docker-volume-backup/releases/tag/1.2.3",
            "assets": [
                self.asset("vaultline-worker_1.2.3_linux-amd64.tar.gz", "sha256:" + "a" * 64),
                self.asset("vaultline-worker_1.2.3_amd64.deb", "sha256:" + "b" * 64, "https://evil.example/releases/download/1.2.3"),
            ],
        }

        self.assertEqual(_latest_release_payload(data)["assets"], {})

    def test_invalid_semver_and_invalid_digest_are_ignored(self):
        invalid_tag_payload = _latest_release_payload(
            {"tag_name": "v1.2.3-beta", "html_url": "x", "assets": [self.asset("vaultline-worker_v1.2.3-beta_amd64.deb", "sha256:" + "a" * 64)]}
        )
        invalid_digest_payload = _latest_release_payload(
            {"tag_name": "1.2.3", "html_url": "x", "assets": [self.asset("vaultline-worker_1.2.3_amd64.deb", "sha256:not-hex")]}
        )

        self.assertEqual(invalid_tag_payload["assets"], {})
        self.assertEqual(invalid_digest_payload["assets"]["linux-amd64"]["name"], "vaultline-worker_1.2.3_amd64.deb")
        self.assertNotIn("sha256", invalid_digest_payload["assets"]["linux-amd64"])

    def test_failure_response_preserves_error_fields_and_empty_assets(self):
        payload = _latest_release_payload(exception=URLError("offline"))

        self.assertEqual(payload["tag_name"], "")
        self.assertEqual(payload["html_url"], "")
        self.assertEqual(payload["assets"], {})
        self.assertIn("offline", payload["error"])

    def test_asset_payload_does_not_echo_secrets_or_tokens(self):
        payload = _latest_release_payload(
            {
                "tag_name": "1.2.3",
                "html_url": "x",
                "token": "secret-token",
                "assets": [
                    {
                        **self.asset("vaultline-worker_1.2.3_amd64.deb", "sha256:" + "a" * 64),
                        "authorization": "Bearer secret-token",
                        "node_id": "secret-node",
                    }
                ],
            }
        )

        serialized = json.dumps(payload, sort_keys=True)
        self.assertNotRegex(serialized, re.compile("secret|token|authorization|node_id", re.IGNORECASE))


if __name__ == "__main__":
    unittest.main()
