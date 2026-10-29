"""XBLOCK (extended data tree block) for PST NDB layer.

An XBLOCK connects multiple data blocks into a data tree, used when
a node's data exceeds a single block (e.g. multi-page Heap-on-Node).

See [MS-PST] 2.2.2.8.3.2.
"""

import struct

XBLOCK_BTYPE = 0x01
XBLOCK_LEVEL = 0x01  # Level 1 = references data blocks (level 0)

# Per [MS-PST] 2.2.2.8.3.2.1: header is 8 bytes (btype, cLevel, cEnt, lcbTotal),
# each BID entry is 8 bytes, and a block's raw data must fit in MAX_BLOCK_DATA
# (8176) bytes. (8176 - 8) // 8 = 1021 entries max per single XBLOCK/XXBLOCK page.
MAX_ENTRIES_PER_BLOCK = (8176 - 8) // 8


def build_xblock(bid_list, total_data_bytes, level=1):
    """Build a single-page XBLOCK/XXBLOCK data tree block.

    Only builds one page - does not check whether bid_list fits. Callers with
    more than MAX_ENTRIES_PER_BLOCK BIDs must build a two-level tree themselves
    (see build_xblock_tree / pst_file._store_subnode_data).

    Args:
        bid_list: List of BIDs (data blocks for level 1, XBLOCKs for level 2+).
        total_data_bytes: Total bytes of raw leaf data covered by this page's subtree.
        level: 1 for a leaf-referencing XBLOCK, 2 for an XBLOCK-referencing XXBLOCK.

    Returns:
        Raw bytes for the XBLOCK block data (before block packing/trailer).
    """
    cEnt = len(bid_list)
    if cEnt > MAX_ENTRIES_PER_BLOCK:
        raise ValueError(
            f"{cEnt} entries exceeds MAX_ENTRIES_PER_BLOCK ({MAX_ENTRIES_PER_BLOCK}) "
            "for a single XBLOCK/XXBLOCK page - caller must split into a two-level tree"
        )

    # Header: btype(1) + cLevel(1) + cEnt(2) + lcbTotal(4) = 8 bytes
    header = struct.pack('<BB H I',
                         XBLOCK_BTYPE,
                         level,
                         cEnt,
                         total_data_bytes)

    # BID array: 8 bytes each
    bid_data = b''
    for bid in bid_list:
        bid_data += struct.pack('<Q', bid)

    return header + bid_data
