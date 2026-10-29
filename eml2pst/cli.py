"""Command-line interface for eml2pst."""

import argparse
import base64
import collections
import json
import os
import struct
import sys
from pathlib import Path

from .pst_file import PSTFileBuilder
from .eml_parser import parse_eml_file, parse_eml_bytes
from .verify import PstReadError, reconcile

# Exit codes: 0 = every message converted; 1 = usage or fatal error;
# 2 = the PST was written but some messages failed and are NOT in it, or
#     reading the finished PST back did not find what was added (DO NOT USE).
EXIT_PARTIAL = 2


class _ArgumentParser(argparse.ArgumentParser):
    """argparse exits 2 on a usage error, which would read as EXIT_PARTIAL."""

    def error(self, message):
        self.print_usage(sys.stderr)
        self.exit(1, f'{self.prog}: error: {message}\n')


def _process_stdin(builder, stats):
    """Read JSONL from stdin and add messages to the builder.

    Each line is a JSON object with:
        folder: Folder path (e.g. "Inbox", "Inbox/Projects")
        eml:    Base64-encoded EML content
        eml_file: Path to EML file on disk (alternative to eml)

    `eml_file` is opened as given, so whoever writes the JSONL can make this
    read any file the user running it can read. Only feed it JSONL you trust.
    """
    # Cache: folder path -> NID
    folder_nids = {}

    def get_or_create_folder(folder_path):
        """Get or create a folder by path (e.g. 'Inbox/Projects/2024')."""
        if not folder_path:
            return builder._ipm_subtree_nid
        if folder_path in folder_nids:
            return folder_nids[folder_path]

        parts = folder_path.replace('\\', '/').split('/')
        current_path = ''
        parent_nid = None

        for part in parts:
            current_path = f"{current_path}/{part}" if current_path else part
            if current_path not in folder_nids:
                nid = builder.add_folder(part, parent_nid)
                folder_nids[current_path] = nid
                stats['folders'] += 1
            parent_nid = folder_nids[current_path]

        return folder_nids[folder_path]

    for line_num, line in enumerate(sys.stdin, 1):
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError as e:
            stats['errors'] += 1
            print(f"  ! Line {line_num}: invalid JSON: {e}", file=sys.stderr)
            continue

        folder_path = obj.get('folder', '')
        try:
            folder_nid = get_or_create_folder(folder_path)

            if 'eml' in obj:
                eml_data = base64.b64decode(obj['eml'])
                parsed = parse_eml_bytes(eml_data)
            elif 'eml_file' in obj:
                parsed = parse_eml_file(obj['eml_file'])
            else:
                stats['errors'] += 1
                print(f"  ! Line {line_num}: missing 'eml' or 'eml_file'",
                      file=sys.stderr)
                continue

            builder.add_message(folder_nid, parsed)
            stats['messages'] += 1
            stats['per_folder'][folder_nid] += 1
            subject = parsed.get('subject', '(No Subject)')[:60]
            print(f"  + [{folder_path or 'Root'}] {subject}", file=sys.stderr)
        except Exception as e:
            stats['errors'] += 1
            print(f"  ! Line {line_num}: {e}", file=sys.stderr)


def _process_directory(builder, input_dir, stats):
    """Recursively process a directory tree of EML files.

    Symlinked directories are skipped, not followed: a link back to an
    ancestor would otherwise recurse until Python's recursion limit.
    """
    def recurse(dir_path, parent_nid=None):
        if dir_path == input_dir:
            target_nid = builder._ipm_subtree_nid
        else:
            target_nid = builder.add_folder(dir_path.name, parent_nid)
            stats['folders'] += 1

        eml_files = sorted(p for p in dir_path.glob('*.eml') if p.is_file())
        for eml_path in eml_files:
            try:
                parsed = parse_eml_file(eml_path)
                builder.add_message(target_nid, parsed)
                stats['messages'] += 1
                stats['per_folder'][target_nid] += 1
                print(f"  + {eml_path.name}", file=sys.stderr)
            except Exception as e:
                stats['errors'] += 1
                print(f"  ! Error parsing {eml_path.name}: {e}", file=sys.stderr)

        subdirs = sorted(d for d in dir_path.iterdir() if d.is_dir() and not d.is_symlink())
        for subdir in subdirs:
            recurse(subdir, target_nid)

    print(f"Scanning {input_dir}...", file=sys.stderr)
    recurse(input_dir)


def main():
    parser = _ArgumentParser(
        prog='eml2pst',
        description='Convert EML files and directory trees to PST format.',
    )
    parser.add_argument(
        'input_dir',
        type=Path,
        nargs='?',
        default=None,
        help='Directory containing EML files (subdirectories become PST folders)',
    )
    parser.add_argument(
        '-o', '--output',
        type=Path,
        default=Path('output.pst'),
        help='Output PST file path (default: output.pst)',
    )
    parser.add_argument(
        '-n', '--name',
        default='Personal Folders',
        help='Display name for the PST store (default: Personal Folders)',
    )
    parser.add_argument(
        '--stdin',
        action='store_true',
        help='Read JSONL from stdin: {"folder":"path","eml":"<base64>"}',
    )

    args = parser.parse_args()

    if not args.stdin and args.input_dir is None:
        parser.error("either input_dir or --stdin is required")

    if args.input_dir and not args.stdin and not args.input_dir.is_dir():
        print(f"Error: '{args.input_dir}' is not a directory", file=sys.stderr)
        sys.exit(1)

    # Build under a temporary name and only rename to the requested one once
    # the file has been read back and verified, so a crash or a failed check
    # never leaves something at args.output that looks like a usable PST.
    partial = args.output.with_name(args.output.name + '.partial')
    builder = PSTFileBuilder(partial, display_name=args.name)
    stats = {'folders': 0, 'messages': 0, 'errors': 0,
             'per_folder': collections.Counter()}

    if args.stdin:
        print("Reading JSONL from stdin...", file=sys.stderr)
        _process_stdin(builder, stats)
    else:
        _process_directory(builder, args.input_dir, stats)

    print(f"\nWriting PST file to {args.output}...", file=sys.stderr)
    builder.write()

    # Read the finished file back rather than trusting the builder's own
    # bookkeeping: messages it accepted have silently gone missing before.
    try:
        check = reconcile(partial, stats['per_folder'])
    except (PstReadError, OSError, struct.error) as e:
        check = {'ok': False, 'problems': [f"could not read the PST back: {e}"]}
    if not check['ok']:
        print(f"\nVERIFICATION FAILED - DO NOT USE {partial}:", file=sys.stderr)
        for problem in check['problems']:
            print(f"  - {problem}", file=sys.stderr)
        sys.exit(EXIT_PARTIAL)
    os.replace(partial, args.output)

    print(f"\nDone: {stats['messages']} messages, {stats['folders']} folders "
          f"(verified by reading the PST back)", file=sys.stderr)
    if stats['errors']:
        print(f"  ({stats['errors']} messages FAILED and are not in the PST)",
              file=sys.stderr)
        sys.exit(EXIT_PARTIAL)


if __name__ == '__main__':
    main()
