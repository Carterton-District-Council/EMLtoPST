"""Standard top-level store objects that every real PST carries.

A PST created by Outlook has a set of store-level objects beyond the folders
and messages: a receive folder table, an outgoing queue, and a family of empty
"template" tables that define the canonical column set for each table type.
This pipeline never created them, so the Inbox Repair Tool reported them
missing and rebuilt them on every pass:

    !!Receive folder table missing
    !!Receive folder table missing default message class
    !!Missing the outgoing queue
    !!Missing template (nid=60D) ... (nid=6F8)

These are a missing feature rather than corruption - no message data depends on
them - but their absence is enough to make Outlook rewrite the file, which
defeats the goal of a PST that imports without a repair pass.

Everything here is transcribed from a real Outlook-repaired PST rather than
from spec prose: the NIDs, the exact column sets, and the fact that all of
them except the receive folder table are *empty* tables (zero rows). That has
been the method that resolved every table-shaped defect in this series - see
PATCHES.md.

The receive folder table is the one exception: it carries a single row mapping
the default message class (the empty string, which is what "default message
class" means in that log line) to the root folder.
"""

import struct

from ..ltp.heap import HeapOnNode, make_hid
from ..ltp.tc import build_tc_node
from ..mapi.properties import NID_ROOT_FOLDER

PR_MESSAGE_CLASS = 0x001A001F
PR_RECEIVE_FOLDER_SETTINGS = 0x66050003

# (nid, description, user column tags). PidTagLtpRowId/PidTagLtpRowVer
# (0x67F20003 / 0x67F30003) are added by build_tc_node() itself and are
# deliberately not listed here.
SPECIAL_TABLES = (
    (0x60D, 'hierarchy table template', (
        0x0E300102, 0x0E330014, 0x0E340102, 0x0E380003, 0x3001001F,
        0x36020003, 0x36030003, 0x360A000B, 0x3613001F, 0x66350003,
        0x66360003,
    )),
    (0x60E, 'contents table template', (
        0x00170003, 0x001A001F, 0x00360003, 0x0037001F, 0x00390040,
        0x0042001F, 0x0057000B, 0x0058000B, 0x0070001F, 0x00710102,
        0x0E03001F, 0x0E04001F, 0x0E060040, 0x0E070003, 0x0E080003,
        0x0E170003, 0x0E300102, 0x0E330014, 0x0E340102, 0x0E380003,
        0x0E3C0102, 0x0E3D0102, 0x10970003, 0x30080040, 0x30130102,
        0x65C60003,
    )),
    (0x60F, 'associated contents table template', (
        0x001A001F, 0x0E070003, 0x0E170003, 0x3001001F, 0x6800001F,
        0x6803000B, 0x68051003, 0x682F001F, 0x70030003, 0x70040102,
        0x70050102, 0x7006001F, 0x70070003,
    )),
    (0x610, 'search contents table template', (
        0x00170003, 0x001A001F, 0x00360003, 0x0037001F, 0x0042001F,
        0x0057000B, 0x0058000B, 0x0E03001F, 0x0E04001F, 0x0E05001F,
        0x0E060040, 0x0E070003, 0x0E080003, 0x0E170003, 0x0E2A000B,
        0x30080040, 0x67F10003,
    )),
    (0x62B, 'receive folder table', (
        PR_MESSAGE_CLASS, PR_RECEIVE_FOLDER_SETTINGS,
    )),
    (0x64C, 'outgoing queue table', (
        0x00390040, 0x0E100003, 0x0E140003,
    )),
    (0x671, 'attachment table template', (
        0x0E200003, 0x3704001F, 0x37050003, 0x370B0003,
    )),
    (0x692, 'recipient table template', (
        0x0C150003, 0x0E0F000B, 0x0FF90102, 0x0FFE0003, 0x0FFF0102,
        0x3001001F, 0x3002001F, 0x3003001F, 0x300B0102, 0x39000003,
        0x39FF001F, 0x3A40000B,
    )),
    (0x6B6, 'template table (NID type 0x16)', (
        0x0E330014, 0x0E370102, 0x0E380003,
    )),
    (0x6D7, 'template table (NID type 0x17)', (
        0x001A001F, 0x0E300102, 0x0E310102, 0x0E330014, 0x0E340102,
        0x0E380003, 0x0E3E0102,
    )),
    (0x6F8, 'template table (NID type 0x18)', (
        0x0E330014, 0x30070040,
    )),
)

RECEIVE_FOLDER_TABLE_NID = 0x62B

# These NIDs are fixed by the format, but their *index* part (nid >> 5) comes
# from the same space _alloc_nid() hands out. A folder allocated the matching
# index would derive its hierarchy table NID as 0x60D and collide with the
# template - which a bulk run reaches easily, since packing a mailbox creates
# one folder per archive period. The allocator skips these.
RESERVED_NID_INDEXES = frozenset(nid >> 5 for nid, _, _ in SPECIAL_TABLES)


def build_special_tables(receive_folder_nid=NID_ROOT_FOLDER):
    """Build every standard top-level table.

    Yields (nid, description, pages, subnodes) ready for _add_node().

    All are empty except the receive folder table, which gets the single
    default-message-class row Outlook expects: message class "" -> the folder
    delivered mail belongs to.
    """
    for nid, description, tags in SPECIAL_TABLES:
        if nid == RECEIVE_FOLDER_TABLE_NID:
            rows = [{
                '_nid': 1,
                PR_MESSAGE_CLASS: '',
                PR_RECEIVE_FOLDER_SETTINGS: receive_folder_nid,
            }]
        else:
            rows = []
        pages, subnodes = build_tc_node(list(tags), rows)
        yield nid, description, pages, subnodes


# ---------------------------------------------------------------------------
# Search scaffolding and HMP
#
# Real Outlook refuses to open a PST that lacks these and hands straight over
# to the Inbox Repair Tool, which is why they are built despite holding no
# message data. Everything below is transcribed from a reference PST that real Outlook
# opened successfully (an Inbox Repair Tool output) - rather than from
# [MS-PST], which does not document the HMP at all.
# ---------------------------------------------------------------------------

# Nodes that exist purely as NBT entries with no data block whatsoever
# (bidData = 0, bidSub = 0) in the accepted reference:
#   0x1E1 search management queue, 0x201 search activity list,
#   0xEC1 / 0xEE1 / 0xF21 search folder objects.
EMPTY_NODE_NIDS = (0x1E1, 0x201, 0xEC1, 0xEE1, 0xF21)

# 0xF01: 104 bytes, only two of them non-zero. Byte 0 = 8 and byte 32 = 1.
F01_NID = 0x0F01
F01_DATA = (b'\x08' + b'\x00' * 31 + b'\x01' + b'\x00' * 71)

# The HMP (0xC01). Undocumented, and Outlook grows it in use - a well-used
# store's copy runs to ~105 KB - but the minimal form Outlook accepts is a
# single 56-byte HN holding a BTH that maps one 16-byte GUID to a 4-byte
# value. Byte-for-byte layout from the reference:
#     HNHDR   ibHnpm=0x2C sig=0xEC bClientSig=0x9C hidUserRoot=0x20
#     alloc 1 (4 bytes)  = 0x40, the HID of the BTH header
#     alloc 2 (8 bytes)  = BTHHEADER: cbKey=16, cbEnt=4, bIdxLevels=0, root=0x60
#     alloc 3 (20 bytes) = one leaf entry: GUID + 0x00000402
HMP_NID = 0x0C01
HMP_CLIENT_SIG = 0x9C
HMP_GUID = bytes.fromhex('6eb80a836cb3d0449d9ff9bff3808795')
HMP_VALUE = 0x00000402


def build_hmp():
    """Build the minimal HMP node. Returns (pages, subnodes)."""
    hn = HeapOnNode(client_sig=HMP_CLIENT_SIG)
    root_slot = hn.allocate_placeholder(4)          # HID 0x20
    bth_header = hn.allocate_placeholder(8)         # HID 0x40
    leaf = hn.allocate(HMP_GUID + struct.pack('<I', HMP_VALUE))  # HID 0x60

    hn.patch(root_slot, struct.pack('<I', bth_header))
    # BTHHEADER: bType=0xB5, cbKey=16, cbEnt=4, bIdxLevels=0, hidRoot=leaf
    hn.patch(bth_header, struct.pack('<BBBBI', 0xB5, 16, 4, 0, leaf))
    hn.set_user_root(root_slot)
    return hn.build(), []
