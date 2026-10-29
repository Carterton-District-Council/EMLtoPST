"""eml_parser: nothing in a message may be silently discarded."""

import pytest

from eml2pst.eml_parser import parse_eml_bytes
from eml2pst.utils import datetime_to_filetime
from conftest import HEAD, HTML, TEXT, multipart

FORWARDED = (b"Content-Type: message/rfc822\r\n\r\n"
             b"From: Carol <carol@example.com>\r\nSubject: inner subject\r\n"
             b"Date: Mon, 03 Feb 2025 09:00:00 +0000\r\n"
             b"Content-Type: text/plain\r\n\r\nFORWARDED TEXT")


def by_type(parsed):
    return {a["mime_type"]: a for a in parsed["attachments"]}


def test_forwarded_message_is_attached_whole():
    parsed = parse_eml_bytes(multipart(TEXT, FORWARDED))
    assert parsed["body_text"] == "outer body"
    att = by_type(parsed)["message/rfc822"]
    assert att["filename"] == "inner subject.eml"
    # The forwarded message's own headers survive, not just its body.
    assert b"From: Carol <carol@example.com>" in att["data"]
    assert b"FORWARDED TEXT" in att["data"]


def test_forwarded_message_attachments_stay_inside_it():
    inner_pdf = (b"Content-Type: message/rfc822\r\n\r\nSubject: has pdf\r\nMIME-Version: 1.0\r\n"
                 b"Content-Type: multipart/mixed; boundary=YY\r\n\r\n"
                 b"--YY\r\nContent-Type: text/plain\r\n\r\ninner\r\n"
                 b"--YY\r\nContent-Type: application/pdf\r\nContent-Disposition: attachment; "
                 b"filename=x.pdf\r\n\r\n%PDF\r\n--YY--\r\n")
    parsed = parse_eml_bytes(multipart(TEXT, inner_pdf))
    assert [a["mime_type"] for a in parsed["attachments"]] == ["message/rfc822"]
    assert b"filename=x.pdf" in parsed["attachments"][0]["data"]


@pytest.mark.parametrize("part, mime, filename", [
    (b"Content-Type: text/calendar; method=REQUEST\r\n\r\nBEGIN:VCALENDAR\r\nEND:VCALENDAR",
     "text/calendar", "attachment_0.ics"),
    (b"Content-Type: audio/wav\r\nContent-Disposition: inline; filename=v.wav\r\n\r\nRIFF",
     "audio/wav", "v.wav"),
    (b"Content-Type: video/mp4\r\n\r\nMP4", "video/mp4", "attachment_0.mp4"),
    (b"Content-Type: text/plain\r\n\r\nSECOND PART", "text/plain", "attachment_0.txt"),
    (b"Content-Type: text/csv\r\nContent-Disposition: inline; filename=a.csv\r\n\r\na,b",
     "text/csv", "a.csv"),
    (b"Content-Type: message/delivery-status\r\n\r\nReporting-MTA: dns; x\r\n\r\nStatus: 5.0.0",
     "message/delivery-status", "attachment_0.txt"),
])
def test_parts_upstream_dropped_become_attachments(part, mime, filename):
    parsed = parse_eml_bytes(multipart(TEXT, part))
    assert parsed["body_text"] == "outer body"
    att = by_type(parsed)[mime]
    assert att["filename"] == filename
    assert att["size"] > 0
    assert parsed["has_attachments"]


def test_meeting_invite_keeps_bodies_and_calendar():
    cal = b"Content-Type: text/calendar; method=REQUEST\r\n\r\nBEGIN:VCALENDAR\r\nEND:VCALENDAR"
    parsed = parse_eml_bytes(multipart(TEXT, HTML, cal, subtype=b"alternative"))
    assert parsed["body_text"] == "outer body"
    assert parsed["body_html"] == "<p>outer body</p>"
    assert b"BEGIN:VCALENDAR" in by_type(parsed)["text/calendar"]["data"]


def test_ordinary_message_is_unchanged():
    pdf = b"Content-Type: application/pdf\r\nContent-Disposition: attachment; filename=r.pdf\r\n\r\n%PDF"
    img = b"Content-Type: image/png\r\n\r\nPNG"
    parsed = parse_eml_bytes(multipart(TEXT, HTML, pdf, img))
    assert [a["filename"] for a in parsed["attachments"]] == ["r.pdf", "attachment_1.png"]


def test_encoded_words_are_decoded():
    raw = (b"From: =?utf-8?q?M=C4=81ori_User?= <m@example.com>\r\n"
           b"To: =?utf-8?b?VMSBbmUs?= <t@example.com>, Bob <bob@example.com>\r\n"
           b"Subject: =?utf-8?b?SMOpbGxv?= =?utf-8?q?_w=C5=8Drld?=\r\n"
           b"Date: Thu, 06 Feb 2025 10:30:00 +0000\r\nMIME-Version: 1.0\r\n"
           b"Content-Type: application/pdf\r\n"
           b"Content-Disposition: attachment; filename=\"=?utf-8?b?csOpc3Vtw6kucGRm?=\"\r\n\r\nx")
    parsed = parse_eml_bytes(raw)
    assert parsed["subject"] == "Héllo wōrld"
    assert parsed["sender_name"] == "Māori User"
    # The encoded name contains a comma; splitting before decoding keeps both recipients.
    assert [(r["name"], r["email"]) for r in parsed["recipients"]] == [
        ("Tāne,", "t@example.com"), ("Bob", "bob@example.com")]
    assert parsed["attachments"][0]["filename"] == "résumé.pdf"


@pytest.mark.parametrize("charset", [b"nosuchcharset", b"\x80", b"\x00"])
def test_malformed_encoded_word_is_left_as_found(charset):
    """The \\x80 and \\x00 cases were found by fuzzing: they raised instead."""
    word = b"=?" + charset + b"?q?x?="
    raw = multipart(TEXT, b"Content-Type: image/png; name=\"" + word + b"\"\r\n\r\nPNG",
                    subject=word)
    parsed = parse_eml_bytes(raw)
    # Kept undecoded (a raw 8-bit byte in a header reads back as U+FFFD, as before).
    assert parsed["subject"].startswith("=?") and parsed["subject"].endswith("?q?x?=")
    assert len(parsed["attachments"]) == 1


def test_nul_in_rfc2231_filename_charset_still_converts():
    """Found by fuzzing; upstream raised ValueError and lost the whole message."""
    part = b"Content-Type: application/pdf\r\nContent-Disposition: attachment; filename*=\x00''x.pdf\r\n\r\n%PDF"
    parsed = parse_eml_bytes(multipart(TEXT, part))
    assert parsed["attachments"][0]["filename"] == "attachment_0.pdf"


def test_nul_in_body_charset_still_converts():
    """Found by fuzzing; upstream raised ValueError and lost the whole message."""
    part = b"Content-Type: text/plain; charset=\"a\x00b\"\r\n\r\nhello"
    assert parse_eml_bytes(multipart(part))["body_text"] == "hello"


def test_unsafe_attachment_filename_is_neutralised():
    part = (b"Content-Type: application/octet-stream\r\n"
            b"Content-Disposition: attachment; filename=\"..\\\\..\\\\evil:name?.exe\"\r\n\r\nx")
    name = parse_eml_bytes(multipart(TEXT, part))["attachments"][0]["filename"]
    assert not any(c in name for c in '\\/:?*"<>|')


@pytest.mark.parametrize("date", [b"garbage", b"Thu, 31 Feb 2025 10:00:00 +0000"])
def test_bad_date_falls_back_to_received(date):
    from datetime import datetime, timezone
    raw = (b"Received: from a by b; Mon, 03 Feb 2025 09:00:00 +0000\r\n"
           b"From: a@example.com\r\nDate: " + date + b"\r\nSubject: s\r\n\r\nbody")
    parsed = parse_eml_bytes(raw)
    assert parsed["delivery_time"] == datetime_to_filetime(datetime(2025, 2, 3, 9, tzinfo=timezone.utc))


def test_bad_date_without_received_still_converts():
    parsed = parse_eml_bytes(b"From: a@example.com\r\nDate: garbage\r\nSubject: s\r\n\r\nbody")
    assert parsed["delivery_time"] > 0


def test_deep_nesting_raises_rather_than_hangs():
    depth = 2000
    raw = HEAD + b"Subject: deep\r\nMIME-Version: 1.0\r\nContent-Type: multipart/mixed; boundary=B0\r\n\r\n"
    for i in range(1, depth):
        raw += b"--B%d\r\nContent-Type: multipart/mixed; boundary=B%d\r\n\r\n" % (i - 1, i)
    raw += b"--B%d\r\nContent-Type: text/plain\r\n\r\nx\r\n" % (depth - 1)
    try:
        parse_eml_bytes(raw)
    except RecursionError:
        pass  # rejected as one failed message, which the CLI reports and exits 2 for
