import hashlib
import importlib.util
import io
import json
import sys
import tempfile
import unittest
import urllib.error
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
SPEC = importlib.util.spec_from_file_location("mirror", Path(__file__).resolve().parents[1] / "scripts/mirror.py")
m = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(m)


class Response(io.BytesIO):
    def __init__(self, body, content_type="application/octet-stream", length=None):
        super().__init__(body)
        self.headers = {"Content-Type": content_type, "Content-Length": str(len(body) if length is None else length)}


class MirrorTests(unittest.TestCase):
    def test_transfer_tag_requires_run_and_attempt(self):
        with patch.dict(m.os.environ, {"GITHUB_RUN_ID": "123", "GITHUB_RUN_ATTEMPT": "2"}):
            self.assertEqual(m.transfer_tag(), "mirror-run-123-2")
        with patch.dict(m.os.environ, {}, clear=True):
            with self.assertRaises(ValueError):
                m.transfer_tag()

    def test_transfer_rejects_wrong_run_or_published_release(self):
        for release in ({"draft": True, "tag_name": "mirror-run-999-1"},
                        {"draft": False, "tag_name": "mirror-run-123-2"}, None):
            with self.subTest(release=release), patch.dict(m.os.environ, {
                    "GITHUB_RUN_ID": "123", "GITHUB_RUN_ATTEMPT": "2", "TRANSFER_RELEASE_ID": "5"}):
                client = Mock(repo="owner/repo")
                client.api.return_value = release
                with self.assertRaises(ValueError):
                    m.transfer_release(client)

    def test_transfer_download_verifies_utf8_bytes_and_json(self):
        payload = '{"files": ["中文.bin"]}\n'.encode()
        asset = {"url": "asset-url", "name": "catalog.json", "state": "uploaded",
                 "size": len(payload), "digest": "sha256:" + hashlib.sha256(payload).hexdigest()}
        with tempfile.TemporaryDirectory() as temp, patch.object(m, "gh", return_value=SimpleNamespace(stdout=payload.decode())):
            target = Path(temp) / "subdir/catalog.json"
            m.transfer_download(Mock(), asset, target)
            self.assertEqual(target.read_bytes(), payload)
            asset["digest"] = "sha256:" + "0" * 64
            bad = Path(temp) / "bad.json"
            with self.assertRaises(m.PublicationError):
                m.transfer_download(Mock(), asset, bad)
            self.assertFalse(bad.exists())

    def test_transfer_upload_checks_remote_digest_and_retries_pending_state(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "catalog.json"
            path.write_text('{"files": []}\n')
            client = Mock()
            client.assets.side_effect = [{}, {path.name: {
                "state": "uploaded", "size": path.stat().st_size, "digest": "sha256:" + m.file_sha(path)}}]
            with patch.object(m.time, "sleep") as sleep:
                m.transfer_upload(client, {"tag_name": "mirror-run-123-1"}, path)
            client.upload.assert_called_once_with("mirror-run-123-1", path, clobber=True)
            self.assertEqual(client.assets.call_count, 2)
            sleep.assert_called_once_with(2)

    def test_transfer_upload_fails_when_remote_digest_never_matches(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "catalog.json"
            path.write_text("{}")
            client = Mock()
            client.assets.return_value = {}
            with patch.object(m.time, "sleep"), self.assertRaises(m.PublicationError):
                m.transfer_upload(client, {"tag_name": "mirror-run-123-1"}, path)
            self.assertEqual(client.assets.call_count, 3)

    def test_stage_inventory_exposes_draft_id_and_does_not_publish_firmware(self):
        with tempfile.TemporaryDirectory() as temp, patch.dict(m.os.environ, {
                "GITHUB_RUN_ID": "123", "GITHUB_RUN_ATTEMPT": "2", "GITHUB_OUTPUT": str(Path(temp) / "outputs")}), \
                patch.object(m, "prune_transfer_drafts"), patch.object(m, "transfer_upload") as upload:
            path = Path(temp) / "catalog.json"
            m.save(path, {"files": [{"filename": "public.bin"}]})
            client = Mock(repo="owner/repo")
            client.api.return_value = {"id": 5, "tag_name": "mirror-run-123-2"}
            with patch.object(m, "GitHub", return_value=client):
                self.assertFalse(m.stage_inventory(SimpleNamespace(repo="owner/repo", catalog=str(path))))
            self.assertTrue(client.api.call_args.args[1]["draft"])
            self.assertEqual(Path(m.os.environ["GITHUB_OUTPUT"]).read_text(), "release_id=5\n")
            upload.assert_called_once_with(client, client.api.return_value, path)

    def test_empty_inventory_does_not_create_draft(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(m, "GitHub") as client:
            path = Path(temp) / "catalog.json"
            m.save(path, {"files": []})
            with self.assertRaises(ValueError):
                m.stage_inventory(SimpleNamespace(repo="owner/repo", catalog=str(path)))
            client.assert_not_called()

    def test_fetch_reports_never_uses_another_runs_missing_shard(self):
        client = Mock(repo="owner/repo")
        client.assets.return_value = {"shard-0.json": {"name": "shard-0.json"},
                                      "shard-999.json": {"name": "shard-999.json"}}
        with patch.object(m, "GitHub", return_value=client), patch.object(m, "transfer_release"), \
                patch.object(m, "transfer_download") as download:
            self.assertFalse(m.fetch_reports(SimpleNamespace(repo="owner/repo", shards=2, report_dir="reports")))
            self.assertEqual(download.call_count, 1)
            self.assertEqual(download.call_args.args[2], Path("reports/shard-0.json"))

    def test_transfer_cleanup_deletes_only_validated_current_draft(self):
        client = Mock(repo="owner/repo")
        with patch.object(m, "GitHub", return_value=client), \
                patch.object(m, "transfer_release", return_value={"id": 5}):
            self.assertFalse(m.cleanup_transfer(SimpleNamespace(repo="owner/repo")))
        client.api.assert_called_once_with("repos/owner/repo/releases/5", method="DELETE")

    def test_stale_transfer_cleanup_preserves_other_drafts_and_recent_runs(self):
        client = Mock(repo="owner/repo")
        client.api.side_effect = [[
            {"id": 1, "draft": True, "tag_name": "mirror-run-1-1", "body": json.dumps({"schema": "ikuai-workflow-transfer/v1", "created_at": "2020-01-01T00:00:00+00:00"})},
            {"id": 2, "draft": False, "tag_name": "mirror-run-2-1"},
            {"id": 3, "draft": True, "tag_name": "manual-draft"},
            {"id": 4, "draft": True, "tag_name": "mirror-run-4-1", "body": json.dumps({"schema": "ikuai-workflow-transfer/v1", "created_at": m.now()})},
            {"id": 5, "draft": True, "tag_name": "mirror-run-5-1", "body": "unrelated"},
        ], None]
        m.prune_transfer_drafts(client)
        self.assertEqual(client.api.call_count, 2)
        self.assertEqual(client.api.call_args.args[0], "repos/owner/repo/releases/1")
        self.assertEqual(client.api.call_args.kwargs["method"], "DELETE")

    def test_draft_transfer_is_not_retargeted_as_a_public_tag(self):
        client = Mock(repo="owner/repo")
        client.api.return_value = [{"draft": True, "tag_name": "mirror-run-1-1"}]
        with patch.object(m, "dated_tag") as tag:
            m.retarget_release_tags(client, "root")
        tag.assert_not_called()

    def test_workflow_uses_verified_release_metadata_not_artifact_service(self):
        workflow = (m.ROOT / ".github/workflows/sync.yml").read_text()
        self.assertNotIn("actions/upload-artifact@", workflow)
        self.assertNotIn("actions/download-artifact@", workflow)
        for command in ("stage-inventory", "fetch-inventory", "stage-report", "fetch-reports", "cleanup-transfer"):
            self.assertIn("scripts/mirror.py " + command, workflow)

    def test_new_releases_target_current_main_after_history_compaction(self):
        client = m.GitHub("owner/repo")
        with patch.object(client, "release", return_value=None), \
                patch.object(client, "api", side_effect=[{"object": {"sha": "current-main"}}, {"id": 1}]) as api, \
                patch.object(m, "dated_tag") as tag, \
                patch.dict(m.os.environ, {"GITHUB_SHA": "obsolete-workflow-commit"}):
            client.ensure_release("enterprise-3.7.26-Build1")
        self.assertEqual(api.call_args.args[1]["target_commitish"], "main")
        self.assertEqual(tag.call_args.args[2], "current-main")

    def test_build_dates_use_china_timezone_and_undated_files_sort_last(self):
        self.assertEqual(m.build_date("enterprise-3.7.26-Enterprise-Build202609111742"), "2026-09-11T09:42:00+00:00")
        self.assertIsNone(m.build_date("bad-Build202699991742"))
        self.assertEqual(m.release_sort_date("hardware-M100", [{"name": "fw_Build202609111742.bin"}]), "2026-09-11T09:42:00+00:00")
        self.assertEqual(m.release_sort_date("resources-router-0"), "1970-01-01T00:00:00+00:00")
        self.assertIsNone(m.release_sort_date(m.INDEX_TAG))

    def test_annotation_is_reused_when_root_and_build_date_match(self):
        client = Mock(repo="owner/repo")
        client.api.side_effect = [
            {"object": {"type": "tag", "sha": "annotation"}},
            {"object": {"sha": "root"}, "tagger": {"date": "2026-09-11T09:42:00Z"}},
        ]
        m.dated_tag(client, "test", "root", "2026-09-11T09:42:00+00:00")
        self.assertEqual(client.api.call_count, 2)

    def test_annotation_retargets_root_without_losing_build_date(self):
        client = Mock(repo="owner/repo")
        client.api.side_effect = [
            {"object": {"type": "tag", "sha": "old-annotation"}},
            {"object": {"sha": "old-root"}, "tagger": {"date": "2026-09-11T09:42:00Z"}},
            {"sha": "new-annotation"}, {},
        ]
        m.dated_tag(client, "test", "new-root", "2026-09-11T09:42:00+00:00")
        payload = client.api.call_args_list[2].args[1]
        self.assertEqual(payload["object"], "new-root")
        self.assertEqual(payload["tagger"]["date"], "2026-09-11T09:42:00+00:00")
        self.assertEqual(client.api.call_args.args[1], {"sha": "new-annotation", "force": True})

    def test_github_transport_errors_have_bounded_retries(self):
        failure = SimpleNamespace(returncode=1, stderr="unexpected EOF")
        success = SimpleNamespace(returncode=0, stderr="")
        with patch.object(m.subprocess, "run", side_effect=[failure, success]) as run, patch.object(m.time, "sleep"):
            self.assertIs(m.run_gh(["api", "example"]), success)
            self.assertEqual(run.call_count, 2)
        with patch.object(m.subprocess, "run", return_value=failure) as run, patch.object(m.time, "sleep"):
            self.assertIs(m.run_gh(["api", "example"]), failure)
            self.assertEqual(run.call_count, 3)

    def test_github_permission_errors_are_publication_failures(self):
        result = SimpleNamespace(returncode=1, stderr="gh: Resource not accessible by integration (HTTP 403)")
        with patch.object(m, "run_gh", return_value=result):
            with self.assertRaisesRegex(m.PublicationError, "POST repos/owner/repo/releases"):
                m.GitHub("owner/repo").api("repos/owner/repo/releases", {"tag_name": "test"})

    def test_publication_failure_stops_before_downloading_next_file(self):
        entries = m.merge_candidates([m.candidate("a.bin", "https://example.com/a"),
                                      m.candidate("b.bin", "https://example.com/b")])
        client = Mock(repo="owner/repo")
        client.release.return_value = None
        client.assets.return_value = {}
        client.read_asset.return_value = {"files": []}
        client.ensure_release.side_effect = m.PublicationError("HTTP 403")
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(m, "download", return_value=(Path(directory) / "a.bin", {})) as download, \
                patch.object(m, "split_file", return_value=[]):
            with self.assertRaisesRegex(m.PublicationError, "403"):
                m.sync_group(client, "hardware-test", entries, Path(directory))
        self.assertEqual(download.call_count, 1)
        client.upload.assert_not_called()

    def test_upload_permission_errors_do_not_retry(self):
        result = SimpleNamespace(returncode=1, stderr="gh: HTTP 403")
        with patch.object(m, "gh", return_value=result) as gh, patch.object(m.time, "sleep") as sleep:
            with self.assertRaises(m.PublicationError):
                m.GitHub("owner/repo").upload("test", Path("firmware.bin"))
        self.assertEqual(gh.call_count, 1)
        sleep.assert_not_called()

    def test_enterprise_and_numeric_newest_versions_are_first(self):
        tags = ["free-4.0.315-beta-Build1", "enterprise-3.7.9-Enterprise-Build1",
                "enterprise-3.7.26-Enterprise-Build2"]
        self.assertEqual(sorted(tags, key=m.group_priority, reverse=True), [tags[2], tags[1], tags[0]])

    def test_slow_download_switches_source_without_restarting_same_source(self):
        entry = m.merge_candidates([m.candidate("test.bin", "https://example.com/a")])[0]
        entry["sources"].append({"url": "https://example.com/b"})
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(m.time, "monotonic", side_effect=[0, 301, 302, 303]), \
                patch.object(m, "request", side_effect=[Response(b"slow"), Response(b"fast")]) as request:
            path, record = m.download(entry, Path(directory))
            self.assertEqual(path.read_bytes(), b"fast")
            self.assertEqual(record["source"]["url"], "https://example.com/b")
        self.assertEqual(request.call_count, 2)

    def test_html_content_type_is_rejected_before_reading_body(self):
        entry = m.merge_candidates([m.candidate("test.bin", "https://example.com/a")])[0]
        response = Response(b"blocked", content_type="text/html")
        with tempfile.TemporaryDirectory() as directory, patch.object(m, "request", return_value=response), \
                patch.object(response, "read1") as read:
            with self.assertRaises(ValueError):
                m.download(entry, Path(directory))
        read.assert_not_called()

    def test_old_completed_runs_are_removed_but_active_runs_are_kept(self):
        runs = [
            {"id": 1, "created_at": "2026-11-01T00:00:00Z", "status": "completed"},
            {"id": 2, "created_at": "2026-11-02T00:00:00Z", "status": "in_progress"},
            {"id": 3, "created_at": "2026-11-03T00:00:00Z", "status": "queued"},
            {"id": 4, "created_at": "2026-11-04T00:00:00Z", "status": "completed"},
        ]
        self.assertEqual(m.runs_to_delete(runs), [1])
        self.assertEqual(m.runs_to_delete(runs, current_run="1"), [])
        self.assertEqual(m.runs_to_delete([]), [])

    def test_history_compaction_and_keepalive_threshold(self):
        current = datetime(2026, 11, 6, tzinfo=timezone.utc)
        commit = {"parents": [], "committer": {"date": "2026-11-01T00:00:00Z"}}
        self.assertFalse(m.root_commit_needed(commit, current))
        commit["parents"] = [{"sha": "parent"}]
        self.assertTrue(m.root_commit_needed(commit, current))
        commit["parents"] = []
        commit["committer"]["date"] = "2026-09-01T00:00:00Z"
        self.assertTrue(m.root_commit_needed(commit, current))

    def test_compaction_uses_latest_tree_and_force_with_lease(self):
        client = Mock(repo="owner/repo")
        client.api.side_effect = [
            {"object": {"sha": "old-head"}},
            {"parents": [{"sha": "parent"}], "committer": {"date": "2026-11-01T00:00:00Z"},
             "author": {"name": "Owner", "email": "owner@example.com"}, "tree": {"sha": "latest-tree"}},
        ]
        with patch.object(m, "GitHub", return_value=client), \
                patch.object(m, "git_remote_env", return_value={}), \
                patch.object(m, "git_run", side_effect=["", "new-root", ""]) as git, \
                patch.object(m, "retarget_release_tags") as tags:
            self.assertFalse(m.compact_history(SimpleNamespace(repo="owner/repo")))
        self.assertEqual(git.call_args_list[1].args[0], ["commit-tree", "latest-tree"])
        self.assertIn("--force-with-lease=refs/heads/main:old-head", git.call_args_list[2].args[0])
        tags.assert_called_once_with(client, "new-root")

    def test_git_authentication_stays_in_environment(self):
        with patch.dict(m.os.environ, {"GH_TOKEN": "example-token"}, clear=True):
            env = m.git_remote_env()
        self.assertEqual(env["GIT_CONFIG_COUNT"], "1")
        self.assertEqual(env["GIT_CONFIG_KEY_0"], "http.https://github.com/.extraheader")
        self.assertTrue(env["GIT_CONFIG_VALUE_0"].startswith("AUTHORIZATION: basic "))

    def test_compaction_retries_with_concurrent_updates_preserved(self):
        client = Mock(repo="owner/repo")
        metadata = {"parents": [{"sha": "parent"}], "committer": {"date": "2026-11-01T00:00:00Z"},
                    "author": {"name": "Owner", "email": "owner@example.com"}}
        client.api.side_effect = [
            {"object": {"sha": "old-head"}}, {**metadata, "tree": {"sha": "old-tree"}},
            {"object": {"sha": "new-user-head"}}, {**metadata, "tree": {"sha": "new-user-tree"}},
        ]
        with patch.object(m, "GitHub", return_value=client), \
                patch.object(m, "git_remote_env", return_value={}), \
                patch.object(m, "git_run", side_effect=["", "root1", RuntimeError("stale info"), "", "root2", ""]) as git, \
                patch.object(m, "retarget_release_tags") as tags:
            self.assertFalse(m.compact_history(SimpleNamespace(repo="owner/repo")))
        self.assertEqual(git.call_args_list[4].args[0], ["commit-tree", "new-user-tree"])
        self.assertIn("--force-with-lease=refs/heads/main:new-user-head", git.call_args_list[5].args[0])
        tags.assert_called_once_with(client, "root2")

    def test_tag_maintenance_stops_when_main_changes(self):
        client = Mock(repo="owner/repo")
        client.api.side_effect = [[{"name": "tag", "commit": {"sha": "old"}}],
                                  {"object": {"sha": "new-user-head"}}]
        m.retarget_release_tags(client, "expected-root")
        self.assertEqual(client.api.call_count, 2)

    def test_scheduled_jitter_bounds(self):
        for delay in (0, 1800):
            with self.subTest(delay=delay), patch.dict(m.os.environ, {"GITHUB_EVENT_NAME": "schedule"}), \
                    patch.object(m.secrets, "randbelow", return_value=delay) as random_delay, \
                    patch.object(m.time, "sleep") as sleep:
                self.assertFalse(m.jitter(None))
                random_delay.assert_called_once_with(1801)
                sleep.assert_called_once_with(delay)

    def test_manual_runs_do_not_wait(self):
        with patch.dict(m.os.environ, {"GITHUB_EVENT_NAME": "workflow_dispatch"}), \
                patch.object(m.secrets, "randbelow") as random_delay, \
                patch.object(m.time, "sleep") as sleep:
            self.assertFalse(m.jitter(None))
            random_delay.assert_not_called()
            sleep.assert_not_called()

    def test_local_runs_do_not_wait(self):
        with patch.dict(m.os.environ, {}, clear=True), patch.object(m.time, "sleep") as sleep:
            self.assertFalse(m.jitter(None))
            sleep.assert_not_called()

    def test_architectures_share_release_not_assets(self):
        entries = m.merge_candidates(m.x86_candidates("3.7.26_Build202609111743"))
        self.assertEqual(len(entries), 10)
        self.assertEqual(len({e["tag"] for e in entries}), 1)
        self.assertEqual(len({e["asset_name"] for e in entries}), 10)

    def test_enterprise_filename(self):
        entries = m.x86_candidates("3.7.26_Enterprise_Build202609111742", "enterprise")
        self.assertEqual(len(entries), 2)
        self.assertEqual(entries[0]["filename"], "iKuai8_x32_3.7.26_Enterprise_Build202609111742.bin")
        self.assertIn("/ent/", entries[0]["sources"][0]["url"])

    def test_beta_and_oem(self):
        self.assertEqual(len(m.x86_candidates("4.0.315_beta_Build202610291837")), 4)
        self.assertEqual(len(m.x86_candidates("3.6.5_Build202207280937", "oem")), 2)

    def test_prerelease_classification(self):
        self.assertFalse(m.is_prerelease("resources-router-1"))
        self.assertFalse(m.is_prerelease("free-3.7.26-Build202609111743"))
        self.assertTrue(m.is_prerelease("free-4.0.315-beta-Build202610291837"))

    def test_legacy_x86_group(self):
        self.assertTrue(m.group_for("iKuai8_2.7.18_Build20180409-09_33.iso").startswith("free-2.7.18"))

    def test_official_hardware_profiles(self):
        text = "[M100_BETA]\nfirmware_x64 = IK-MT7981V1-M100_sysupgrade_4.0.222_beta_Build202605191449.bin\n"
        entries, _ = m.parse_versions(text)
        api_source = next(s for e in entries for s in e["sources"] if "firmware" in s)
        self.assertEqual(api_source["device"], "M100")
        self.assertEqual(api_source["system"], "enterprise")
        self.assertEqual(api_source["platform"], "arm")
        self.assertTrue(any("/firmware/M100/" in s["url"] for e in entries for s in e["sources"]))
        self.assertEqual(m.platform_for("iKuai8_x32_3.7.26.bin"), "x86-32")

    def test_signing_does_not_store_ephemeral_credentials(self):
        api = m.FirmwareAPI()
        api.context = object()
        response = Response(json.dumps({"code": 0, "data": {"signUrl": "https://example.com/signed", "token": "ephemeral"}}).encode())
        source = {"device": "M100", "platform": "arm", "firmware": "test.bin", "system": "enterprise"}
        with patch.object(m, "request", return_value=response):
            url, headers, _ = api.resolve(source)
        self.assertEqual(headers["X-Auth-Token"], "ephemeral")
        self.assertNotIn("token", source)
        self.assertEqual(url, "https://example.com/signed")

    def test_source_merge_and_priority(self):
        a = m.candidate("iKuai8_x64_3.7.26_Build1.bin", "https://example.com/a", inferred=True)
        b = m.candidate(a["filename"], cloud_path="/历史固件/" + a["filename"], size=20)
        entries = m.merge_candidates([a, b, a])
        self.assertEqual(len(entries), 1)
        self.assertEqual(len(entries[0]["sources"]), 2)
        self.assertIn("cloud_path", entries[0]["sources"][0])
        self.assertEqual(entries[0]["size"], 20)
        self.assertEqual(m.merge_candidates(entries)[0]["id"], entries[0]["id"])

    def test_unicode_and_duplicate_paths(self):
        entries = m.merge_candidates([
            m.candidate("教程.pdf", cloud_path="/AP/a/教程.pdf", size=4),
            m.candidate("教程.pdf", cloud_path="/AP/b/教程.pdf", size=4),
        ])
        self.assertEqual(len({e["asset_name"] for e in entries}), 2)
        self.assertTrue(all(e["asset_name"].isascii() for e in entries))
        self.assertEqual(m.merge_candidates(entries), entries)
        self.assertTrue(all("_" not in e["asset_name"] for e in entries))

    def test_english_names(self):
        self.assertEqual(m.english_filename("DHCP静态分配和ARP绑定的作用.pdf", "a" * 64),
                         ("DHCP-Static-Leases-and-ARP-Binding.pdf", ""))
        self.assertEqual(m.english_filename("爱快IPSEC对接华为云IPSEC.pdf", "a" * 64),
                         ("iKuai-IPSEC-Integration-Huawei-Cloud-IPSEC.pdf", ""))

    def test_filename_table(self):
        records = {"a": {"id": "a", "filename": "中文.pdf", "assets": [{"name": "English.pdf"}]}}
        body = m.release_body("owner/repo", "resources-tools-0", records, [])
        self.assertIn("| 中文.pdf | [English.pdf]", body)
        self.assertEqual(m.release_title("resources-tutorial-2"), "功能配合类教程 · 3/4")

    def test_category_directory_groups_actual_releases_and_sorts_newest_first(self):
        releases = [
            {"tag_name": "free-3.7.9-Build202401121849", "name": "older", "created_at": "2024-01-12"},
            {"tag_name": "free-3.7.26-Build202609111743", "name": "newer", "created_at": "2026-09-11"},
            {"tag_name": "resources-router-2", "name": "router docs"},
            {"tag_name": "enterprise-3.7.26-Enterprise-Build202609111742", "name": "enterprise"},
            {"tag_name": m.INDEX_TAG, "name": "index"},
            {"tag_name": "free-4.0.1-Build1", "name": "draft", "draft": True},
        ]
        body = m.category_body("owner/repo", releases)
        self.assertLess(body.index("[newer]"), body.index("[older]"))
        self.assertIn('<a name="resources-router"></a>', body)
        self.assertNotIn("[draft]", body)
        self.assertNotIn("[index]", body)
        self.assertEqual(body.count("[router docs]"), 1)
        self.assertEqual(body.count("[enterprise]"), 1)

    def test_readme_category_shortcuts_all_have_real_directory_anchors(self):
        readme = (m.ROOT / "README.md").read_text()
        fragments = m.re.findall(r"/releases/tag/download-categories#user-content-([a-z0-9-]+)", readme)
        body = m.category_body("owner/repo", [])
        self.assertEqual(len(fragments), 20)
        self.assertEqual(len(set(fragments)), len(fragments))
        for fragment in fragments:
            self.assertIn(f'<a name="{fragment}"></a>', body)
        self.assertEqual(m.release_title(m.CATEGORY_TAG), "iKuai 分类下载目录")

    def test_enterprise_v4_beta_has_its_own_category_without_free_or_stable_releases(self):
        releases = [
            {"tag_name": "enterprise-4.0.223-beta-Enterprise-Build202605281557", "name": "Enterprise Beta"},
            {"tag_name": "enterprise-4.0.223-Enterprise-Build202605281557", "name": "Enterprise Stable"},
            {"tag_name": "free-4.0.223-beta-Build202605281556", "name": "Free Beta"},
            {"tag_name": "enterprise-3.7.26-Enterprise-Build202609111742", "name": "Enterprise 3.x"},
        ]
        body = m.category_body("owner/repo", releases)
        section = body.split('<a name="enterprise-4-beta"></a>', 1)[1].split('<a name="oem"></a>', 1)[0]
        self.assertIn("[Enterprise Beta]", section)
        self.assertNotIn("[Enterprise Stable]", section)
        self.assertNotIn("[Free Beta]", section)
        self.assertNotIn("[Enterprise 3.x]", section)
        self.assertEqual(body.count("[Enterprise Beta]"), 2)

    def test_rename_keeps_content_digest(self):
        entry = m.merge_candidates([m.candidate("教程.pdf", cloud_path="/AP/教程.pdf", size=8)])[0]
        old_name = m.legacy_asset_filename(entry)
        remote = {"id": 10, "name": old_name, "label": "", "size": 8,
                  "digest": "sha256:" + "1" * 64, "created_at": "2026-11-06T00:00:00Z"}
        assets, records = {old_name: remote}, {}
        class Client:
            repo = "owner/repo"
            def api(self, endpoint, data, method):
                return {**remote, **data}
        self.assertTrue(m.repair_group_names(Client(), [entry], records, assets))
        record = records[entry["id"]]
        self.assertEqual(record["assets"][0]["name"], entry["asset_name"])
        self.assertEqual(record["sha256"], "1" * 64)
        self.assertTrue(m.complete_record(record, assets))

    def test_incomplete_upload_without_digest_is_not_recovered(self):
        entry = m.merge_candidates([m.candidate("test.bin", "https://example.com/a")])[0]
        assets = {entry["asset_name"]: {"id": 1, "name": entry["asset_name"], "size": 8,
                                      "state": "starter", "digest": None}}
        records, client = {}, Mock(repo="owner/repo")
        self.assertFalse(m.repair_group_names(client, [entry], records, assets))
        self.assertEqual(records, {})
        client.api.assert_not_called()

    def test_names_preserve_file_extensions(self):
        examples = ("爱快App5.0.17更新2024-09-20-104309.apk", "链路聚合（WAN口）.pdf",
                    "高级自定义协议.txt", "小白说明.rar", "虚拟机安装爱快系统（英文版）.mp4")
        for original in examples:
            name, missing = m.english_filename(original, "a" * 64)
            self.assertFalse(missing)
            self.assertTrue(name.isascii())
            self.assertEqual(Path(name).suffix, Path(original).suffix)

    def test_reject_path_traversal(self):
        for name in ("../a", "..", "a/b", "a\\b", "a\nb"):
            with self.assertRaises(ValueError):
                m.candidate(name, "https://example.com/a")

    def test_cloud_pagination(self):
        pages = [
            {"code": 200, "data": {"content": [{"name": "a"}], "total": 2, "has_more": True}},
            {"code": 200, "data": {"content": [{"name": "b"}], "total": 2, "has_more": False}},
        ]
        with patch.object(m, "get_json", side_effect=pages) as get:
            self.assertEqual(len(m.cloud_list("/")), 2)
            self.assertEqual(get.call_args.args[1]["page"], 2)

    def test_reject_incomplete_pagination(self):
        with patch.object(m, "get_json", return_value={"code": 200, "data": {"content": [], "total": 5}}):
            with self.assertRaises(ValueError):
                m.cloud_list("/")

    def test_html_error_not_mirrored(self):
        entry = m.merge_candidates([m.candidate("firmware.bin", "https://example.com/a")])[0]
        with tempfile.TemporaryDirectory() as directory, patch.object(m, "request", return_value=Response(b"<!DOCTYPE html>blocked")):
            with self.assertRaises(ValueError):
                m.download(entry, Path(directory))
            self.assertEqual(list(Path(directory).iterdir()), [])

    def test_download_size_and_blob_integrity(self):
        body = b"real firmware data"
        sha1 = hashlib.sha1(f"blob {len(body)}\0".encode() + body).hexdigest()
        entry = m.merge_candidates([m.candidate("test.bin", "https://example.com/a", size=len(body), git_blob_sha1=sha1)])[0]
        with tempfile.TemporaryDirectory() as directory, patch.object(m, "request", return_value=Response(body)):
            path, record = m.download(entry, Path(directory))
            self.assertEqual(path.read_bytes(), body)
            self.assertEqual(record["sha256"], hashlib.sha256(body).hexdigest())

    def test_bad_size_rejected(self):
        entry = m.merge_candidates([m.candidate("test.bin", "https://example.com/a", size=99)])[0]
        with tempfile.TemporaryDirectory() as directory, patch.object(m, "request", return_value=Response(b"short")):
            with self.assertRaises(ValueError):
                m.download(entry, Path(directory))

    def test_404_not_retried(self):
        entry = m.merge_candidates([m.candidate("test.bin", "https://example.com/a")])[0]
        with tempfile.TemporaryDirectory() as directory, patch.object(m, "request", side_effect=urllib.error.HTTPError("url", 404, "gone", {}, None)) as get:
            with self.assertRaises(ValueError):
                m.download(entry, Path(directory))
            self.assertEqual(get.call_count, 1)

    def test_split_roundtrip(self):
        with tempfile.TemporaryDirectory() as directory:
            original = Path(directory) / "large.bin"
            original.write_bytes(bytes(range(256)))
            parts = m.split_file(original, size=100)
            self.assertEqual([p.stat().st_size for p in parts], [100, 100, 56])
            self.assertEqual(b"".join(p.read_bytes() for p in parts), original.read_bytes())

    def test_resume_requires_remote_digest(self):
        record = {"assets": [{"name": "a", "size": 8, "sha256": "1" * 64}]}
        assets = {"a": {"name": "a", "size": 8, "digest": "sha256:" + "1" * 64}}
        self.assertTrue(m.complete_record(record, assets))
        assets["a"]["digest"] = "sha256:" + "2" * 64
        self.assertFalse(m.complete_record(record, assets))


if __name__ == "__main__":
    unittest.main()
