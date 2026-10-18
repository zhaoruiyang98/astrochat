import hashlib
import sqlite3
import unittest
from io import BytesIO
from threading import Event
from unittest.mock import MagicMock, Mock, patch

import requests

from astrochat.github import Discussion, GitHubClient
from astrochat.image_sync import (
    ImageSync,
    ImageSyncCancelled,
    ImageSyncError,
    download_image,
    github_image,
)
from astrochat.sync_state import SyncState


SOURCE = "https://github.com/user-attachments/assets/abc123-def456"
SIGNED = "https://private-user-images.githubusercontent.com/123/image.png?jwt=secret"
UPLOAD = "/user_uploads/1/abc/image.png"
PNG = b"\x89PNG\r\n\x1a\nimage-content"


def response(status=200, data=PNG, headers=None, text=""):
    result = MagicMock()
    result.__enter__.return_value = result
    result.status_code = status
    result.headers = headers if headers is not None else {"Content-Type": "image/png"}
    result.iter_content.return_value = iter([data])
    result.text = text
    return result


class DownloadTests(unittest.TestCase):
    def setUp(self):
        self.session = MagicMock()
        self.session.__enter__.return_value = self.session
        self.factory = patch("astrochat.image_sync.requests.Session", return_value=self.session)
        self.factory.start()
        self.addCleanup(self.factory.stop)

    def test_authenticated_initial_request_and_unauthenticated_signed_redirect(self):
        first = response(302, headers={"Location": SIGNED})
        second = response()
        self.session.get.side_effect = [first, second]
        with download_image(SOURCE, "Bearer private-token") as image:
            self.assertEqual(image.read(), PNG)
            self.assertEqual(image.name, "image.png")
        initial, redirected = self.session.get.call_args_list
        self.assertEqual(initial.kwargs["headers"]["Authorization"], "Bearer private-token")
        self.assertNotIn("Authorization", redirected.kwargs["headers"])
        self.assertEqual(redirected.args, (SIGNED,))
        for call in (initial, redirected):
            self.assertFalse(call.kwargs["allow_redirects"])
            self.assertTrue(call.kwargs["stream"])
            self.assertEqual(call.kwargs["timeout"], (10, 30))
        # Exercise Requests' real preparation path: a user's .netrc must not
        # silently reintroduce credentials for the CDN after a redirect.
        with requests.sessions.Session() as transport:
            transport.auth = self.session.auth
            with patch("requests.sessions.get_netrc_auth") as netrc:
                prepared = transport.prepare_request(requests.Request("GET", SIGNED))
                self.assertNotIn("Authorization", prepared.headers)
                netrc.assert_not_called()
        self.assertEqual(self.session.cookies.clear.call_count, 2)
        first.__exit__.assert_called_once()
        second.__exit__.assert_called_once()
        self.session.__exit__.assert_called_once()

    def test_initial_cdn_request_and_same_host_redirect_never_receive_credentials(self):
        self.session.get.side_effect = [
            response(302, headers={"Location": "/123/other.png?jwt=secret2"}),
            response(),
        ]
        with download_image(SIGNED, "Bearer private-token"):
            pass
        for call in self.session.get.call_args_list:
            self.assertNotIn("Authorization", call.kwargs["headers"])

    def test_foreign_hosts_credentials_ports_and_schemes_are_not_downloaded(self):
        for url in (
            "https://127.0.0.1/image.png",
            "http://github.com/user-attachments/assets/abc",
            "https://github.com.evil.test/image.png",
            "https://github.com@evil.test/image.png",
            "https://user:password@github.com/user-attachments/assets/abc",
            "https://github.com:8443/user-attachments/assets/abc",
            "file:///etc/passwd",
            "https://github.com/login",
            "https://github.com/\nuser-attachments/assets/abc",
            "https://evil.githubusercontent.com/image.png",
            "https://github.com:bad/path",
        ):
            with self.subTest(url=url):
                self.session.get.reset_mock()
                with self.assertRaises(ImageSyncError):
                    download_image(url, "Bearer private-token")
                self.session.get.assert_not_called()
                self.assertFalse(github_image(url))

    def test_unsafe_redirect_is_rejected_before_second_request(self):
        for location in ("https://localhost/private", "http://user-images.githubusercontent.com/a"):
            self.session.get.reset_mock()
            self.session.get.side_effect = [response(302, headers={"Location": location})]
            with self.assertRaises(ImageSyncError):
                download_image(SOURCE, "Bearer private-token")
            self.assertEqual(self.session.get.call_count, 1)

    def test_redirect_limit_and_missing_location(self):
        self.session.get.return_value = response(302, headers={"Location": SIGNED})
        with self.assertRaisesRegex(ImageSyncError, "Too many"):
            download_image(SOURCE, "Bearer private-token")
        self.assertEqual(self.session.get.call_count, 6)
        self.session.get.return_value = response(302, headers={})
        with self.assertRaisesRegex(ImageSyncError, "no destination"):
            download_image(SOURCE, "Bearer private-token")

    def test_size_limit_applies_to_headers_and_stream_without_content_length(self):
        for headers in ({"Content-Length": "1000"}, {}):
            self.session.get.return_value = response(data=PNG * 4, headers=headers)
            with patch("astrochat.image_sync.MAX_IMAGE_BYTES", 32):
                with self.assertRaisesRegex(ImageSyncError, "size limit"):
                    download_image(SIGNED, "Bearer private-token")

    def test_html_json_svg_and_invalid_image_bytes_are_rejected(self):
        for content_type, data in (
            ("text/html", b"<html>login</html>"),
            ("application/json", b"{}"),
            ("image/png", b"<html>login</html>"),
            ("image/svg+xml", b"<svg/>"),
            ("application/octet-stream", b"not-an-image"),
            ("image/png", b""),
        ):
            with self.subTest(content_type=content_type, data=data):
                self.session.get.return_value = response(
                    data=data, headers={"Content-Type": content_type}
                )
                with self.assertRaises(ImageSyncError):
                    download_image(SIGNED, "Bearer private-token")

    def test_supported_binary_types_and_octet_stream_names(self):
        for data, extension in (
            (PNG, "png"),
            (b"\xff\xd8\xffimage", "jpg"),
            (b"GIF89aimage", "gif"),
            (b"RIFF1234WEBPimage", "webp"),
        ):
            self.session.get.return_value = response(
                data=data, headers={"Content-Type": "application/octet-stream"}
            )
            with download_image(SIGNED, "Bearer private-token") as image:
                self.assertEqual(image.name, f"image.{extension}")
                self.assertEqual(image.read(), data)

    def test_network_and_http_errors_do_not_expose_signed_urls(self):
        self.session.get.side_effect = requests.Timeout(f"failed {SIGNED} private-token")
        with self.assertRaises(ImageSyncError) as caught:
            download_image(SIGNED, "Bearer private-token")
        self.assertNotIn("secret", str(caught.exception))
        self.assertNotIn("private-token", str(caught.exception))
        self.assertTrue(caught.exception.__suppress_context__)
        self.session.get.side_effect = None
        for code in (401, 403, 404, 429, 500):
            self.session.get.return_value = response(code, text=SIGNED)
            with self.assertRaisesRegex(ImageSyncError, f"HTTP {code}"):
                download_image(SIGNED, "Bearer private-token")

    def test_cancellation_during_stream_and_deadline_close_response(self):
        stop = Event()
        streamed = response()

        def chunks(**kwargs):
            yield PNG
            stop.set()
            yield b"tail"

        streamed.iter_content.side_effect = chunks
        self.session.get.return_value = streamed
        with self.assertRaises(ImageSyncCancelled):
            download_image(SIGNED, "Bearer private-token", stop)
        streamed.__exit__.assert_called_once()
        self.session.get.reset_mock()
        with self.assertRaises(ImageSyncCancelled):
            download_image(SIGNED, "Bearer private-token", stop)
        self.session.get.assert_not_called()
        with patch("astrochat.image_sync.time.monotonic", side_effect=[0, 61]):
            with self.assertRaisesRegex(ImageSyncError, "time limit"):
                download_image(SIGNED, "Bearer private-token")


class ImageSyncTests(unittest.TestCase):
    def setUp(self):
        self.connection = sqlite3.connect(":memory:")
        self.addCleanup(self.connection.close)
        self.state = SyncState(self.connection)
        self.session = Mock()
        self.session.headers = {}
        self.github = GitHubClient("private-token", self.session)
        self.client = Mock()
        self.client.upload_file.return_value = {"result": "success", "url": UPLOAD}
        self.images = ImageSync(self.github, self.client, self.state)
        self.entry = Discussion(
            "I_1", f"![plot]({SOURCE})", "https://github.com/u/r/issues/1", "alice", "now", "Issue"
        )

    def test_render_authorizes_with_context_and_upload_mapping_is_reused(self):
        self.session.post.return_value = response(text=f'<p><img src="{SIGNED}"></p>')
        data = BytesIO(PNG)
        with patch("astrochat.image_sync.download_image", return_value=data) as download:
            mapping = self.images.prepare([self.entry, self.entry], "u/r#1")
        self.assertEqual(mapping, {SOURCE: UPLOAD})
        self.assertTrue(data.closed)
        download.assert_called_once_with(SIGNED, "Bearer private-token", None)
        call = self.session.post.call_args
        self.assertEqual(call.args[0], "https://api.github.com/markdown")
        self.assertEqual(call.kwargs["json"]["context"], "u/r")
        self.assertEqual(call.kwargs["json"]["mode"], "gfm")
        self.assertIn(SOURCE, call.kwargs["json"]["text"])
        self.assertFalse(call.kwargs["allow_redirects"])
        key = hashlib.sha256(SOURCE.encode()).hexdigest()
        self.assertEqual(self.state.image_url(key), UPLOAD)
        changes = self.connection.total_changes
        self.assertEqual(self.images.prepare([self.entry], "u/r#1"), mapping)
        self.client.upload_file.assert_called_once()
        self.session.post.assert_called_once()
        self.assertEqual(self.connection.total_changes, changes)
        self.assertNotIn("secret", str(self.connection.execute("SELECT * FROM images").fetchall()))

    def test_direct_cdn_images_do_not_need_render_api_and_legacy_uri_works(self):
        self.entry = Discussion(
            "I_1",
            "![image](https://user-images.githubusercontent.com/1/a.png)",
            "",
            "a",
            "now",
            "Issue",
        )
        self.client.upload_file.return_value = {"result": "success", "uri": UPLOAD}
        with patch("astrochat.image_sync.download_image", return_value=BytesIO(PNG)):
            mapping = self.images.prepare([self.entry], "draft")
        self.assertEqual(list(mapping.values()), [UPLOAD])
        self.session.post.assert_not_called()

    def test_private_image_flows_from_authorized_render_through_download_to_upload(self):
        self.session.post.return_value = response(text=f'<img src="{SIGNED}">')
        transport = MagicMock()
        transport.__enter__.return_value = transport
        transport.get.return_value = response()
        uploaded_bytes = []

        def upload(image):
            uploaded_bytes.append((image.name, image.read()))
            return {"result": "success", "url": UPLOAD}

        self.client.upload_file.side_effect = upload
        with patch("astrochat.image_sync.requests.Session", return_value=transport):
            self.assertEqual(self.images.prepare([self.entry], "u/r#1"), {SOURCE: UPLOAD})
        self.assertEqual(uploaded_bytes, [("image.png", PNG)])
        self.assertNotIn("Authorization", transport.get.call_args.kwargs["headers"])
        self.assertEqual(self.session.headers["Authorization"], "Bearer private-token")
        self.assertEqual(self.images.prepare([self.entry], "u/r#1"), {SOURCE: UPLOAD})
        self.client.upload_file.assert_called_once()
        transport.get.assert_called_once()

    def test_third_party_images_plain_links_and_code_do_not_trigger_network(self):
        self.entry = Discussion(
            "I_1",
            f"[file]({SOURCE}) ![outside](https://example.com/image.png)\n\n```md\n![code]({SOURCE})\n```",
            "",
            "a",
            "now",
            "Issue",
        )
        self.assertEqual(self.images.prepare([self.entry], "u/r#1"), {})
        self.session.post.assert_not_called()
        self.client.upload_file.assert_not_called()

    def test_failed_download_or_upload_does_not_record_mapping(self):
        self.session.post.return_value = response(text=f'<img src="{SIGNED}">')
        with patch(
            "astrochat.image_sync.download_image", side_effect=ImageSyncError("download failed")
        ):
            with self.assertRaises(ImageSyncError):
                self.images.prepare([self.entry], "u/r#1")
        self.client.upload_file.assert_not_called()
        self.client.upload_file.return_value = {"result": "error", "msg": SIGNED}
        with patch("astrochat.image_sync.download_image", return_value=BytesIO(PNG)):
            with self.assertRaises(ImageSyncError) as caught:
                self.images.prepare([self.entry], "u/r#1")
        self.assertNotIn("secret", str(caught.exception))
        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM images").fetchone()[0], 0)

    def test_render_errors_do_not_upload_or_leak_response_body(self):
        for result in (response(403, text=SIGNED), response(text="<p>not accessible</p>")):
            self.session.post.return_value = result
            with self.assertRaises(ImageSyncError) as caught:
                self.images.prepare([self.entry], "u/r#1")
            self.assertNotIn("secret", str(caught.exception))
        self.session.post.side_effect = requests.ConnectionError("private-token")
        with self.assertRaises(ImageSyncError) as caught:
            self.images.prepare([self.entry], "u/r#1")
        self.assertNotIn("private-token", str(caught.exception))
        self.client.upload_file.assert_not_called()

    def test_dry_run_uses_cache_and_placeholders_without_network_or_state_writes(self):
        self.state.dry_run = True
        changes = self.connection.total_changes
        mapping = self.images.prepare([self.entry], "u/r#1")
        self.assertTrue(mapping[SOURCE].startswith("/user_uploads/astrochat-preview/"))
        self.assertEqual(self.connection.total_changes, changes)
        self.client.upload_file.assert_not_called()
        self.session.post.assert_not_called()
        self.state.dry_run = False
        self.state.save_image(hashlib.sha256(SOURCE.encode()).hexdigest(), UPLOAD)
        self.state.dry_run = True
        self.assertEqual(self.images.prepare([self.entry], "u/r#1"), {SOURCE: UPLOAD})

    def test_successful_upload_is_checkpointed_even_if_cancelled_before_message(self):
        stop = Event()
        self.session.post.return_value = response(text=f'<img src="{SIGNED}">')

        def upload(image):
            stop.set()
            return {"result": "success", "url": UPLOAD}

        self.client.upload_file.side_effect = upload
        with patch("astrochat.image_sync.download_image", return_value=BytesIO(PNG)):
            self.assertEqual(self.images.prepare([self.entry], "u/r#1", stop), {SOURCE: UPLOAD})
        self.assertEqual(self.state.image_url(hashlib.sha256(SOURCE.encode()).hexdigest()), UPLOAD)

    def test_same_source_in_another_database_is_uploaded_separately(self):
        key = hashlib.sha256(SOURCE.encode()).hexdigest()
        self.state.save_image(key, UPLOAD)
        with sqlite3.connect(":memory:") as connection:
            other = SyncState(connection)
            self.assertIsNone(other.image_url(key))
            images = ImageSync(self.github, self.client, other)
            with patch.object(images, "upload", return_value="/user_uploads/2/new.png") as upload:
                self.assertEqual(
                    images.prepare([self.entry], "u/r#1"), {SOURCE: "/user_uploads/2/new.png"}
                )
                upload.assert_called_once()
            self.assertEqual(self.state.image_url(key), UPLOAD)


if __name__ == "__main__":
    unittest.main()
