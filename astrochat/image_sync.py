"""Copy GitHub attachment images to Zulip, retaining only stable upload mappings."""

import hashlib
import html
import re
import time
from html.parser import HTMLParser
from io import BytesIO
from threading import Event
from urllib.parse import urldefrag, urljoin, urlsplit

import requests
import zulip

from astrochat.github import Discussion, GitHubClient
from astrochat.markdown import markdown_images
from astrochat.sync_state import SyncState

MAX_IMAGE_BYTES = 10 * 1024 * 1024
MAX_REDIRECTS = 5
IMAGE_HOSTS = {"user-images.githubusercontent.com", "private-user-images.githubusercontent.com"}
DOWNLOAD_HOSTS = IMAGE_HOSTS | {
    "github.com",
    "github-production-user-asset-6210df.s3.amazonaws.com",
    "github-cloud.s3.amazonaws.com",
}
ASSET_PATH = re.compile(
    r"/(?:user-attachments/assets/[a-fA-F0-9-]+"
    r"|[\w.-]+/[\w.-]+/assets/\d+/[a-fA-F0-9-]+"
    r"|(?:user-attachments|[\w.-]+/[\w.-]+)/files/\d+/[^/]+)"
)


class ImageSyncError(RuntimeError):
    """Safe to log: no PAT, signed URL, or upstream response body."""


class ImageSyncCancelled(Exception):
    pass


class _NoAmbientAuth(requests.auth.AuthBase):
    def __call__(self, request):
        # An explicit auth handler prevents Requests from consulting .netrc.
        # Network proxy and CA settings can still come from the environment.
        return request


def check_cancelled(stop: Event | None) -> None:
    if stop is not None and stop.is_set():
        raise ImageSyncCancelled()


def _https_host(url: str) -> str | None:
    # Both the initial source and every redirect are untrusted input. An exact
    # host allowlist keeps downloads away from arbitrary websites and LAN hosts.
    if re.search(r"[\x00-\x20\\<>]", url):
        return None
    try:
        parts = urlsplit(url)
        if parts.scheme == "https" and parts.port in (None, 443) and parts.username is None:
            return parts.hostname
    except ValueError:
        pass
    return None


def github_image(url: str) -> bool:
    host = _https_host(url)
    return host in IMAGE_HOSTS or (
        host == "github.com" and ASSET_PATH.fullmatch(urlsplit(url).path) is not None
    )


class _RenderedImage(HTMLParser):
    def __init__(self):
        super().__init__()
        self.sources: list[str] = []

    def handle_starttag(self, tag, attrs):
        source = dict(attrs).get("src")
        if tag == "img" and source:
            self.sources.append(source)


def _image_extension(data: bytes) -> str:
    # Do not upload an HTML login/error page, even when mislabeled image/png.
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if data.startswith(b"\xff\xd8\xff"):
        return "jpg"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "gif"
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "webp"
    raise ImageSyncError("Unsupported image data; expected PNG, JPEG, GIF or WebP")


def _read_image(response, deadline: float, stop: Event | None) -> BytesIO:
    content_type = response.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
    if content_type not in {
        "image/png",
        "image/jpeg",
        "image/gif",
        "image/webp",
        "application/octet-stream",
        "",
    }:
        raise ImageSyncError("GitHub did not return a supported image; check token access")
    length = response.headers.get("Content-Length", "")
    if length.isdecimal() and int(length) > MAX_IMAGE_BYTES:
        raise ImageSyncError("GitHub image exceeds the 10 MiB size limit")
    image = BytesIO()
    for chunk in response.iter_content(chunk_size=64 * 1024):
        check_cancelled(stop)
        if time.monotonic() > deadline:
            raise ImageSyncError("GitHub image download exceeded its time limit")
        if image.tell() + len(chunk) > MAX_IMAGE_BYTES:
            raise ImageSyncError("GitHub image exceeds the 10 MiB size limit")
        image.write(chunk)
    extension = _image_extension(image.getvalue())
    image.name = f"image.{extension}"
    image.seek(0)
    return image


def download_image(url: str, authorization: str, stop: Event | None = None) -> BytesIO:
    deadline = time.monotonic() + 60
    try:
        # This session never inherits the authenticated GraphQL session, cookies,
        # or .netrc credentials. PATs go only to the initial
        # github.com attachment endpoint, never to a redirected CDN request.
        with requests.Session() as session:
            session.auth = _NoAmbientAuth()
            for hop in range(MAX_REDIRECTS + 1):
                check_cancelled(stop)
                host = _https_host(url)
                if host not in DOWNLOAD_HOSTS or (host == "github.com" and not github_image(url)):
                    raise ImageSyncError("GitHub image redirected to an unsupported address")
                if time.monotonic() > deadline:
                    raise ImageSyncError("GitHub image download exceeded its time limit")
                headers = {"Accept": "image/*", "User-Agent": "astrochat"}
                if hop == 0 and host == "github.com":
                    headers["Authorization"] = authorization
                session.cookies.clear()
                with session.get(
                    url, headers=headers, stream=True, allow_redirects=False, timeout=(10, 30)
                ) as response:
                    if response.status_code in {301, 302, 303, 307, 308}:
                        location = response.headers.get("Location")
                        if not location:
                            raise ImageSyncError("GitHub image redirect has no destination")
                        url = urljoin(url, location)
                        continue
                    if response.status_code != 200:
                        raise ImageSyncError(
                            f"GitHub image download failed (HTTP {response.status_code})"
                        )
                    return _read_image(response, deadline, stop)
    except requests.RequestException:
        # Requests exception text may contain the temporary signed URL.
        raise ImageSyncError("GitHub image download failed due to a network error") from None
    raise ImageSyncError("Too many GitHub image redirects")


class ImageSync:
    def __init__(self, github: GitHubClient, client: zulip.Client, state: SyncState):
        self.github = github
        self.client = client
        self.state = state

    def prepare(
        self, entries: list[Discussion], reference: str, stop: Event | None = None
    ) -> dict[str, str]:
        urls: set[str] = set()
        for entry in entries:
            check_cancelled(stop)
            urls.update(markdown_images(entry.body, entry.url))
        replacements = {}
        for url in sorted(urls):
            check_cancelled(stop)
            if not github_image(url):
                continue
            source = urldefrag(url)[0]
            key = hashlib.sha256(source.encode()).hexdigest()
            uploaded = self.state.image_url(key)
            if uploaded is None:
                if self.state.dry_run:
                    # A hypothetical URL lets preview report message edits while
                    # doing no image downloads, uploads or checkpoint writes.
                    uploaded = f"/user_uploads/astrochat-preview/{key}/image.png"
                else:
                    uploaded = self.upload(source, reference, stop)
                    self.state.save_image(key, uploaded)
            replacements[url] = uploaded
        return replacements

    def resolve(self, source: str, reference: str) -> str:
        if _https_host(source) != "github.com":
            return source
        # GitHub's GFM renderer resolves private attachment URLs to short-lived
        # signed URLs. Use the repository context when this is an Issue or PR.
        body = {"text": f'<img src="{html.escape(source, quote=True)}">', "mode": "gfm"}
        repository = reference.partition("#")[0]
        if re.fullmatch(r"[\w.-]+/[\w.-]+", repository):
            body["context"] = repository
        try:
            with self.github.session.post(
                "https://api.github.com/markdown", json=body, timeout=30, allow_redirects=False
            ) as response:
                if response.status_code != 200:
                    raise ImageSyncError(
                        f"GitHub image authorization failed (HTTP {response.status_code})"
                    )
                parser = _RenderedImage()
                parser.feed(response.text)
                parser.close()
        except requests.RequestException:
            raise ImageSyncError(
                "GitHub image authorization failed due to a network error"
            ) from None
        if len(parser.sources) != 1:
            raise ImageSyncError("GitHub could not resolve the image; check token access")
        return parser.sources[0]

    def upload(self, source: str, reference: str, stop: Event | None) -> str:
        check_cancelled(stop)
        url = self.resolve(source, reference)
        authorization = self.github.session.headers["Authorization"]
        with download_image(url, authorization, stop) as image:
            check_cancelled(stop)
            try:
                response = self.client.upload_file(image)
            except requests.RequestException:
                raise ImageSyncError("Zulip image upload failed due to a network error") from None
        if response.get("result") != "success":
            raise ImageSyncError("Zulip image upload failed; check upload permissions and quota")
        uploaded = response.get("url", response.get("uri"))
        if not isinstance(uploaded, str) or not uploaded.startswith("/user_uploads/"):
            raise ImageSyncError("Zulip image upload returned an invalid attachment URL")
        return uploaded
