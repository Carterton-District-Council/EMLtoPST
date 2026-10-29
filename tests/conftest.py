"""Shared fixtures. Every message here is synthetic - never put real mail in this suite."""

import shutil

import pytest

HEAD = (b"From: Alice <alice@example.com>\r\nTo: Bob <bob@example.com>\r\n"
        b"Date: Thu, 06 Feb 2025 10:30:00 +0000\r\nMessage-ID: <t@example.com>\r\n")


def multipart(*parts, subtype=b"mixed", subject=b"test"):
    """Build a multipart message from raw part bytes (headers + blank line + body)."""
    body = b"".join(b"--XX\r\n" + p + b"\r\n" for p in parts) + b"--XX--\r\n"
    return (HEAD + b"Subject: " + subject + b"\r\nMIME-Version: 1.0\r\n"
            b"Content-Type: multipart/" + subtype + b"; boundary=XX\r\n\r\n" + body)


TEXT = b"Content-Type: text/plain; charset=utf-8\r\n\r\nouter body"
HTML = b"Content-Type: text/html; charset=utf-8\r\n\r\n<p>outer body</p>"


@pytest.fixture
def readpst():
    path = shutil.which("readpst")
    if not path:
        pytest.skip("readpst (libpst / pst-utils) is not installed")
    return path
