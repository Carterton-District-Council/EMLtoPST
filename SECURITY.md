# Security

## Reporting a vulnerability

Please report security issues privately through GitHub: on this repository's **Security** tab,
choose **Report a vulnerability**. Do not open a public issue for them.

This is a small library maintained by a local council. We will acknowledge a report and fix
confirmed issues as quickly as we can, but cannot promise a response time. Fixes go into the
latest version only.

## What this tool does and does not do

eml2pst reads `.eml` files and writes a PST file. It makes no network connections, runs no
external programs, and uses only the Python standard library.

## Things to know before using it

**Output PSTs are not encrypted or password-protected.** They contain the full text and
attachments of every message, and anyone who can read the file can read the mail. Store and
transfer them as carefully as the mailbox they came from, and delete working copies once they are
no longer needed. (Outlook's own "compressible encryption" is a fixed byte substitution, not
security, so this tool does not use it.)

**Attachment contents are copied as they are.** Nothing is scanned or removed. A PST built from
malicious mail contains that malicious mail. Attachment *filenames* have characters Windows
forbids (`<>:"/\|?*` and control characters) replaced, so a stored name cannot be a path such as
`..\..\x.exe`.

**Stdin mode trusts its input.** The `eml_file` field in JSONL is opened as given, so whoever
writes the JSONL can make eml2pst read any file the user running it can read, and copy it into
the PST. Only feed stdin mode JSONL you produced or trust.

**The output file is overwritten.** `-o` replaces an existing file of that name once the new
PST has been verified. A `<name>.partial` file next to it is also overwritten during the build.

**Directory mode does not follow symlinked directories.** Symlinked `.eml` files are read.

**Resource use is not capped.** Each message is read fully into memory, so one very large
message uses memory in proportion to its size. PST data is written to disk as it is added, so
memory does not grow with the number of messages. Disk use is roughly the size of the input
mail. Converting untrusted input from an unknown source should be done with OS-level memory and
disk limits.

## How it was reviewed

Before publication in October 2026 the code had a review and security audit:

- **Static analysis** with bandit and semgrep. The one finding, an MD5 call, is the PST format's
  own search-key hash and is now marked `usedforsecurity=False`.
- **Secret scanning** with detect-secrets, plus a manual check for internal names, paths and
  addresses. None remain.
- **Fuzzing** of the EML parser with Hypothesis: 5,000 malformed and mutated messages, run in a
  sandbox with no network. It found two ways one bad header could make the whole message fail to
  convert. Both are fixed (PATCHES.md, patch 46) and every case now converts and verifies.
- **Integrity checks** previously written as `assert` statements, which `python -O` removes, are
  now explicit errors (patch 49).
- **Read-back verification**: the command-line tool re-reads every PST it writes and exits 2 if
  the contents do not match what was added (patch 48).

No vulnerability allowing code execution, writing outside the output path, or network access
was found. The issues found were data-integrity problems, all fixed and listed in
[PATCHES.md](PATCHES.md).
