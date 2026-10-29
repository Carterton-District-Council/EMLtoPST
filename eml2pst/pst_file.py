"""PST file assembly — orchestrates all layers to produce a valid .pst file.

This module ties together NDB, LTP, and Messaging layers to write
a complete Unicode PST file from in-memory data.
"""

import bisect
import struct
from pathlib import Path

from .crc import compute_crc
from .ndb.header import build_header, pack_root, HEADER_SIZE
from .ndb.block import pack_block, block_total_size, MAX_BLOCK_DATA
from .ndb.xblock import build_xblock, MAX_ENTRIES_PER_BLOCK
from .ndb.btree import (
    build_btree_pages, pack_nbt_entry, pack_bbt_entry,
    PTTYPE_NBT, PTTYPE_BBT, PAGE_SIZE,
)
from .ndb.amap import build_amap_page, FIRST_AMAP_OFFSET, AMAP_COVERAGE, compute_amap_free
from .ndb.legacy_maps import (
    build_legacy_map_page, legacy_page_positions, reserved_bytes_at_amap_index,
)
from .ndb.subnode import build_sl_block
from .messaging.store import (
    build_message_store, build_name_to_id_map,
    IPM_SUBTREE_NAME, WASTEBASKET_NAME, FINDER_NAME,
)
from .messaging.special import (
    build_special_tables, build_hmp, RESERVED_NID_INDEXES,
    EMPTY_NODE_NIDS, F01_NID, F01_DATA, HMP_NID,
)
from .messaging.folder import (
    build_folder_pc, folder_row_values, build_hierarchy_tc, build_contents_tc,
    build_assoc_contents_tc, folder_nid_hierarchy,
    folder_nid_contents, folder_nid_assoc,
)
from .messaging.message import (
    build_message_pc, build_recipients_tc, build_attachments_tc,
    message_row_values,
    build_attachment_pc, attachment_subnode_nid, message_stored_size,
    message_nid_recipients, message_nid_attachments,
)
from .mapi.properties import (
    NID_MESSAGE_STORE, NID_NAME_TO_ID_MAP, NID_ROOT_FOLDER,
    NID_TYPE_NORMAL_FOLDER, NID_TYPE_NORMAL_MESSAGE,
    NID_TYPE_INTERNAL,
    PR_DISPLAY_NAME, PR_CONTENT_COUNT, PR_CONTENT_UNREAD_COUNT,
    PR_SUBFOLDERS,
    PR_SUBJECT, PR_MESSAGE_CLASS, PR_MESSAGE_FLAGS,
    PR_MESSAGE_SIZE, PR_MESSAGE_DELIVERY_TIME,
    PR_IMPORTANCE, PR_HASATTACH, PR_SENDER_NAME,
    PR_SENSITIVITY,
    MSGFLAG_READ, MSGFLAG_HASATTACH,
    make_nid,
)
from .ltp.pc import build_pc_node
from .utils import filetime_now
from .errors import PSTWriteError


def subnode_data_chunks(data, chunk_size=None):
    """The exact block payloads _store_subnode_data() will write for `data`.

    Shared with _message_stored_bytes() so PR_MESSAGE_SIZE is measured from
    the same chunking the writer actually performs. Computing the two
    separately is how PR_MESSAGE_SIZE went wrong in the first place.
    """
    chunk_size = chunk_size or MAX_BLOCK_DATA
    if len(data) <= MAX_BLOCK_DATA:
        return [data]
    chunks = [data[i:i + chunk_size] for i in range(0, len(data), chunk_size)]
    return [
        c if i == len(chunks) - 1 else c + b'\x00' * (MAX_BLOCK_DATA - len(c))
        for i, c in enumerate(chunks)
    ]


def _skip_amap_pages(offset, size):
    """Advance offset so [offset, offset+size) doesn't overlap a reserved map page.

    AMap pages are at fixed positions: FIRST_AMAP_OFFSET + n * AMAP_COVERAGE.
    Each one may be followed by one or more deprecated-but-mandatory map pages
    (PMap/FMap/FPMap) in its own reserved run - see ndb/legacy_maps.py for
    which appear where and why they're still required.
    """
    while True:
        if offset < FIRST_AMAP_OFFSET:
            return offset
        rel = offset - FIRST_AMAP_OFFSET
        n = rel // AMAP_COVERAGE
        amap_pos = FIRST_AMAP_OFFSET + n * AMAP_COVERAGE
        reserved_end = amap_pos + reserved_bytes_at_amap_index(n)
        # Check if we overlap with this boundary's reserved run
        if offset < reserved_end:
            offset = reserved_end
            continue
        # Check if the item extends into the next AMap page's position
        next_amap = FIRST_AMAP_OFFSET + (n + 1) * AMAP_COVERAGE
        if offset + size > next_amap:
            offset = next_amap + reserved_bytes_at_amap_index(n + 1)
            continue
        return offset


class PSTFileBuilder:
    """Builds a complete PST file from folders and messages.

    Data blocks (message bodies, attachment binary content, ...) are written to
    `output_path` as soon as each message is added rather than held in memory
    for the whole mailbox - see PATCHES.md ("streaming writes") for why: an
    in-memory-only builder held gigabytes of decoded attachment data for a
    full mailbox and thrashed a 6.8GB desktop into a hard freeze during
    testing. Only small per-block metadata (bid, offset, size) is retained
    until `write()` assembles the header/B-tree/AMap pages that must be built
    after every block's final position is known.
    """

    def __init__(self, output_path, display_name="Personal Folders"):
        self.display_name = display_name
        self._output_path = Path(output_path)
        self._f = open(self._output_path, 'w+b')
        self._next_nid_index = 32  # Start after reserved NIDs
        # BID bit 1 (i-bit) distinguishes leaf (0) from internal (1) blocks.
        # Leaf BIDs (data blocks): 4, 8, 12, 16, ... (BID % 4 == 0)
        # Internal BIDs (SLBLOCK etc): 6, 10, 14, 18, ... (BID % 4 == 2)
        self._next_leaf_bid = 4
        self._next_internal_bid = 6
        self._next_page_bid = 5  # Odd BIDs are for pages

        # Collected nodes: list of (nid, data_bid, subnode_bid, parent_nid)
        self._nodes = []
        # Metadata only for every data block already written to disk:
        # list of (bid, offset, raw_size). Block offsets only ever depend on
        # blocks written so far, so they can be (and are) assigned immediately
        # in _write_block rather than deferred to a final layout pass.
        self._data_block_meta = []
        # Highest nidIndex seen per NID type, for the header's rgnid
        # high-water marks (see ndb/header.py). Every NID that ends up in
        # the file is routed through _note_nid(), including subnode NIDs -
        # scanpst counts those too (its computed NID_TYPE_LTP mark matches
        # the PR_ATTACH_DATA_BIN subnode, which is never a top-level node).
        self._nid_high_water = [0] * 32
        # after the first AMap page and any legacy map pages that follow it
        self._current_offset = FIRST_AMAP_OFFSET + reserved_bytes_at_amap_index(0)
        # Folder structure for building hierarchy
        self._folders = {}  # nid -> {name, parent_nid, subfolder_nids, message_nids}
        self._messages = {}  # msg_nid -> small summary dict (NOT the full parsed_eml - see add_message)
        self._root_nid = NID_ROOT_FOLDER
        # Pre-initialize root folder so add_folder() can register subfolders
        self._folders[self._root_nid] = {
            'name': 'Top of Personal Folders',
            # The root folder is its own parent. This looks like a cycle and
            # was briefly "fixed" to 0 on the theory that it caused M365's
            # MaxDepthSurpassed error - but a genuine Outlook-authored PST
            # (an Outlook-written reference file) has `0x122 parent=0x122` too, so this
            # is exactly what Microsoft writes and the change was reverted.
            # Do not "fix" it again without a real Outlook-written file that
            # says otherwise. See PATCHES.md patch 41.
            'parent_nid': self._root_nid,
            'subfolder_nids': [],
            'message_nids': [],
        }

        # A real Outlook-authored PST has THREE levels, not two: the store
        # root, a child folder that is the IPM subtree ("Outlook Data File"),
        # and the user's folders beneath that. Verified against
        # an Outlook-written reference PST, whose NBT reads:
        #     0x122  root          parent=0x122
        #       0x8022  "Outlook Data File"   parent=0x122   <- IPM subtree
        #         0x8062  "Inbox"             parent=0x8022  <- user folder
        # This writer used to collapse the first two, making the root itself
        # the IPM subtree with period folders directly beneath it. Outlook
        # accepts that, but it is not what Outlook writes, and PidTagIpmSubTree
        # EntryId then points at the root rather than at a child of it.
        self._ipm_subtree_nid = self._alloc_nid(NID_TYPE_NORMAL_FOLDER)
        self._folders[self._ipm_subtree_nid] = {
            'name': IPM_SUBTREE_NAME,
            'parent_nid': self._root_nid,
            'subfolder_nids': [],
            'message_nids': [],
        }
        self._folders[self._root_nid]['subfolder_nids'].append(self._ipm_subtree_nid)

        # Alongside the subtree, a real store has a wastebasket and a finder
        # folder, and declares both in the message store's properties. Ours
        # declared neither: PidTagValidFolderMask said only the IPM subtree
        # existed, and PidTagIpmWastebasketEntryId / PidTagFinderEntryId were
        # absent entirely. The evidence that this matters is direct - when
        # Outlook opened our PSTs it *created* "Deleted Items" and "Search
        # Root" itself, because they were missing, and a genuine
        # Outlook-authored PST has them from the start.
        self._wastebasket_nid = self._alloc_nid(NID_TYPE_NORMAL_FOLDER)
        self._folders[self._wastebasket_nid] = {
            'name': WASTEBASKET_NAME,
            'parent_nid': self._root_nid,
            'subfolder_nids': [],
            'message_nids': [],
        }
        self._folders[self._root_nid]['subfolder_nids'].append(self._wastebasket_nid)

        self._finder_nid = self._alloc_nid(NID_TYPE_NORMAL_FOLDER)
        self._folders[self._finder_nid] = {
            'name': FINDER_NAME,
            'parent_nid': self._root_nid,
            'subfolder_nids': [],
            'message_nids': [],
        }
        self._folders[self._root_nid]['subfolder_nids'].append(self._finder_nid)

    def _alloc_data_bid(self):
        """Allocate a leaf data block BID (i-bit = 0, BID % 4 == 0)."""
        bid = self._next_leaf_bid
        self._next_leaf_bid += 4
        return bid

    def _alloc_internal_bid(self):
        """Allocate an internal block BID (i-bit = 1, BID % 4 == 2)."""
        bid = self._next_internal_bid
        self._next_internal_bid += 4
        return bid

    def _alloc_page_bid(self):
        """Allocate a page BID (odd number)."""
        bid = self._next_page_bid
        if bid % 2 == 0:
            bid += 1
        self._next_page_bid = bid + 2
        return bid

    def _note_nid(self, nid):
        """Record a NID against its type's high-water mark."""
        nid_type = nid & 0x1F
        index = (nid >> 5) & 0x7FFFFFF
        if index > self._nid_high_water[nid_type]:
            self._nid_high_water[nid_type] = index

    def _alloc_nid(self, nid_type):
        """Allocate a new NID of the given type.

        Indexes belonging to the standard top-level tables are skipped: those
        NIDs are fixed by the format, and a folder handed the same index would
        derive a colliding hierarchy/contents table NID. See
        messaging.special.RESERVED_NID_INDEXES.
        """
        while self._next_nid_index in RESERVED_NID_INDEXES:
            self._next_nid_index += 1
        idx = self._next_nid_index
        self._next_nid_index += 1
        return make_nid(nid_type, idx)

    def _store_data_block(self, data):
        """Store data as a leaf block (i-bit=0), return the BID."""
        if not (len(data) <= MAX_BLOCK_DATA):
            raise PSTWriteError(f"leaf block data too large: {len(data)}")
        bid = self._alloc_data_bid()
        self._write_block(bid, data)
        return bid

    def _store_internal_block(self, data):
        """Store data as an internal block (i-bit=1, for SLBLOCK/XBLOCK), return the BID."""
        if not (len(data) <= MAX_BLOCK_DATA):
            raise PSTWriteError(f"internal block data too large: {len(data)}")
        bid = self._alloc_internal_bid()
        self._write_block(bid, data)
        return bid

    def _write_block(self, bid, data):
        """Assign this block's final file offset and write it immediately.

        Offsets only ever depend on the blocks written so far (each just
        advances a running cursor, skipping over fixed AMap page slots), so
        there's no need to wait until every message is known to place a block
        - unlike the header/B-tree/AMap pages built in write(), which do need
        every block's final position first.
        """
        total = block_total_size(len(data))
        offset = _skip_amap_pages(self._current_offset, total)
        self._current_offset = offset + total
        self._f.seek(offset)
        self._f.write(pack_block(data, bid, offset))
        self._data_block_meta.append((bid, offset, len(data)))

    def _store_xblock_tree(self, bid_size_pairs, total_data_bytes):
        """Store (bid, size) leaf entries as an XBLOCK tree, return the tree's root BID.

        Builds a single-level XBLOCK when everything fits in one page. When there
        are more than MAX_ENTRIES_PER_BLOCK leaf BIDs (i.e. the value is roughly
        >8MB, since each leaf block holds up to 8176 bytes), builds the two-level
        tree the [MS-PST] spec requires instead: leaf BIDs are grouped into
        multiple level-1 XBLOCKs, and a single level-2 XXBLOCK references those.
        (Not just theoretical - this is the exact case that crashed on a real
        ~36MB attachment from the archive; see REVERSE_ENGINEERING_LOG.md.)
        """
        if len(bid_size_pairs) <= MAX_ENTRIES_PER_BLOCK:
            bids = [bid for bid, _ in bid_size_pairs]
            return self._store_internal_block(build_xblock(bids, total_data_bytes, level=1))

        level1_bids = []
        for i in range(0, len(bid_size_pairs), MAX_ENTRIES_PER_BLOCK):
            group = bid_size_pairs[i:i + MAX_ENTRIES_PER_BLOCK]
            group_bids = [bid for bid, _ in group]
            group_total = sum(size for _, size in group)
            level1_bids.append(
                self._store_internal_block(build_xblock(group_bids, group_total, level=1))
            )

        # A single level-2 XXBLOCK must itself fit in one page too. With
        # MAX_ENTRIES_PER_BLOCK (1021) leaf BIDs per level-1 XBLOCK, one XXBLOCK
        # can address up to 1021 * 1021 leaf blocks (~8GB) - comfortably beyond
        # any single email message, so a third level is not implemented.
        if not (len(level1_bids) <= MAX_ENTRIES_PER_BLOCK):
            raise PSTWriteError(f"value needs {len(level1_bids)} level-1 XBLOCKs, exceeding what a single "
            "level-2 XXBLOCK page can reference - a third tree level would be needed "
            "(not implemented; this would require a >8GB single property value)")
        return self._store_internal_block(build_xblock(level1_bids, total_data_bytes, level=2))

    def _store_node_pages(self, pages):
        """Store HN pages. Single page -> leaf BID, multi-page -> XBLOCK tree BID."""
        if len(pages) == 1:
            return self._store_data_block(pages[0])
        bid_size_pairs = []
        total = 0
        for page in pages:
            bid_size_pairs.append((self._store_data_block(page), len(page)))
            total += len(page)
        return self._store_xblock_tree(bid_size_pairs, total)

    def _store_subnode_data(self, data, chunk_size=None):
        """Store subnode data. Small -> leaf BID, large -> chunked + XBLOCK tree BID.

        chunk_size overrides the default MAX_BLOCK_DATA-sized chunking. A TC's
        Row Matrix needs this: per [MS-PST] 2.3.4.4, each data block "MUST
        store an integral number of rows" - rows may never span a block
        boundary. Plain byte-count chunking (the default here) doesn't know
        about row boundaries and will split a row across two blocks whenever
        MAX_BLOCK_DATA isn't a multiple of the row size, which is corrupt per
        spec. Callers with row-shaped data pass a chunk_size that's already a
        whole multiple of their row size (see messaging.folder/message's
        row-aligned chunk size, computed in ltp/tc.py). See PATCHES.md.
        """
        if len(data) <= MAX_BLOCK_DATA:
            return self._store_data_block(data)

        # Every block of a data tree except the last must be *full*
        # (MAX_BLOCK_DATA), per [MS-PST] 2.3.4.4's "each block except the last
        # one MUST have a size of 8192 bytes". With a row-aligned chunk_size
        # that usually leaves a few bytes over - e.g. a Contents Table whose
        # rows are 86 bytes fits 95 whole rows in 8170, six short of 8176 - so
        # the remainder is padded out as the "dead space" the same section
        # tells readers to ignore. Emitting the short 8170-byte block instead
        # made real Outlook reject the node outright ("Block size does not
        # match parent's. Middle page not full: cEnt=22, cbBid=8170") and
        # rebuild the whole NBT. Rows per block stay exactly
        # floor(MAX_BLOCK_DATA / row size), which is what a reader computes.
        padded = subnode_data_chunks(data, chunk_size)
        bid_size_pairs = [(self._store_data_block(c), len(c)) for c in padded]
        return self._store_xblock_tree(bid_size_pairs, sum(len(c) for c in padded))

    def _build_sl_bid(self, sl_entries):
        """Build SLBLOCK from entries and return internal BID, or 0 if empty.

        SLBLOCK entries are a BTree leaf keyed by subnode NID, so they MUST be
        in ascending NID order - readers locate a subnode by searching the
        sorted keys, not by scanning. This code appended them in construction
        order instead: recipients (0x692), then attachments (0x671), then each
        attachment's own subnode (0x5...), i.e. strictly *descending*. Real
        Outlook's Inbox Repair Tool reported it as "SBLOCK (leaf) keys are not
        ascending <0, 692> <1, 671>", and it plausibly also explains its
        inability to find those very subnodes afterwards ("Attachment table
        ... sub-object not found", "TC (nid=692) missing required column"):
        a key search over unsorted entries fails even though the data is
        present. See PATCHES.md.
        """
        if not sl_entries:
            return 0
        for entry_nid, _bid, _sub in sl_entries:
            self._note_nid(entry_nid)
        return self._store_internal_block(build_sl_block(sorted(sl_entries)))

    @staticmethod
    def _node_stored_bytes(pages, subnodes):
        """Data-block bytes a node occupies: its own pages plus its subnodes'.

        This is the quantity Outlook reports as PR_MESSAGE_SIZE - confirmed
        exactly, on 4,157 of 4,158 messages, against the values the Inbox
        Repair Tool itself wrote (the one exception being the single message
        it did not rewrite because our value already matched). XBLOCK tree
        blocks are deliberately not counted: only leaf data payloads are.
        """
        total = sum(len(p) for p in pages)
        for entry in (subnodes or []):
            data = entry[1]
            chunk_size = entry[2] if len(entry) > 2 else None
            total += sum(len(c) for c in subnode_data_chunks(data, chunk_size))
        return total

    def _store_tc_or_pc(self, pages, subnodes):
        """Store a PC or TC node's pages and subnodes, return (data_bid, sub_bid).

        Each subnode entry is (nid, data) or, for row-shaped data that must
        chunk on row boundaries (see _store_subnode_data), (nid, data,
        chunk_size).
        """
        data_bid = self._store_node_pages(pages)
        sub_bid = 0
        if subnodes:
            sl = []
            for entry in subnodes:
                sn, sd = entry[0], entry[1]
                chunk_size = entry[2] if len(entry) > 2 else None
                sl.append((sn, self._store_subnode_data(sd, chunk_size), 0))
            sub_bid = self._build_sl_bid(sl)
        return data_bid, sub_bid

    def _add_node(self, nid, pages, subnodes=None, extra_sl_entries=None, parent_nid=0):
        """Add a node with its HN pages and optional subnodes.

        Args:
            nid: Node ID.
            pages: List of bytes (HN pages from build_pc_node/build_tc_node).
            subnodes: List of (nid, data_bytes) for large values.
            extra_sl_entries: Additional (nid, bid_data, bid_sub) SL entries.
            parent_nid: Parent node ID.
        """
        data_bid = self._store_node_pages(pages)

        sl_entries = list(extra_sl_entries or [])
        if subnodes:
            for sub_nid, sub_data in subnodes:
                sub_bid = self._store_subnode_data(sub_data)
                sl_entries.append((sub_nid, sub_bid, 0))

        sub_bid = self._build_sl_bid(sl_entries)
        self._note_nid(nid)
        self._nodes.append((nid, data_bid, sub_bid, parent_nid))
        return data_bid

    def _add_empty_node(self, nid):
        """Add an NBT entry with no data and no subnodes (bidData/bidSub = 0).

        The search-related objects exist in exactly this form in a PST that
        real Outlook opens: present in the NBT, pointing at nothing. Giving
        them an empty HN page instead would be a different thing entirely.
        """
        self._note_nid(nid)
        self._nodes.append((nid, 0, 0, 0))

    def add_folder(self, name, parent_nid=None):
        """Add a folder to the PST.

        Args:
            name: Folder display name.
            parent_nid: Parent folder NID (None = the IPM subtree, which is
                where user folders belong - not the store root).

        Returns:
            NID of the new folder.
        """
        if parent_nid is None:
            parent_nid = self._ipm_subtree_nid

        folder_nid = self._alloc_nid(NID_TYPE_NORMAL_FOLDER)

        self._folders[folder_nid] = {
            'name': name,
            'parent_nid': parent_nid,
            'subfolder_nids': [],
            'message_nids': [],
        }

        # Register as subfolder of parent
        if parent_nid in self._folders:
            self._folders[parent_nid]['subfolder_nids'].append(folder_nid)

        return folder_nid

    def add_message(self, folder_nid, parsed_eml):
        """Add a message to a folder.

        Args:
            folder_nid: NID of the target folder.
            parsed_eml: Dict from eml_parser.parse_eml_file().

        Returns:
            NID of the new message.
        """
        msg_nid = self._alloc_nid(NID_TYPE_NORMAL_MESSAGE)

        if folder_nid in self._folders:
            self._folders[folder_nid]['message_nids'].append(msg_nid)

        # Keep only the small summary fields the folder's Contents TC row
        # needs later in _build_folder_nodes() - not the full parsed_eml dict,
        # which can hold multi-MB decoded attachment/body bytes per message
        # and was the main reason a full-mailbox build held gigabytes in RAM.
        # message_row_values() is the single source for every value that has
        # to agree between this message's PC and its Contents Table row, so
        # compute it once here - while the full parsed_eml (recipients
        # included) is still in scope - and keep only the small result. The
        # timestamp is sampled once and passed to both for the same reason.
        now = filetime_now()

        # Build SL entries for recipients/attachments TCs, accumulating the
        # data-block bytes each occupies - they are part of PR_MESSAGE_SIZE.
        extra_sl = []
        sub_object_bytes = 0

        if parsed_eml.get('recipients'):
            recip_pages, recip_subnodes = build_recipients_tc(parsed_eml['recipients'])
            sub_object_bytes += self._node_stored_bytes(recip_pages, recip_subnodes)
            recip_bid, recip_sub_bid = self._store_tc_or_pc(recip_pages, recip_subnodes)
            recip_nid = message_nid_recipients(msg_nid)
            extra_sl.append((recip_nid, recip_bid, recip_sub_bid))

        if parsed_eml.get('attachments'):
            attach_pages, attach_subnodes = build_attachments_tc(parsed_eml['attachments'])
            sub_object_bytes += self._node_stored_bytes(attach_pages, attach_subnodes)
            attach_bid, attach_sub_bid = self._store_tc_or_pc(attach_pages, attach_subnodes)
            attach_nid = message_nid_attachments(msg_nid)
            extra_sl.append((attach_nid, attach_bid, attach_sub_bid))

            # Per [MS-PST] 2.4.6.2: each attachment gets its own subnode PC
            for i, att in enumerate(parsed_eml['attachments']):
                att_pages, att_subnodes = build_attachment_pc(att, i)
                sub_object_bytes += self._node_stored_bytes(att_pages, att_subnodes)
                att_bid, att_sub_bid = self._store_tc_or_pc(att_pages, att_subnodes)
                att_nid = attachment_subnode_nid(i)
                extra_sl.append((att_nid, att_bid, att_sub_bid))

        # PR_MESSAGE_SIZE is the total data-block bytes of the message and all
        # its sub-objects, so it can only be measured after building them -
        # and it lives inside the very PC it measures. That is not circular:
        # it is a fixed 4-byte inline property that is always present, so its
        # value cannot change the size of anything. Build once to measure,
        # then rebuild with the real value; the assert below is what makes
        # that reasoning checkable rather than assumed.
        probe_pages, probe_subnodes = build_message_pc(parsed_eml, now=now,
                                                       message_size=0)
        message_size = (self._node_stored_bytes(probe_pages, probe_subnodes)
                        + sub_object_bytes)

        msg_pages, msg_subnodes = build_message_pc(parsed_eml, now=now,
                                                  message_size=message_size)
        if not ((self._node_stored_bytes(msg_pages, msg_subnodes)
                + sub_object_bytes) == message_size):
            raise PSTWriteError("PR_MESSAGE_SIZE changed the size it measures for message "
            f"0x{msg_nid:X}")

        self._messages[msg_nid] = {
            'has_attachments': parsed_eml.get('has_attachments', False),
            'row_values': message_row_values(parsed_eml, now,
                                             message_size=message_size),
        }

        # Add message node with PC pages, subnodes, and extra SL entries
        self._add_node(msg_nid, msg_pages, msg_subnodes,
                       extra_sl_entries=extra_sl, parent_nid=folder_nid)

        return msg_nid

    def _build_internal_nodes(self):
        """Build the required internal nodes (store, root folder, name map)."""
        # 1. Message Store (NID 0x21)
        (store_pages, store_subnodes), self._record_key = build_message_store(
            self.display_name,
            ipm_subtree_nid=self._ipm_subtree_nid,
            wastebasket_nid=self._wastebasket_nid,
            finder_nid=self._finder_nid)
        self._add_node(NID_MESSAGE_STORE, store_pages, store_subnodes)

        # 2. Name-to-ID Map (NID 0x61) — required structure with GUID/entry/string streams
        namemap_pages, namemap_subnodes = build_name_to_id_map()
        self._add_node(NID_NAME_TO_ID_MAP, namemap_pages, namemap_subnodes)

        # 3. Standard top-level tables (receive folder table, outgoing queue,
        #    and the empty per-type templates). Absent, Outlook rebuilds the
        #    file on every open - see messaging/special.py.
        for nid, _description, pages, subnodes in build_special_tables():
            self._add_node(nid, pages, subnodes)

        # 4. Search scaffolding and the HMP. These hold no message data, but
        #    real Outlook refuses to open a store without them and hands
        #    straight over to the Inbox Repair Tool - see messaging/special.py.
        for nid in EMPTY_NODE_NIDS:
            self._add_empty_node(nid)
        self._add_node(F01_NID, [F01_DATA])
        hmp_pages, hmp_subnodes = build_hmp()
        self._add_node(HMP_NID, hmp_pages, hmp_subnodes)

    def _build_folder_nodes(self):
        """Build all folder nodes (PC + 3 TCs as separate top-level NBT entries)."""
        for folder_nid, finfo in self._folders.items():
            has_subs = len(finfo['subfolder_nids']) > 0
            msg_count = len(finfo['message_nids'])

            # Folder PC (returns (pages, subnodes))
            pc_pages, pc_subnodes = build_folder_pc(
                finfo['name'],
                content_count=msg_count,
                has_subfolders=has_subs,
            )

            # Build the 3 TC data blocks (separate top-level NBT entries)
            # Hierarchy TC (subfolder list)
            sub_rows = []
            for sub_nid in finfo['subfolder_nids']:
                sub_info = self._folders.get(sub_nid, {})
                # Built from the same map as the subfolder's own PC, so the
                # two cannot disagree - see folder_row_values().
                sub_row = {'_nid': sub_nid}
                sub_row.update(folder_row_values(
                    sub_info.get('name', ''),
                    content_count=len(sub_info.get('message_nids', [])),
                    has_subfolders=len(sub_info.get('subfolder_nids', [])) > 0,
                ))
                sub_rows.append(sub_row)
            hier_nid = folder_nid_hierarchy(folder_nid)
            hier_pages, hier_subnodes = build_hierarchy_tc(sub_rows)
            hier_bid, hier_sub_bid = self._store_tc_or_pc(hier_pages, hier_subnodes)

            # Contents TC (message list)
            msg_rows = []
            for msg_nid in finfo['message_nids']:
                parsed = self._messages.get(msg_nid, {})
                # Every cell comes from the same map the message's own PC was
                # built from. Recomputing any of them here is what produced
                # "row doesn't match sub-object" once per message, twice over
                # (patches 35 and 36). Sender lands in
                # PidTagSentRepresentingName (0x0042001F), which is what a real
                # Contents Table uses; PR_HASATTACH is not a Contents Table
                # column at all. See folder.py.
                row = {'_nid': msg_nid}
                row.update(parsed.get('row_values', {}))
                msg_rows.append(row)
            contents_nid = folder_nid_contents(folder_nid)
            contents_pages, contents_subnodes = build_contents_tc(msg_rows)
            contents_bid, contents_sub_bid = self._store_tc_or_pc(
                contents_pages, contents_subnodes)

            # Associated Contents TC (empty)
            assoc_nid = folder_nid_assoc(folder_nid)
            assoc_pages, assoc_subnodes = build_assoc_contents_tc()
            assoc_bid, assoc_sub_bid = self._store_tc_or_pc(assoc_pages, assoc_subnodes)

            # Add folder PC node
            self._add_node(folder_nid, pc_pages, pc_subnodes,
                           parent_nid=finfo['parent_nid'])

            # Add 3 TC nodes as separate top-level NBT entries (as Outlook does)
            for table_nid in (hier_nid, contents_nid, assoc_nid):
                self._note_nid(table_nid)
            self._nodes.append((hier_nid, hier_bid, hier_sub_bid, 0))
            self._nodes.append((contents_nid, contents_bid, contents_sub_bid, 0))
            self._nodes.append((assoc_nid, assoc_bid, assoc_sub_bid, 0))

    def write(self):
        """Assemble and write the complete PST file to the constructor's output_path.

        Data blocks are already on disk (written incrementally by
        `_write_block` as messages were added) - this only builds and writes
        the pieces that need every block's final position first: folder
        metadata, the NBT/BBT B-tree pages, the AMap pages, and the header.
        """
        # Build internal structures (small - folder/store metadata, not message bodies)
        self._build_internal_nodes()
        self._build_folder_nodes()

        # All data block offsets were assigned incrementally in _write_block;
        # self._current_offset already reflects every block written so far.
        current_offset = self._current_offset

        # Align to page boundary for B-tree pages
        if current_offset % PAGE_SIZE != 0:
            current_offset = ((current_offset + PAGE_SIZE - 1) // PAGE_SIZE) * PAGE_SIZE

        # Phase 2: Build NBT entries
        nbt_entries = []
        for nid, data_bid, sub_bid, parent_nid in self._nodes:
            entry = pack_nbt_entry(nid, data_bid, sub_bid, parent_nid)
            nbt_entries.append(entry)
        nbt_entries.sort(key=lambda e: struct.unpack('<Q', e[:8])[0])

        # Phase 3: Build BBT entries from block metadata (offset, size only - no data held)
        bbt_entries = []
        for bid, offset, raw_size in self._data_block_meta:
            entry = pack_bbt_entry(bid, offset, raw_size)
            bbt_entries.append(entry)
        bbt_entries.sort(key=lambda e: struct.unpack('<Q', e[:8])[0])

        # Phase 4: Build B-tree pages (potentially multi-level)
        page_offset_cursor = [current_offset]  # mutable for closure
        page_offsets = {}  # bid -> offset

        def alloc_page_offset(bid):
            offset = _skip_amap_pages(page_offset_cursor[0], PAGE_SIZE)
            page_offsets[bid] = offset
            page_offset_cursor[0] = offset + PAGE_SIZE
            return offset

        nbt_pages = build_btree_pages(
            nbt_entries, PTTYPE_NBT,
            self._alloc_page_bid, alloc_page_offset
        )
        bbt_pages = build_btree_pages(
            bbt_entries, PTTYPE_BBT,
            self._alloc_page_bid, alloc_page_offset
        )

        # Root pages are the last in each list
        nbt_root_bid, nbt_root_offset, _ = nbt_pages[-1]
        bbt_root_bid, bbt_root_offset, _ = bbt_pages[-1]

        file_eof = page_offset_cursor[0]

        # Phase 5: Build AMap pages (one per AMAP_COVERAGE region) plus the
        # deprecated-but-mandatory PMap/FMap/FPMap pages - see
        # ndb/legacy_maps.py for why those are still required and where they go.
        allocated_ranges = []
        for bid, offset, raw_size in self._data_block_meta:
            total = block_total_size(raw_size)
            allocated_ranges.append((offset, total))
        for _, pg_offset, _ in nbt_pages + bbt_pages:
            allocated_ranges.append((pg_offset, PAGE_SIZE))

        num_amaps = max(1, (file_eof - FIRST_AMAP_OFFSET + AMAP_COVERAGE - 1) // AMAP_COVERAGE)

        # Legacy map pages occupy real space, so the AMap covering each one has
        # to mark it allocated like any other page. (AMap pages themselves
        # don't need listing here - build_amap_page() self-maps its own page,
        # and no AMap page ever falls inside another AMap page's window.)
        legacy_positions = legacy_page_positions(file_eof)
        for offset, _ptype in legacy_positions:
            allocated_ranges.append((offset, PAGE_SIZE))
        allocated_ranges.sort(key=lambda r: r[0])

        amap_pages_list = []  # list of (offset, page_bytes)
        total_amap_free = 0
        last_amap_offset = FIRST_AMAP_OFFSET

        # allocated_ranges is sorted by offset, so each AMap page's relevant
        # slice can be found with a binary search instead of scanning the full
        # (block-count) list per page - passing the full list to
        # build_amap_page()/compute_amap_free() for every one of the (page-count)
        # AMap pages was O(blocks * pages), tens of millions of wasted iterations
        # for a real ~4,000-message mailbox, and wouldn't remotely scale to a
        # full multi-mailbox production run. A single MAX_BLOCK_DATA-sized margin
        # before the window start is enough since no single range in this list
        # exceeds that size.
        offsets = [r[0] for r in allocated_ranges]

        for i in range(num_amaps):
            amap_offset = FIRST_AMAP_OFFSET + i * AMAP_COVERAGE
            window_end = amap_offset + AMAP_COVERAGE
            lo = bisect.bisect_left(offsets, amap_offset - MAX_BLOCK_DATA - 64)
            hi = bisect.bisect_left(offsets, window_end)
            window_ranges = allocated_ranges[lo:hi]

            # Per [MS-PST] 2.2.2.7.7.4: for AMap/PMap/FMap/FPMap pages, the trailer's bid
            # field must equal the page's own absolute file offset - not an allocated BID.
            # Confirmed via a real Outlook "Data File Corruption" error naming the exact
            # expected bid for a known AMap page (see PATCHES.md).
            page = build_amap_page(
                window_ranges, amap_offset, amap_offset, amap_offset
            )
            free = compute_amap_free(
                window_ranges, amap_offset, amap_offset, file_eof
            )
            amap_pages_list.append((amap_offset, page))
            total_amap_free += free
            last_amap_offset = amap_offset

        legacy_pages_list = [
            (offset, build_legacy_map_page(ptype, offset))
            for offset, ptype in legacy_positions
        ]

        # Phase 6: Build header
        root = pack_root(
            file_eof=file_eof,
            ib_amap_last=last_amap_offset,
            cb_amap_free=total_amap_free,
            # Per [MS-PST] 2.2.2.5: because the PMap is deprecated, cbPMapFree
            # SHOULD be zero and creators of new PSTs MUST initialize it to
            # zero. That's only self-consistent with PMap pages that contain
            # no free bits - see ndb/legacy_maps.py.
            cb_pmap_free=0,
            bref_nbt=(nbt_root_bid, nbt_root_offset),
            bref_bbt=(bbt_root_bid, bbt_root_offset),
            f_amap_valid=2,
        )

        header = build_header(
            root_data=root,
            bid_next_p=self._next_page_bid,
            bid_next_b=max(self._next_leaf_bid, self._next_internal_bid),
            unique=1,
            rgnid=self._nid_high_water,
        )

        # Phase 7: Write header, AMap pages, legacy map pages, and B-tree pages at
        # their offsets. Data blocks were already written to self._f as
        # messages were added - only these remaining pieces needed every
        # block's final position first.
        self._f.seek(0)
        self._f.write(header)

        for amap_offset, amap_data in amap_pages_list:
            self._f.seek(amap_offset)
            self._f.write(amap_data)

        for legacy_offset, legacy_data in legacy_pages_list:
            self._f.seek(legacy_offset)
            self._f.write(legacy_data)

        for pg_bid, pg_offset, pg_data in nbt_pages + bbt_pages:
            self._f.seek(pg_offset)
            self._f.write(pg_data)

        self._f.truncate(file_eof)
        self._f.close()

        return self._output_path
