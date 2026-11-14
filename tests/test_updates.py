"""Tests of the UpdateWatcher helper."""

from __future__ import annotations

import unittest
from unittest import mock

from wvbot.config import CORE_URL, Config
from wvbot.updates import SITE_URL, UpdateWatcher


class UpdateWatcherTest(unittest.TestCase):
    def test_auth_headers_dict_and_callable_and_setter(self) -> None:
        http = mock.Mock()
        w_dict = UpdateWatcher(Config(), http, {"Authorization": "Bearer static"})
        self.assertEqual(w_dict.auth_headers, {"Authorization": "Bearer static"})

        tok = "first"
        w_call = UpdateWatcher(Config(), http, lambda: {"Authorization": f"Bearer {tok}"})
        self.assertEqual(w_call.auth_headers, {"Authorization": "Bearer first"})
        tok = "second"
        self.assertEqual(w_call.auth_headers, {"Authorization": "Bearer second"})

        w_call.auth_headers = {"Authorization": "Bearer override"}
        self.assertEqual(w_call.auth_headers, {"Authorization": "Bearer override"})

    def test_check_detects_build_and_bundle_changes(self) -> None:
        responses = [
            {"latestAppVersion": {"versionNumber": 100}},
            '<script src="/static/js/main.abc123def.js"></script>',
            {"latestAppVersion": {"versionNumber": 101}},
            '<script src="/static/js/main.9999aaaa.js"></script>',
        ]

        def fake_request(method, url, body=None, headers=None, retry=True):
            return responses.pop(0)

        http = mock.Mock()
        http.request.side_effect = fake_request
        watcher = UpdateWatcher(Config(), http, lambda: {"Authorization": "Bearer a"})

        first = watcher.check()
        self.assertEqual(first["build_number"], "100")
        self.assertEqual(first["bundle_hash"], "abc123def")
        self.assertFalse(first["changed"])
        self.assertEqual(http.request.call_args_list[0][0][1], f"{CORE_URL}/appVersions/check")
        self.assertEqual(http.request.call_args_list[1][0][1], f"{SITE_URL}/")

        second = watcher.check()
        self.assertTrue(second["changed"])
        self.assertTrue(second["build_changed"])
        self.assertTrue(second["bundle_changed"])
        self.assertEqual(second["previous_build"], "100")
        self.assertEqual(second["previous_bundle"], "abc123def")

    def test_bundle_fetch_failure_does_not_abort_check(self) -> None:
        def fake_request(method, url, body=None, headers=None, retry=True):
            if url.startswith(CORE_URL):
                return {"latestAppVersion": {"versionString": "1.2.3"}}
            raise RuntimeError("shield blocked")

        http = mock.Mock()
        http.request.side_effect = fake_request
        watcher = UpdateWatcher(Config(), http)
        res = watcher.check()
        self.assertEqual(res["build_number"], "1.2.3")
        self.assertEqual(res["bundle_hash"], "")
        self.assertFalse(res["changed"])


if __name__ == "__main__":
    unittest.main()
