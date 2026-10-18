"""One-use, private web forms for GitHub credentials; no tokens pass through Zulip."""

import logging
from threading import Thread

import requests
from flask import Flask, jsonify, request, send_from_directory
from waitress import create_server, wasyncore
from werkzeug.exceptions import HTTPException

from astrochat.github import GitHubClient
from astrochat.project_commands import auth_origin
from astrochat.project_store import Project, ProjectError, ProjectStore

logger = logging.getLogger(__name__)


def validate_token(project: Project, token: str) -> str:
    with requests.Session() as session:
        github = GitHubClient(token, session)
        return github.project(project.owner, project.number, project.owner_type)["id"]


class Authorization:
    def __init__(
        self, store: ProjectStore, worker, public_url: str, validate=validate_token
    ) -> None:
        self.store = store
        self.worker = worker
        self.origin = auth_origin(public_url)
        self.validate = validate

    def data(self, fields: set[str]) -> dict[str, str]:
        # JSON plus an exact Origin check blocks cross-origin form submissions.
        # The unguessable, short-lived ticket authenticates the configured owner.
        if request.headers.get("Origin") != self.origin or not request.is_json:
            raise ProjectError("Open the authorization link from your private bot conversation.")
        data = request.get_json()
        if (
            not isinstance(data, dict)
            or data.keys() != fields
            or any(not isinstance(value, str) for value in data.values())
            or not 20 <= len(data["ticket"]) <= 128
        ):
            raise ProjectError("Invalid authorization request.")
        return data

    def info(self):
        data = self.data({"ticket"})
        project = self.store.ticket_project(data["ticket"])
        return jsonify(
            name=project.name, url=project.url, channel=project.channel, user_id=project.user_id
        )

    def submit(self):
        data = self.data({"ticket", "token"})
        token = data["token"].strip()
        if not 10 <= len(token) <= 512 or any(ord(c) < 33 or ord(c) > 126 for c in token):
            raise ProjectError("Enter a valid GitHub personal access token.")
        # Consume before contacting GitHub; concurrent submissions cannot reuse the
        # link. A failed attempt requires a fresh link and leaves the old token intact.
        project = self.store.ticket_project(data["ticket"], consume=True)
        try:
            github_id = self.validate(project, token)
        except Exception as error:
            logger.warning("GitHub authorization failed (%s)", type(error).__name__)
            raise ProjectError(
                "GitHub access could not be verified. Check the token and Project permissions, "
                "then request a fresh link with project auth NAME."
            ) from None
        if project.github_id is not None and github_id != project.github_id:
            raise ProjectError(
                "GitHub Project identity changed. Remove this configuration and set it up again."
            )
        self.store.authorize(project, token, github_id)
        self.worker.changed(project.id)
        return jsonify(message="Authorization saved. Return to Zulip and use project status NAME.")


def security_headers(response):
    response.headers.update(
        {
            "Cache-Control": "no-store",
            "Referrer-Policy": "no-referrer",
            "X-Content-Type-Options": "nosniff",
            "X-Frame-Options": "DENY",
            "Content-Security-Policy": "default-src 'none'; script-src 'self'; style-src 'self'; "
            "connect-src 'self'; base-uri 'none'; frame-ancestors 'none'; "
            "form-action 'none'",
        }
    )
    return response


def create_app(authorization: Authorization) -> Flask:
    app = Flask(__name__)
    app.config["MAX_CONTENT_LENGTH"] = 4096
    app.add_url_rule(
        "/authorize", view_func=lambda: send_from_directory(app.static_folder, "authorize.html")
    )
    app.add_url_rule("/api/authorize/info", view_func=authorization.info, methods=["POST"])
    app.add_url_rule("/api/authorize", view_func=authorization.submit, methods=["POST"])
    app.after_request(security_headers)

    @app.errorhandler(ProjectError)
    def user_error(error):
        return jsonify(error=str(error)), 400

    @app.errorhandler(Exception)
    def server_error(error):
        # Never echo or log request bodies, headers, tickets or upstream error text.
        code = error.code if isinstance(error, HTTPException) else 500
        logger.warning("Authorization request failed (%s)", type(error).__name__)
        return jsonify(error="Request could not be completed. Request a new link in Zulip."), code

    return app


class AuthorizationServer:
    def __init__(self, app: Flask, port: int) -> None:
        self.channels = {}
        self.server = create_server(
            app,
            host="127.0.0.1",
            port=port,
            map=self.channels,
            threads=4,
            max_request_body_size=4096,
            channel_timeout=30,
            expose_tracebacks=False,
            asyncore_loop_timeout=0.1,
        )
        self.thread = Thread(target=self.server.run, name="project-authorization", daemon=True)

    def start(self) -> None:
        self.thread.start()

    def close(self) -> None:
        self.server.close()
        dispatcher = self.server.task_dispatcher
        dispatcher.shutdown(timeout=35)
        # Wait for in-flight validation before closing the shared database. Waitress
        # has stopped accepting new work; requests to GitHub have a 30-second timeout.
        with dispatcher.lock:
            while dispatcher.threads:
                dispatcher.thread_exit_cv.wait()
        wasyncore.close_all(map=self.channels)
        if self.thread.ident is not None:
            self.thread.join()
