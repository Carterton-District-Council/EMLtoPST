"""Build real PSTs and read them back - with our own verifier and with readpst (libpst)."""

import base64
import json
import mailbox
import os
import subprocess
import sys
from email import message_from_bytes
from email.header import decode_header, make_header
from pathlib import Path

import pytest

from eml2pst.eml_parser import parse_eml_bytes
from eml2pst.errors import PSTWriteError
from eml2pst.pst_file import PSTFileBuilder
from eml2pst.verify import reconcile
from conftest import HEAD, TEXT, multipart

REPO = Path(__file__).resolve().parent.parent


def run_cli(*args, stdin=None):
    return subprocess.run([sys.executable, "-m", "eml2pst", *map(str, args)], cwd=REPO,
                          input=stdin, capture_output=True, text=True)


def read_mbox_messages(readpst, pst, out):
    out.mkdir()
    subprocess.run([readpst, "-q", "-o", str(out), str(pst)], check=True)
    msgs = {}
    for path in out.rglob("*"):
        if path.is_file():
            msgs[path.relative_to(out).as_posix()] = list(mailbox.mbox(str(path)))
    return msgs


def test_cli_converts_sample_tree(tmp_path, readpst):
    pst = tmp_path / "out.pst"
    result = run_cli("test_emails", "-o", pst)
    assert result.returncode == 0, result.stderr
    assert "verified by reading the PST back" in result.stderr
    msgs = read_mbox_messages(readpst, pst, tmp_path / "x")
    assert {k: len(v) for k, v in msgs.items()} == {"Inbox.mbox": 2, "Sent.mbox": 1}


def test_cli_puts_folders_under_the_ipm_subtree(tmp_path):
    from eml2pst.cli import _process_directory
    import collections
    builder = PSTFileBuilder(tmp_path / "o.pst")
    stats = {"folders": 0, "messages": 0, "errors": 0, "per_folder": collections.Counter()}
    _process_directory(builder, REPO / "test_emails", stats)
    tops = [f for f in builder._folders.values() if f["name"] in ("Inbox", "Sent")]
    assert tops and all(f["parent_nid"] == builder._ipm_subtree_nid for f in tops)


def test_cli_exits_2_when_a_message_fails(tmp_path):
    good = base64.b64encode(HEAD + b"Subject: ok\r\n\r\nbody").decode()
    lines = [json.dumps({"folder": "A", "eml": good}), "not json", json.dumps({"folder": "A"})]
    result = run_cli("--stdin", "-o", tmp_path / "o.pst", stdin="\n".join(lines))
    assert result.returncode == 2
    assert "FAILED" in result.stderr


def test_cli_usage_error_is_not_mistaken_for_do_not_use():
    assert run_cli().returncode == 1
    assert run_cli("--no-such-option").returncode == 1


def test_readme_library_example(tmp_path):
    builder = PSTFileBuilder(tmp_path / "mailbox.pst", display_name="Archive")
    inbox = builder.add_folder("Inbox")
    builder.add_message(inbox, parse_eml_bytes(HEAD + b"Subject: s\r\n\r\nbody"))
    builder.write()
    check = reconcile(tmp_path / "mailbox.pst", {inbox: 1})
    assert check["ok"], check["problems"]


def test_cli_survives_symlink_loop(tmp_path):
    tree = tmp_path / "tree" / "Inbox"
    tree.mkdir(parents=True)
    (tree / "m.eml").write_bytes(HEAD + b"Subject: s\r\n\r\nbody")
    os.symlink(tmp_path / "tree", tree / "loop")
    result = run_cli(tmp_path / "tree", "-o", tmp_path / "o.pst")
    assert result.returncode == 0, result.stderr


def test_large_attachment_and_unicode_round_trip(tmp_path, readpst):
    """A >8 MB attachment needs a two-level XXBLOCK tree (PATCHES.md patch 2)."""
    blob = os.urandom(9 * 1024 * 1024)
    att = (b"Content-Type: application/octet-stream\r\nContent-Transfer-Encoding: base64\r\n"
           b"Content-Disposition: attachment; filename=big.bin\r\n\r\n"
           + base64.encodebytes(blob))
    raw = multipart(TEXT, att, subject=b"=?utf-8?b?SMOpbGxvIHfFjXJsZA==?=")
    pst = tmp_path / "big.pst"
    builder = PSTFileBuilder(pst)
    folder = builder.add_folder("Big")
    builder.add_message(folder, parse_eml_bytes(raw))
    builder.write()
    assert reconcile(pst, {folder: 1})["ok"]

    [msg] = read_mbox_messages(readpst, pst, tmp_path / "x")["Big.mbox"]
    assert str(make_header(decode_header(msg["Subject"]))) == "Héllo wōrld"
    parts = [p for p in msg.walk() if p.get_filename() == "big.bin"]
    assert parts and parts[0].get_payload(decode=True) == blob


def test_previously_dropped_parts_reach_the_pst(tmp_path, readpst):
    cal = b"Content-Type: text/calendar\r\n\r\nBEGIN:VCALENDAR\r\nEND:VCALENDAR"
    fwd = (b"Content-Type: message/rfc822\r\n\r\nFrom: c@example.com\r\nSubject: inner\r\n"
           b"Content-Type: text/plain\r\n\r\nFORWARDED TEXT")
    pst = tmp_path / "p.pst"
    builder = PSTFileBuilder(pst)
    folder = builder.add_folder("F")
    builder.add_message(folder, parse_eml_bytes(multipart(TEXT, cal, fwd)))
    builder.write()
    [msg] = read_mbox_messages(readpst, pst, tmp_path / "x")["F.mbox"]
    # readpst writes an attached .eml back out as a *base64-encoded*
    # message/rfc822 part. The email package parses that as a sub-message whose
    # body is the base64 text, so decode it by hand.
    def content(part):
        if part.is_multipart():
            return base64.b64decode(part.as_bytes().split(b"\n\n", 1)[1])
        return part.get_payload(decode=True)
    names = {p.get_filename(): content(p) for p in msg.walk() if p.get_filename()}
    assert b"BEGIN:VCALENDAR" in names["attachment_0.ics"]
    assert b"FORWARDED TEXT" in names["inner.eml"]


def test_many_messages_reconcile(tmp_path):
    """Enough rows that a folder's Contents Table spans several blocks (patch 14)."""
    pst = tmp_path / "many.pst"
    builder = PSTFileBuilder(pst)
    a, b = builder.add_folder("A"), builder.add_folder("B")
    for i in range(1500):
        raw = HEAD + b"Subject: message %d\r\n\r\nbody %d" % (i, i)
        builder.add_message(a if i % 3 else b, parse_eml_bytes(raw))
    builder.write()
    result = reconcile(pst, {a: 1000, b: 500})
    assert result["ok"], result["problems"]


def test_reconcile_detects_a_missing_message(tmp_path):
    pst = tmp_path / "one.pst"
    builder = PSTFileBuilder(pst)
    folder = builder.add_folder("A")
    builder.add_message(folder, parse_eml_bytes(HEAD + b"Subject: s\r\n\r\nb"))
    builder.write()
    result = reconcile(pst, {folder: 2})
    assert not result["ok"]
    assert any("1 in the PST" in p for p in result["problems"])


def test_invariants_survive_python_O():
    """These were asserts upstream; python -O used to strip them silently."""
    code = ("from eml2pst.ndb.block import pack_block\n"
            "from eml2pst.errors import PSTWriteError\n"
            "try:\n    pack_block(b'x' * 9000, 4, 0)\nexcept PSTWriteError:\n    print('raised')\n")
    out = subprocess.run([sys.executable, "-O", "-c", code], cwd=REPO, capture_output=True, text=True)
    assert out.stdout.strip() == "raised", out.stderr


def test_failed_verification_leaves_no_pst_under_the_requested_name(tmp_path, monkeypatch):
    import eml2pst.cli as cli
    monkeypatch.setattr(cli, "reconcile", lambda *a, **k: {"ok": False, "problems": ["forced"]})
    monkeypatch.setattr(sys, "argv", ["eml2pst", str(REPO / "test_emails"), "-o", str(tmp_path / "o.pst")])
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 2
    assert not (tmp_path / "o.pst").exists()
    assert (tmp_path / "o.pst.partial").exists()
