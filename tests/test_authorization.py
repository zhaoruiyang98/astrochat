import unittest
from unittest.mock import Mock

from astrochat.authorization import Authorization, create_app
from astrochat.project_store import ProjectError
from test_project_management import ProjectFixture


class AuthorizationTests(ProjectFixture):
    def setUp(self):
        super().setUp()
        self.create()
        self.ticket = self.store.issue_ticket(10, "research")
        self.worker = Mock()
        self.validate = Mock(return_value="P_1")
        self.authorization = Authorization(
            self.store, self.worker, "https://auth.example.com", self.validate
        )
        self.app = create_app(self.authorization)
        self.client = self.app.test_client()
        self.headers = {"Origin": "https://auth.example.com"}

    def post(self, path="/api/authorize", **changes):
        data = {"ticket": self.ticket, "token": "github-secret-token", **changes}
        return self.client.post(path, json=data, headers=self.headers)

    def test_page_and_static_assets_do_not_contain_credentials_or_consume_link(self):
        for path in ("/authorize", "/static/authorize.js", "/static/authorize.css"):
            with self.client.get(path) as response:
                self.assertEqual(response.status_code, 200)
                self.assertNotIn(self.ticket.encode(), response.data)
                self.assertIn("no-store", response.headers["Cache-Control"])
                self.assertEqual(response.headers["Referrer-Policy"], "no-referrer")
                self.assertIn("frame-ancestors 'none'", response.headers["Content-Security-Policy"])
        self.assertEqual(self.store.ticket_project(self.ticket).user_id, 10)

    def test_info_exposes_only_the_ticket_bound_project(self):
        response = self.client.post(
            "/api/authorize/info", json={"ticket": self.ticket}, headers=self.headers
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["user_id"], 10)
        self.assertEqual(response.json["name"], "research")
        self.assertNotIn("token", response.json)
        self.assertNotIn("secret", response.get_data(as_text=True))
        self.validate.assert_not_called()

    def test_submission_saves_encrypted_token_and_cannot_be_replayed(self):
        self.assertEqual(self.post().status_code, 200)
        project = self.store.get_owned(10, "research")
        self.assertEqual(self.store.token_for(project), "github-secret-token")
        self.assertEqual(project.github_id, "P_1")
        self.worker.changed.assert_called_once_with(project.id)
        self.assertNotIn(b"github-secret-token", (self.path / "projects.sqlite3").read_bytes())
        self.assertEqual(self.post().status_code, 400)
        self.assertEqual(self.validate.call_count, 1)

    def test_untrusted_origins_forms_and_spoofed_owner_are_rejected(self):
        for headers in ({}, {"Origin": "https://evil.test"}, {"Origin": "null"}):
            response = self.client.post(
                "/api/authorize",
                json={"ticket": self.ticket, "token": "github-secret-token"},
                headers=headers,
            )
            self.assertEqual(response.status_code, 400)
        response = self.client.post(
            "/api/authorize",
            data={"ticket": self.ticket, "token": "github-secret-token"},
            headers=self.headers,
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.post(user_id=11).status_code, 400)
        self.assertEqual(self.post(project_id="other").status_code, 400)
        self.validate.assert_not_called()
        self.assertFalse(self.store.get_owned(10, "research").authorized)

    def test_invalid_tokens_and_oversized_payloads_are_rejected_without_echo(self):
        for token in ("tiny", "abc\r\nsecret-token", "x" * 513, 123, None):
            response = self.post(token=token)
            self.assertEqual(response.status_code, 400)
            self.assertNotIn("secret-token", response.get_data(as_text=True))
        with self.assertLogs("astrochat.authorization", "WARNING"):
            response = self.post(token="x" * 5000)
        self.assertEqual(response.status_code, 413)
        self.validate.assert_not_called()

    def test_upstream_errors_do_not_leak_tokens_and_leave_existing_token_unchanged(self):
        self.post()
        self.ticket = self.store.issue_ticket(10, "research")
        self.validate.side_effect = RuntimeError("secret-new-token")
        with self.assertLogs("astrochat.authorization", "WARNING") as logs:
            response = self.post(token="secret-new-token")
        self.assertEqual(response.status_code, 400)
        self.assertNotIn("secret-new-token", response.get_data(as_text=True))
        self.assertNotIn("secret-new-token", "".join(logs.output))
        self.assertEqual(
            self.store.token_for(self.store.get_owned(10, "research")), "github-secret-token"
        )
        with self.assertRaises(ProjectError):
            self.store.ticket_project(self.ticket)

    def test_rotation_replaces_token_without_changing_project_or_owner(self):
        self.post()
        before = self.store.get_owned(10, "research")
        self.ticket = self.store.issue_ticket(10, "research")
        self.assertEqual(self.post(token="new-github-secret").status_code, 200)
        after = self.store.get_owned(10, "research")
        self.assertEqual(
            (before.id, before.user_id, before.github_id),
            (after.id, after.user_id, after.github_id),
        )
        self.assertEqual(self.store.token_for(after), "new-github-secret")

    def test_configuration_removed_during_validation_cannot_be_recreated(self):
        def validate(project, token):
            self.store.remove_owned(project.user_id, project.name)
            return "P_1"

        self.validate.side_effect = validate
        self.assertEqual(self.post().status_code, 400)
        self.assertEqual(self.store.list_owned(10), [])
        self.worker.changed.assert_not_called()

    def test_github_identity_change_rejects_rotation(self):
        self.post()
        self.ticket = self.store.issue_ticket(10, "research")
        self.validate.return_value = "P_OTHER"
        self.assertEqual(self.post(token="different-secret").status_code, 400)
        project = self.store.get_owned(10, "research")
        self.assertEqual(project.github_id, "P_1")
        self.assertEqual(self.store.token_for(project), "github-secret-token")

    def test_invalid_or_cross_project_links_cannot_set_another_users_token(self):
        self.create(user=11, channel=30)
        self.assertEqual(self.post(ticket="invented-link-that-is-not-valid").status_code, 400)
        self.assertEqual(self.post().status_code, 200)
        self.assertFalse(self.store.get_owned(11, "research").authorized)


if __name__ == "__main__":
    unittest.main()
