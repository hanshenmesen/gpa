"""Public GPA cloud API. This package must never import desktop drivers."""

from gpa.cloud_server.app import create_app

__all__ = ["create_app"]
