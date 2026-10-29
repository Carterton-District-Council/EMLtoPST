"""Table Context (TC) implementation.

A TC is a 2D table (rows x columns) stored in a Heap-on-Node.
Used for folder hierarchy, contents, recipients, and attachments.

See [MS-PST] 2.3.4.
"""

import struct
import itertools

from .heap import HeapOnNode, HN_CLIENT_TC, MAX_HN_ALLOC
from ..ndb.block import MAX_BLOCK_DATA
from ..mapi.properties import (
    prop_id, prop_type, is_fixed_type, fixed_size,
    PT_LONG, PT_SHORT, PT_BOOLEAN, PT_SYSTIME, PT_UNICODE, PT_STRING8,
    PT_BINARY, PT_LONG_LONG,
    PidTagLtpRowId, PidTagLtpRowVer,
)
from ..errors import PSTWriteError

# TCINFO header
# bType(1) + cCols(1) + rgib[4](2 each=8) + hidRowIndex(4) + hnidRows(4) + hidIndex(4)
# = 1 + 1 + 8 + 4 + 4 + 4 = 22 bytes, then TCOLDESC array follows
# Subnode NIDs used by a Table Context. Index 0 is reserved by the format (see
# PATCHES.md patches 20 and 34), index 1 is the Row Matrix, so oversized cell
# values start at index 2.
NID_TYPE_LTP = 0x1F
ROW_MATRIX_SUBNODE_NID = (1 << 5) | NID_TYPE_LTP  # 0x3F
FIRST_CELL_SPILL_INDEX = 2

TCINFO_FIXED_SIZE = 22

# TCOLDESC: tag(4) + ibData(2) + cbData(1) + iBit(1) = 8 bytes
TCOLDESC_SIZE = 8


def _column_storage_size(ptype):
    """Get the storage size for a column in the row data.

    PT_BOOLEAN is a special case: it's 1 byte in a TC row specifically
    (confirmed against a real worked TC example in [MS-PST]'s own "Sample
    Folder Object" - PidTagSubfolders/PidTagMessageToMe both show CB: 1),
    even though the shared PROP_TYPE_SIZES table used elsewhere (e.g. PC
    values) says 4. A prior version of this code used the PC-shaped 4-byte
    size here too; real Outlook's Inbox Repair Tool caught it directly:
    "COLDESC (tag=0E0F000B) has invalid cbData (4, expect=1)". See
    PATCHES.md.
    """
    if ptype == PT_BOOLEAN:
        return 1
    if is_fixed_type(ptype):
        return fixed_size(ptype)
    else:
        # Variable-size columns store a 4-byte HNID in the fixed area
        return 4


def build_tc_node(column_defs, rows):
    """Build a Table Context data block.

    Args:
        column_defs: List of property tags defining the columns.
        rows: List of dicts mapping prop_tag → value for each row.
              Values should be the appropriate Python type.

    Returns:
        Tuple of (pages, subnodes) where:
            pages: List of bytes objects (HN pages, one per data block)
            subnodes: List of (nid, data_bytes) for row data > MAX_HN_ALLOC
    """
    hn = HeapOnNode(client_sig=HN_CLIENT_TC)
    subnodes = []
    cell_spill_index = itertools.count(FIRST_CELL_SPILL_INDEX)

    # Filter out the reserved pair if the caller passed them — we always add
    # them ourselves, always first, always in this order (see below).
    column_defs = [t for t in column_defs if t not in (PidTagLtpRowId, PidTagLtpRowVer)]
    num_user_cols = len(column_defs)

    # Total columns = the reserved (PidTagLtpRowId, PidTagLtpRowVer) pair + user columns
    total_cols = num_user_cols + 2

    # CEB (Cell Existence Bitmap) size: ceil(total_cols / 8) bytes
    ceb_size = (total_cols + 7) // 8

    # Sort user columns into type groups per [MS-PST] 2.3.4.2, grouped by
    # actual ROW STORAGE SIZE - not by fixed-vs-variable. A variable-length
    # property (PT_UNICODE/PT_STRING8/PT_BINARY) still stores a plain 4-byte
    # HID *in the row*, so it belongs in the same >=4-byte group as directly-
    # stored 4/8-byte fixed values, sorted together by tag - not appended as
    # a separate trailing group. A prior version of this code kept variable
    # columns in their own trailing "group_var", which silently froze
    # rgib[TCI_4b]/[TCI_2b]/[TCI_1b] at the boundary *before* any variable
    # columns instead of after them. Real Outlook's Inbox Repair Tool caught
    # this as "invalid TCI_Bm" (i.e. it independently recomputed a smaller
    # boundary than what this code had declared) - see PATCHES.md.
    group_4b = []  # storage size >= 4 bytes (fixed >=4, or a 4-byte HID)
    group_2b = []  # storage size == 2 bytes (PT_SHORT)
    group_1b = []  # storage size == 1 byte (none currently used)

    for tag in column_defs:
        ptype = prop_type(tag)
        sz = _column_storage_size(ptype)
        if sz >= 4:
            group_4b.append(tag)
        elif sz == 2:
            group_2b.append(tag)
        else:
            group_1b.append(tag)

    # Sort each group by tag for deterministic ordering
    group_4b.sort()
    group_2b.sort()
    group_1b.sort()

    # Row BYTE LAYOUT (ib_data): dwRowID/RowVer always at row offset 0/4,
    # user columns follow at offset 8+ per the size-grouping above. This is
    # entirely separate from both the TCOLDESC array order and iBit, below.
    ib_data_by_tag = {PidTagLtpRowId: 0, PidTagLtpRowVer: 4}
    sorted_user_tags_by_size = group_4b + group_2b + group_1b
    current_offset = 8  # after the reserved pair (2 x 4 bytes)

    offset_after_4b = current_offset
    offset_after_2b = current_offset
    offset_after_1b = current_offset

    for i, tag in enumerate(sorted_user_tags_by_size):
        cb = _column_storage_size(prop_type(tag))
        ib_data_by_tag[tag] = current_offset
        current_offset += cb

        if i < len(group_4b):
            offset_after_4b = current_offset
        elif i < len(group_4b) + len(group_2b):
            offset_after_2b = current_offset
        elif i < len(group_4b) + len(group_2b) + len(group_1b):
            offset_after_1b = current_offset

    # If a group is empty, its boundary equals the previous group's boundary
    if not group_2b:
        offset_after_2b = offset_after_4b
    if not group_1b:
        offset_after_1b = offset_after_2b

    # TCOLDESC ARRAY ORDER and iBit are two *more* independent orderings,
    # on top of ib_data above - three separate things this code previously
    # conflated pairwise, wrongly, twice in a row (see PATCHES.md patches 13
    # and 15 for the failed attempts). Confirmed this round by direct
    # contradiction: patch 15 fixed PidTagLtpRowId/RowVer's *array position*
    # to always be first (which is what "iBit" actually means - iBit is a
    # separate 1-byte FIELD stored per TCOLDESC entry, per [MS-PST] 2.3.4.3,
    # not something derived from array position) - and real Outlook's Inbox
    # Repair Tool immediately caught it: "tags not all increasing (tag=
    # 0C150003, prev=67F30003)", meaning the *array* itself must still be
    # tag-sorted (patch 13 was right about the array), while iBit=0/1 for
    # the reserved pair is independently fixed (patch 15 was right about
    # iBit) - both at once, not one or the other:
    #   - TCOLDESC ARRAY ORDER: every column, including the reserved pair,
    #     sorted by its real tag value (0x67F2/0x67F3 included) - this is
    #     what "MUST be sorted by tag" (2.3.4.1) actually governs.
    #   - iBit: PidTagLtpRowId=0, PidTagLtpRowVer=1 always, independent of
    #     where their TCOLDESC entry falls in the array; user columns get
    #     consecutive iBit values from 2 in the same tag-sorted order used
    #     for the array (arbitrary but stable - iBit only needs to agree
    #     between TCOLDESC and the per-row CEB, not match array position).
    sorted_user_tags_by_tag = sorted(column_defs)
    i_bit_by_tag = {PidTagLtpRowId: 0, PidTagLtpRowVer: 1}
    for i, tag in enumerate(sorted_user_tags_by_tag):
        i_bit_by_tag[tag] = i + 2

    array_order = sorted([PidTagLtpRowId, PidTagLtpRowVer] + column_defs)
    columns = [
        {
            'tag': tag,
            'ib_data': ib_data_by_tag[tag],
            'cb_data': 4 if tag in (PidTagLtpRowId, PidTagLtpRowVer) else _column_storage_size(prop_type(tag)),
            'i_bit': i_bit_by_tag[tag],
        }
        for tag in array_order
    ]

    # rgib boundaries per [MS-PST] 2.3.4.1 TCINFO (the authoritative field
    # table, confirmed against a real Outlook "invalid TCI_4b (0)" error from
    # the Inbox Repair Tool - see PATCHES.md): every rgib entry is an ENDING
    # offset, not a starting one. rgib[TCI_4b] is *not* always 0 - it's the
    # end of the 8-/4-byte group, which is what this code used to (wrongly)
    # store one slot later, in rgib[TCI_2b].
    rgib_0 = offset_after_4b  # TCI_4b: end of 8-/4-byte group
    rgib_1 = offset_after_2b  # TCI_2b: end of 2-byte group
    rgib_2 = offset_after_1b  # TCI_1b: end of 1-byte group
    rgib_3 = current_offset + ceb_size  # TCI_bm: end of CEB = total row stride

    # Row size = rgib[TCI_bm] (libpff uses rgib_3 directly as the row stride)
    row_size = rgib_3

    # Reserve the TC's fixed-size structures BEFORE allocating any per-row
    # strings, so they land on heap page 0. Their sizes are all known here,
    # but their contents aren't (the Row Matrix embeds HIDs of the strings
    # allocated below), so they're filled in via hn.patch() at the end.
    #
    # Order and placement match what real PSTs do - [MS-PST]'s own "Sample
    # Folder Object" dump shows a TC with RowIndex BTH at HID 0x20
    # (allocation 1), TCINFO at 0x40 (allocation 2) and Row Matrix at 0x80
    # (allocation 4), all on page 0, with variable-length values after them.
    #
    # This previously allocated in the opposite order - every per-row string
    # first, then the Row Matrix, then TCINFO last - so as soon as a table's
    # heap outgrew one 8176-byte block, TCINFO/RowIndex/RowMatrix all landed
    # on page 1. Real Outlook silently dropped every such message: on a real
    # mailbox that was 13 of 4,158, exactly the 13 whose Recipients heap
    # spilled to a second page, with no error naming them. See PATCHES.md.
    rows_data_size = len(rows) * row_size
    ri_leaf_size = 8 * len(rows)
    tcinfo_size = TCINFO_FIXED_SIZE + TCOLDESC_SIZE * len(columns)

    ri_header_hid = hn.allocate_placeholder(8)  # BTHHEADER
    tcinfo_hid = hn.allocate_placeholder(tcinfo_size)
    hn.set_user_root(tcinfo_hid)
    rows_hid_slot = (
        hn.allocate_placeholder(rows_data_size)
        if 0 < rows_data_size <= MAX_HN_ALLOC else None
    )
    ri_leaf_slot = (
        hn.allocate_placeholder(ri_leaf_size)
        if 0 < ri_leaf_size <= MAX_HN_ALLOC else None
    )

    # Build TCOLDESC array (reserved pair first, then user columns)
    coldesc_data = b''
    for col in columns:
        coldesc_data += struct.pack('<I H BB',
                                    col['tag'],
                                    col['ib_data'],
                                    col['cb_data'],
                                    col['i_bit'])

    # Build row data
    all_row_data = b''
    for row_idx, row in enumerate(rows):
        row_bytes = bytearray(row_size)

        # dwRowID at offset 0, LtpRowVer at offset 4 (4 bytes each) - the
        # reserved pair, always present. LtpRowVer isn't meaningfully used
        # by this pipeline (no in-place row edits happen), so a constant 0
        # is fine - only its presence/structure is required.
        nid_value = row.get('_nid', row_idx)
        struct.pack_into('<I', row_bytes, 0, nid_value)
        struct.pack_into('<I', row_bytes, 4, 0)

        # CEB: the reserved pair (iBit 0, 1) is always present.
        ceb = bytearray(ceb_size)
        ceb[0] |= (1 << 7)  # iBit 0: PidTagLtpRowId
        ceb[0] |= (1 << 6)  # iBit 1: PidTagLtpRowVer

        # Fill in user column values. The reserved pair can be anywhere in
        # `columns` now (it's tag-sorted, not positional - see above), so
        # skip by tag identity, not by array slice.
        for col in columns:
            tag = col['tag']
            if tag in (PidTagLtpRowId, PidTagLtpRowVer) or tag not in row:
                continue

            value = row[tag]
            ptype = prop_type(tag)
            offset = col['ib_data']

            # Mark column as present in CEB
            bit_idx = col['i_bit']
            ceb[bit_idx // 8] |= (1 << (7 - (bit_idx % 8)))

            if is_fixed_type(ptype):
                if ptype == PT_LONG:
                    struct.pack_into('<I', row_bytes, offset, value & 0xFFFFFFFF)
                elif ptype == PT_SHORT:
                    struct.pack_into('<H', row_bytes, offset, value & 0xFFFF)
                elif ptype == PT_BOOLEAN:
                    struct.pack_into('<B', row_bytes, offset, 1 if value else 0)
                elif ptype == PT_SYSTIME:
                    struct.pack_into('<Q', row_bytes, offset, value)
                elif ptype == PT_LONG_LONG:
                    struct.pack_into('<Q', row_bytes, offset, value)
            else:
                # Variable-size: allocate on heap, store HID
                if ptype == PT_UNICODE:
                    heap_data = value.encode('utf-16-le') if isinstance(value, str) else value
                elif ptype == PT_STRING8:
                    heap_data = value.encode('utf-8') if isinstance(value, str) else value
                elif ptype == PT_BINARY:
                    heap_data = value if isinstance(value, bytes) else b''
                else:
                    heap_data = value if isinstance(value, bytes) else b''

                # A value too large for the heap goes to a subnode, with the
                # subnode's NID in the cell instead of a HID - the same rule
                # build_pc_node() already applied to properties. Without this a
                # single oversized cell was allocated on an HN page anyway and
                # blew past the 8176-byte block limit, crashing the pack: a
                # 2,303-message mailbox hit it on one row whose recipient list
                # ran to ~11 KB. The first test mailbox never had a value that large,
                # which is precisely why a second mailbox was worth testing.
                if len(heap_data) > MAX_HN_ALLOC:
                    spill_nid = (next(cell_spill_index) << 5) | NID_TYPE_LTP
                    subnodes.append((spill_nid, heap_data))
                    struct.pack_into('<I', row_bytes, offset, spill_nid)
                    continue

                # An empty value is stored as HID 0 - "no allocation" - with
                # the cell still marked present in the CEB. Allocating zero
                # bytes instead produces a heap entry with nothing in it,
                # which a reader counts as a *freed* allocation: real Outlook
                # reported "HN (nid=62B) free alloc count doesn't match
                # (computed=1, expected=0)" and then could not parse the table
                # at all, reporting both of its user columns as missing. A
                # real Outlook-written receive folder table stores exactly
                # this - cell present, HID 0 - for its empty default message
                # class. See PATCHES.md patch 39.
                hid = hn.allocate(heap_data) if heap_data else 0
                struct.pack_into('<I', row_bytes, offset, hid)

        # Write CEB at end of column data (offset = current_offset, before rgib_3 boundary)
        ceb_offset = current_offset
        row_bytes[ceb_offset:ceb_offset + ceb_size] = ceb
        all_row_data += bytes(row_bytes)

    # Allocate row data on heap, or as subnode if too large
    # Per [MS-PST] 2.3.4.4, hnidRows is an HNID — either HID or NID.
    #
    # The subnode NID is NID_TYPE_LTP with index 1 (0x3F), not index 0
    # (0x1F). A real Outlook-written Contents Table uses 0x3F; index 0 is
    # reserved, the same way row ID 0 is rejected in a Row Index BTH (patch
    # 20). Using 0x1F left every table whose Row Matrix spills to a subnode
    # unreadable - scanpst reported *all* of their columns missing - while
    # tables small enough to keep the matrix on the heap were fine, which is
    # exactly the split observed once patch 32/33 landed. See PATCHES.md.
    _ROW_MATRIX_SUBNODE_NID = ROW_MATRIX_SUBNODE_NID
    if all_row_data:
        if len(all_row_data) > MAX_HN_ALLOC:
            # Store as subnode — NID with non-zero nidType. Per [MS-PST]
            # 2.3.4.4, each data block "MUST store an integral number of
            # rows" when the Row Matrix spans multiple blocks - pass a
            # row-aligned chunk size so the generic subnode chunker
            # (pst_file.py's _store_subnode_data) never splits a row across
            # two blocks.
            rows_nid = _ROW_MATRIX_SUBNODE_NID
            rows_per_block = max(1, MAX_BLOCK_DATA // row_size)
            subnodes.append((rows_nid, all_row_data, rows_per_block * row_size))
            rows_hid = rows_nid
        else:
            # Fill the slot reserved on page 0 above.
            hn.patch(rows_hid_slot, all_row_data)
            rows_hid = rows_hid_slot
    else:
        rows_hid = 0

    # Build Row Index BTH: maps dwRowID (4 bytes) → row index (4 bytes)
    # Required by [MS-PST] 2.3.4.3 for row lookup
    # Always create a valid BTH, even for empty TCs (libpff requires it)
    #
    # A single heap allocation can hold at most MAX_HN_ALLOC bytes, and
    # hidRowIndex (unlike hnidRows/the row matrix) is a plain HID per spec, not
    # an HNID - it cannot be redirected to a subnode. With enough rows (a real
    # mailbox folder easily has thousands), the flat leaf-entries blob exceeds
    # that on its own, which crashed on a real 4158-message folder. Fixed by
    # building a proper two-level BTH (index page -> multiple leaf pages) once
    # the leaf data would be too big for one allocation, per [MS-PST] 2.3.2.
    ri_leaf_hid = 0
    ri_idx_levels = 0
    if rows:
        # Collect (dwRowID, row_index) pairs, sorted by dwRowID (BTH requires
        # ascending key order).
        ri_pairs = []
        for row_idx, row in enumerate(rows):
            nid_value = row.get('_nid', row_idx)
            ri_pairs.append((nid_value, row_idx))
        ri_pairs.sort(key=lambda x: x[0])

        entry_size = 8  # key(4) + data(4)
        # Leave generous margin below MAX_HN_ALLOC for page/pagemap overhead.
        max_entries_per_leaf = max(1, (MAX_HN_ALLOC - 64) // entry_size)

        if len(ri_pairs) * entry_size <= MAX_HN_ALLOC:
            ri_leaf_data = b''.join(
                struct.pack('<I', rid) + struct.pack('<I', ridx) for rid, ridx in ri_pairs
            )
            # Fill the slot reserved on page 0 above.
            hn.patch(ri_leaf_slot, ri_leaf_data)
            ri_leaf_hid = ri_leaf_slot
            ri_idx_levels = 0
        else:
            index_entries = []  # (first_key_in_group, leaf_hid)
            for i in range(0, len(ri_pairs), max_entries_per_leaf):
                group = ri_pairs[i:i + max_entries_per_leaf]
                leaf_data = b''.join(
                    struct.pack('<I', rid) + struct.pack('<I', ridx) for rid, ridx in group
                )
                leaf_hid = hn.allocate(leaf_data)
                index_entries.append((group[0][0], leaf_hid))

            # A single index page holds far more than enough entries for any
            # realistic mailbox folder (e.g. ~450 leaf groups even at only
            # ~450 rows/leaf covers ~200k rows in one index page), so a third
            # BTH level is not implemented.
            index_data = b''.join(
                struct.pack('<I', key) + struct.pack('<I', hid) for key, hid in index_entries
            )
            if not (len(index_data) <= MAX_HN_ALLOC):
                raise PSTWriteError(f"Row Index BTH needs {len(index_entries)} leaf groups, too many for a "
                "single index page - a third BTH level would be needed (not implemented; "
                "this would require an extremely large single table)")
            ri_leaf_hid = hn.allocate(index_data)
            ri_idx_levels = 1

    # BTHHEADER: bType(1)=0xB5, cbKey(1)=4, cbEnt(1)=4, bIdxLevels(1), hidRoot(4)
    ri_bth_header = struct.pack('<BB BB I',
                                 0xB5, 4, 4, ri_idx_levels, ri_leaf_hid)
    hn.patch(ri_header_hid, ri_bth_header)
    row_index_hid = ri_header_hid

    # Build TCINFO header
    tcinfo = struct.pack('<BB HHHH I I I',
                         0x7C,  # bType = TC
                         total_cols,  # cCols (includes PidTagLtpRowId)
                         rgib_0,  # rgib[TCI_4b]
                         rgib_1,  # rgib[TCI_2b]
                         rgib_2,  # rgib[TCI_1b]
                         rgib_3,  # rgib[TCI_bm]
                         row_index_hid,  # hidRowIndex
                         rows_hid,  # hnidRows
                         0)  # hidIndex (deprecated)

    tcinfo_data = tcinfo + coldesc_data
    if not (len(tcinfo_data) == tcinfo_size):
        raise PSTWriteError(f"TCINFO size changed after reservation: reserved {tcinfo_size}, "
        f"built {len(tcinfo_data)}")

    # Fill the slot reserved on page 0 above (already set as the user root).
    hn.patch(tcinfo_hid, tcinfo_data)

    return hn.build(), subnodes
