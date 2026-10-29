# eml2pst

Convert `.eml` email files into an Outlook PST file, in pure Python with no dependencies. It
writes the Unicode PST format directly from the [MS-PST open
specification](https://learn.microsoft.com/en-us/openspecs/office_file_formats/ms-pst/), so it
needs no Outlook, no Windows and no commercial library.

This is a fork of [igrbtn/EMLtoPST](https://github.com/igrbtn/EMLtoPST), maintained by
Carterton District Council. We tried to contact the upstream author about these changes but
had no reply, so we are publishing them here under the same MIT licence. Thanks to the original
author for the format implementation this is built on.

## What this fork changes

Upstream produced files that Outlook reported as corrupt and that Microsoft 365 refused to
import. We needed to migrate a large email archive into Microsoft 365, so we fixed the writer
until its output was accepted. There are 50 fixes in all, listed with the evidence for each
in [PATCHES.md](PATCHES.md). In summary:

- **PST structure.** Allocation maps, B-tree signatures, table layouts, block alignment and the
  store objects Outlook and Microsoft 365 require (IPM subtree, wastebasket, search folders,
  templates). Checked with Outlook's Inbox Repair Tool (scanpst) and byte-by-byte against a PST
  that Outlook wrote.
- **Silent message loss.** Three defects let `add_message()` accept a message that never
  appeared in the file. Every PST is now read back and counted (`eml2pst/verify.py`).
- **Large attachments.** Values over about 8 MB crashed the writer.
- **MIME handling.** Upstream dropped calendar invites, forwarded emails, audio, video and extra
  text parts without warning. Every part is now kept: as a body or as an attachment.
- **Headers.** Encoded subjects and names (`=?utf-8?b?...?=`) are decoded. A bad `Date:`
  header or charset no longer loses the message.
- **Scale.** Data is written to disk as it is added, so memory stays bounded on large
  mailboxes.

## Status and evidence

The PST writer was used in production for an archive migration: 983 PSTs (about 3 TB, over 6
million messages, including files well above 2 GB) were built and imported into Microsoft 365, and
every message was accounted for by read-back counts at each step.

**Be aware of what that does and does not cover:**

- The production run used the parser *before* fixes 43–50. The MIME and header fixes are newer.
  They are tested against synthetic mail, fuzzing, and a parser comparison on 58,106 real
  messages, but PSTs containing the newly kept attachment types (`.eml`, `.ics`, delivery
  reports) have **not yet been through a Microsoft 365 import**.
- Forwarded messages are stored as attached `.eml` files, not as embedded Outlook items.
- Output is unencrypted (`bCryptMethod = 0`). See [SECURITY.md](SECURITY.md).
- Only the Unicode PST format is written. Older ANSI PST files are not supported.

See the end of [PATCHES.md](PATCHES.md) for the full list of what is not yet verified.

## Install

Requires Python 3.9 or later.

```bash
git clone https://github.com/Carterton-District-Council/EMLtoPST.git
cd EMLtoPST
pip install .
```

Or run it without installing: `python -m eml2pst ...` from the repository root.

## Usage

### Directory mode

```bash
eml2pst mail-export/ -o mailbox.pst
```

Subdirectories become PST folders:

```
mail-export/
  Inbox/
    message1.eml
    message2.eml
  Sent/
    reply.eml
```

produces a PST with `Inbox` (2 messages) and `Sent` (1 message). `.eml` files directly in
`mail-export/` go in the top-level folder. Symlinked directories are skipped.

### Stdin mode (JSONL)

For feeding messages from another program, pipe one JSON object per line:

```bash
your_exporter | eml2pst --stdin -o mailbox.pst
```

```json
{"folder": "Inbox", "eml": "<base64-encoded EML>"}
{"folder": "Inbox/Projects", "eml": "<base64-encoded EML>"}
{"folder": "Sent Items", "eml_file": "/path/to/message.eml"}
```

Nested folders are created from the path. `eml_file` opens whatever path it is given, so only
feed this mode JSONL you trust.

### Options

| Option | Description |
|---|---|
| `input_dir` | Directory of `.eml` files (subdirectories become folders) |
| `--stdin` | Read JSONL from stdin instead |
| `-o, --output` | Output PST path (default `output.pst`). An existing file is overwritten. |
| `-n, --name` | Display name for the PST (default `Personal Folders`) |

### Exit codes

| Code | Meaning |
|---|---|
| 0 | Every message converted, and the PST read back with the expected counts |
| 1 | Usage error |
| 2 | **Do not use the output.** Some messages failed to convert, or the read-back disagreed |

The PST is built as `<output>.partial` and only renamed to `<output>` once it has been read back
and verified. In scripts, check the exit code rather than the log output.

### As a library

```python
from eml2pst.eml_parser import parse_eml_file
from eml2pst.pst_file import PSTFileBuilder
from eml2pst.verify import reconcile

builder = PSTFileBuilder("mailbox.pst", display_name="Archive")
inbox = builder.add_folder("Inbox")
builder.add_message(inbox, parse_eml_file("message1.eml"))
builder.write()

check = reconcile("mailbox.pst", {inbox: 1})
assert check["ok"], check["problems"]
```

Always reconcile. The builder's own count cannot detect a message that went missing inside it.

## Importing into Microsoft 365

What we learned importing these files in production:

- **Do not open a PST in Outlook before importing it.** Outlook modifies the file, so the import
  no longer tests what you built.
- **Microsoft 365 removes duplicates by import source, not by Message-ID.** Re-importing the
  same PST file to the same mailbox is safe. Rebuilding the PST from the same mail, or
  importing it to a different mailbox, creates duplicates.
- A file that Outlook opens can still be rejected by the Microsoft 365 importer. They check
  different things.

## Architecture

The code mirrors the three layers of the MS-PST specification:

```
eml2pst/
├── ndb/          # Node database: blocks, B-trees, allocation maps
├── ltp/          # Lists, tables and properties: heap, BTH, property and table contexts
├── messaging/    # Store, folders, messages, special store objects
├── mapi/         # MAPI property tags and types
├── eml_parser.py # EML parsing and MAPI property mapping
├── pst_file.py   # PST assembly and streaming file writer
├── verify.py     # Independent read-back and reconciliation
├── crc.py        # MS-PST CRC-32
└── cli.py        # Command-line entry point
```

## Tests

```bash
pip install ".[test]"
pytest
```

The round-trip tests also read output back with `readpst` from
[libpst](https://www.five-ten-sg.com/libpst/) (`apt install pst-utils`) and are skipped if it is
not installed. readpst is a useful second opinion but is far more lenient than Outlook: it read
files that Outlook rejected as corrupt.

## Reporting problems

Bugs: open an issue. Security issues: see [SECURITY.md](SECURITY.md).

## Licence

MIT. See [LICENSE](LICENSE). Original work by the upstream author; modifications Copyright (c)
2026 Carterton District Council.
