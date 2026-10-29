"""Read a written PST back and count what is actually in it.

Why this exists
---------------
The builder counts the messages it was *handed*. That number is worthless as a
safety check: three separate defects in this writer silently dropped messages
that `add_message()` had accepted without error, and each was caught only
because a human read a count off an Outlook dialog (4,145 where 4,158 were
expected). See PATCHES.md patches 22, 23 and 34.

So this module does not trust the writer at all. It opens the finished file and
walks its B-trees from the header down, exactly as a reader would, counting the
messages that are genuinely reachable. Anything the writer believed but did not
successfully store shows up as a discrepancy.

It is a *reachability* check, not a content check - it confirms every message
is present and readable in the output, not that the bytes decode to the same
mail. It would not, for example, have caught MIME parts the parser discarded
before they ever reached the writer (PATCHES.md, 2026-10-29).
"""

from __future__ import annotations

import struct

# Block/page constants, per [MS-PST]. Only what a reader needs.
_PAGE_SIZE = 512
_PAGE_ENTRY_COUNT_OFF = 488
_ROOT_OFF = 0xB4
_MAX_BLOCK_DATA = 8176

_NID_TYPE_NORMAL_FOLDER = 0x02
_NID_TYPE_NORMAL_MESSAGE = 0x04
_NID_TYPE_CONTENTS_TABLE = 0x0E

_PR_DISPLAY_NAME = 0x3001


class PstReadError(Exception):
    """The written PST could not be parsed well enough to verify it."""


class _PstReader:
    def __init__(self, path):
        self._f = open(path, "rb")
        root = self._read(0, 1024)[_ROOT_OFF:_ROOT_OFF + 72]
        self._nbt_root = struct.unpack("<QQ", root[36:52])[1]
        self._bbt_root = struct.unpack("<QQ", root[52:68])[1]
        self._blocks = {}
        self._nodes = {}
        self._walk_bbt(self._bbt_root)
        self._walk_nbt(self._nbt_root)

    def close(self):
        self._f.close()

    def _read(self, offset, size):
        self._f.seek(offset)
        return self._f.read(size)

    def _walk_bbt(self, offset, depth=0):
        if depth > 32:
            raise PstReadError("BBT deeper than 32 levels - refusing to recurse")
        page = self._read(offset, _PAGE_SIZE)
        count, _max, ent_size, level = struct.unpack(
            "<BBBB", page[_PAGE_ENTRY_COUNT_OFF:_PAGE_ENTRY_COUNT_OFF + 4])
        for i in range(count):
            entry = page[i * ent_size:(i + 1) * ent_size]
            if level == 0:
                bid, ib, cb = struct.unpack("<QQH", entry[:18])
                self._blocks[bid & ~1] = (ib, cb)
            else:
                self._walk_bbt(struct.unpack("<QQQ", entry[:24])[2], depth + 1)

    def _walk_nbt(self, offset, depth=0):
        if depth > 32:
            raise PstReadError("NBT deeper than 32 levels - refusing to recurse")
        page = self._read(offset, _PAGE_SIZE)
        count, _max, ent_size, level = struct.unpack(
            "<BBBB", page[_PAGE_ENTRY_COUNT_OFF:_PAGE_ENTRY_COUNT_OFF + 4])
        for i in range(count):
            entry = page[i * ent_size:(i + 1) * ent_size]
            if level == 0:
                nid, bid_data, bid_sub, parent = struct.unpack("<QQQI", entry[:28])
                self._nodes[nid] = (bid_data, bid_sub, parent)
            else:
                self._walk_nbt(struct.unpack("<QQQ", entry[:24])[2], depth + 1)

    def _block(self, bid):
        try:
            ib, cb = self._blocks[bid & ~1]
        except KeyError:
            raise PstReadError(f"block {bid} referenced but not in the BBT")
        return self._read(ib, cb)

    def _leaf_bids(self, bid, depth=0):
        """Flatten an XBLOCK data tree to its leaf block IDs."""
        if depth > 8:
            raise PstReadError("data tree deeper than 8 levels")
        if bid & 2:  # internal block: XBLOCK/XXBLOCK
            data = self._block(bid)
            count = struct.unpack("<H", data[2:4])[0]
            out = []
            for child in struct.unpack(f"<{count}Q", data[8:8 + count * 8]):
                out.extend(self._leaf_bids(child, depth + 1))
            return out
        return [bid]

    def _pages(self, bid):
        return [self._block(b) for b in self._leaf_bids(bid)]

    @staticmethod
    def _heap_alloc(pages, hid):
        """Fetch one HN allocation by Heap ID."""
        page = pages[(hid >> 16) & 0xFFFF]
        index = (hid >> 5) & 0x7FF
        ib_hnpm = struct.unpack("<H", page[0:2])[0]
        count = struct.unpack("<H", page[ib_hnpm:ib_hnpm + 2])[0]
        offsets = struct.unpack(
            f"<{count + 1}H", page[ib_hnpm + 4:ib_hnpm + 4 + (count + 1) * 2])
        return page[offsets[index - 1]:offsets[index]]

    def _bth_leaves(self, pages, root_hid):
        """All leaf entries of a BTree-on-Heap, in order."""
        header = self._heap_alloc(pages, root_hid)
        _btype, key_size, ent_size, levels, root = struct.unpack("<BBBBI", header[:8])
        out = []

        def descend(hid, level, depth=0):
            if depth > 8:
                raise PstReadError("BTH deeper than 8 levels")
            data = self._heap_alloc(pages, hid)
            if level == 0:
                step = key_size + ent_size
                for off in range(0, len(data), step):
                    out.append(data[off:off + step])
            else:
                step = key_size + 4
                for off in range(0, len(data), step):
                    child = struct.unpack("<I", data[off + key_size:off + key_size + 4])[0]
                    descend(child, level - 1, depth + 1)

        if root:
            descend(root, levels)
        return out

    def _subnodes(self, bid, depth=0):
        """Subnode NID -> (bidData, bidSub), flattening SIBLOCKs."""
        if depth > 8:
            raise PstReadError("subnode tree deeper than 8 levels")
        data = self._block(bid)
        _btype, level, count = struct.unpack("<BBH", data[:4])
        out = {}
        if level == 0:
            for i in range(count):
                nid, bd, bs = struct.unpack("<QQQ", data[8 + i * 24:8 + i * 24 + 24])
                out[nid & 0xFFFFFFFF] = (bd, bs)
        else:
            for i in range(count):
                _key, child = struct.unpack("<QQ", data[8 + i * 16:8 + i * 16 + 16])
                out.update(self._subnodes(child, depth + 1))
        return out

    def folder_display_name(self, folder_nid):
        bid_data = self._nodes[folder_nid][0]
        if not bid_data:
            return None
        pages = self._pages(bid_data)
        user_root = struct.unpack("<I", pages[0][4:8])[0]
        for entry in self._bth_leaves(pages, user_root):
            prop_id, prop_type, value = struct.unpack("<HHI", entry[:8])
            if prop_id == _PR_DISPLAY_NAME:
                try:
                    return self._heap_alloc(pages, value).decode("utf-16-le")
                except Exception:  # noqa: BLE001 - a name we can't read isn't fatal
                    return None
        return None

    def contents_row_nids(self, contents_nid):
        """The message NIDs a folder's Contents Table actually lists."""
        bid_data, bid_sub, _parent = self._nodes[contents_nid]
        if not bid_data:
            return []
        pages = self._pages(bid_data)
        user_root = struct.unpack("<I", pages[0][4:8])[0]
        tcinfo = self._heap_alloc(pages, user_root)
        rgib = struct.unpack("<HHHH", tcinfo[2:10])
        hid_row_index, hnid_rows = struct.unpack("<II", tcinfo[10:18])
        row_size = rgib[3]
        if not row_size or not hid_row_index:
            return []

        # The Row Matrix lives on the heap when small, in a subnode when large.
        if hnid_rows == 0:
            return []
        if (hnid_rows & 0x1F) != 0:  # a NID, not a HID: it's a subnode
            subs = self._subnodes(bid_sub) if bid_sub else {}
            if hnid_rows not in subs:
                raise PstReadError(
                    f"contents table 0x{contents_nid:X} points at subnode "
                    f"0x{hnid_rows:X}, which is absent")
            row_blocks = self._pages(subs[hnid_rows][0])
        else:
            row_blocks = [self._heap_alloc(pages, hnid_rows)]

        # Rows never straddle a block boundary ([MS-PST] 2.3.4.4): each block
        # holds floor(8176 / row size) rows and is padded out. Indexing the
        # concatenated matrix linearly instead silently reads garbage past the
        # first block (PATCHES.md patch 14).
        rows_per_block = max(1, _MAX_BLOCK_DATA // row_size)

        nids = []
        for entry in self._bth_leaves(pages, hid_row_index):
            _row_id, irow = struct.unpack("<II", entry[:8])
            block, within = divmod(irow, rows_per_block)
            if block >= len(row_blocks):
                raise PstReadError(
                    f"contents table 0x{contents_nid:X} row {irow} is past the "
                    f"end of its row matrix")
            row = row_blocks[block][within * row_size:(within + 1) * row_size]
            if len(row) < 4:
                raise PstReadError(
                    f"contents table 0x{contents_nid:X} row {irow} is truncated")
            nids.append(struct.unpack("<I", row[0:4])[0])
        return nids

    def folders(self):
        for nid, (bid_data, _bs, _parent) in sorted(self._nodes.items()):
            if (nid & 0x1F) == _NID_TYPE_NORMAL_FOLDER and bid_data:
                yield nid

    def message_node_count(self):
        return sum(1 for nid in self._nodes if (nid & 0x1F) == _NID_TYPE_NORMAL_MESSAGE)

    def has_node(self, nid):
        return nid in self._nodes


def read_pst_contents(pst_path):
    """Count what is genuinely reachable in a finished PST.

    Returns a dict:
        folders           {folder NID: number of contents-table rows}
        folder_names      {folder NID: display name}
        listed_messages   total contents-table rows across all folders
        message_nodes     message nodes present in the NBT
        dangling_rows     rows pointing at a NID that isn't in the NBT
        orphan_messages   message nodes no folder lists
    """
    reader = _PstReader(pst_path)
    try:
        folders = {}
        names = {}
        listed = 0
        dangling = 0
        listed_nids = set()

        for folder_nid in reader.folders():
            contents_nid = (folder_nid & ~0x1F) | _NID_TYPE_CONTENTS_TABLE
            if not reader.has_node(contents_nid):
                continue
            nids = reader.contents_row_nids(contents_nid)
            # Folders with no mail are left out, so the result compares
            # directly against "messages added per folder".
            if not nids:
                continue
            folders[folder_nid] = len(nids)
            names[folder_nid] = reader.folder_display_name(folder_nid) or f"0x{folder_nid:X}"
            listed += len(nids)
            for nid in nids:
                listed_nids.add(nid)
                if not reader.has_node(nid):
                    dangling += 1

        message_nodes = reader.message_node_count()
        orphans = sum(
            1 for nid in reader._nodes
            if (nid & 0x1F) == _NID_TYPE_NORMAL_MESSAGE and nid not in listed_nids)

        return {
            "folders": folders,
            "folder_names": names,
            "listed_messages": listed,
            "message_nodes": message_nodes,
            "dangling_rows": dangling,
            "orphan_messages": orphans,
        }
    finally:
        reader.close()


def reconcile(pst_path, expected_counts, folder_names=None):
    """Compare a written PST against the messages that were added to it.

    Args:
        pst_path: the finished .pst.
        expected_counts: {folder NID: number of messages added to that folder},
            counted by the caller as it called add_message() - not taken from
            the builder, which is the thing being checked.
        folder_names: optional {folder NID: label} for readable problems.

    Returns a dict with `ok` plus the evidence behind it. `problems` is a list
    of human-readable strings; empty means everything reconciled.
    """
    actual = read_pst_contents(pst_path)
    names = {**actual["folder_names"], **(folder_names or {})}
    expected_counts = {nid: n for nid, n in expected_counts.items() if n}
    problems = []

    def label(nid):
        return f"{names.get(nid, '?')!r} (0x{nid:X})"

    expected_total = sum(expected_counts.values())
    if actual["listed_messages"] != expected_total:
        problems.append(
            f"message count mismatch: {expected_total} added, "
            f"{actual['listed_messages']} in the PST "
            f"({expected_total - actual['listed_messages']:+d})")

    for nid, expected in sorted(expected_counts.items()):
        got = actual["folders"].get(nid)
        if got is None:
            problems.append(f"folder missing from PST: {label(nid)} ({expected} added)")
        elif got != expected:
            problems.append(
                f"folder {label(nid)}: {expected} added, {got} in the PST "
                f"({expected - got:+d})")

    for nid in sorted(set(actual["folders"]) - set(expected_counts)):
        problems.append(
            f"unexpected folder in PST: {label(nid)} ({actual['folders'][nid]} messages)")

    if actual["dangling_rows"]:
        problems.append(
            f"{actual['dangling_rows']} contents-table rows point at messages "
            f"that are not in the PST")
    if actual["orphan_messages"]:
        problems.append(
            f"{actual['orphan_messages']} message nodes are in the PST but "
            f"listed in no folder")

    return {
        "ok": not problems,
        "problems": problems,
        "expected_messages": expected_total,
        **actual,
    }
