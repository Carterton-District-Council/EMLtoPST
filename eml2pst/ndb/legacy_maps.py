"""Deprecated-but-mandatory map pages: PMap, FMap, FPMap.

All three are functionally deprecated by the Density List, and [MS-PST] says
implementations SHOULD NOT *use* them - but for each one it also says their
"presence ... at the correct intervals MUST be preserved, and all
corresponding checksums MUST be maintained for a PST file to remain valid"
(2.2.2.7.3 / 2.2.2.7.5 / 2.2.2.7.6). Real Outlook's Inbox Repair Tool
enforces exactly that: it validates ptype/CRC/signature/bid at the required
offsets and, when they're absent, reads whatever message data happens to sit
there and reports it as corruption. See PATCHES.md.

Because nothing is allowed to *use* these for allocation decisions, their
contents are filled with 0xFF - matching the convention the header itself
already uses for its own deprecated in-header copies of these maps
(`rgbFM`/`rgbFP` at 0x100/0x180, both 128 bytes of 0xFF). For the PMap
specifically this is also what makes the file self-consistent: [MS-PST]
2.2.2.5 ROOT requires `cbPMapFree` to be zero in newly-created PSTs, and an
all-0xFF PMap is one with no free bits, which is what "zero free space"
means. An all-zero PMap (the natural but wrong reading) makes scanpst report
"PMap page <@N> has free bits, but the header says it doesn't".

Positions - all derived from, and cross-checked against, the sizes of the
header's own in-header copies of these maps:

- PMap: one per 8 AMap pages (a PMap bit covers a 512-byte page vs an AMap
  bit's 64-byte block). First at absolute offset 0x4600, immediately after
  the first AMap page.
- FMap: one byte per AMap page, so one FMap page covers 496 AMap pages. The
  header's `rgbFM` is 128 bytes = the first 128 AMap pages, so the first
  on-disk FMap page picks up at AMap index 128, then every 496 after that.
  Confirmed empirically: scanpst demanded FMap pages at AMap indices 128,
  624, 1120, 1616 and 2112 - exactly this sequence.
- FPMap: one bit per PMap page, so one FPMap page covers 496*8 = 3968 PMap
  pages. The header's `rgbFP` is 128 bytes = 1024 bits = the first 1024 PMap
  pages, so the first on-disk FPMap page picks up at PMap index 1024, i.e.
  AMap index 8192 (~2.08 GB into the file), then every 31744 AMap pages.
  This mirrors the FMap derivation exactly, but is NOT empirically confirmed
  - the test mailbox is far below 2 GB, so no FPMap page is required in it
  and scanpst has never had occasion to check one. See PATCHES.md.
"""

import struct
from ..crc import compute_crc
from .btree import PAGE_SIZE
from .amap import AMAP_COVERAGE, FIRST_AMAP_OFFSET
from ..errors import PSTWriteError

MAP_DATA_SIZE = 496  # 512 - 16 (page trailer)

# ptype values per [MS-PST] 2.2.2.7.1. (btree.py has its own constants for
# these, but its names for 0x82/0x83/0x85 don't match the spec's meanings, so
# they're defined explicitly here rather than imported and silently misread.)
PTYPE_FMAP = 0x82
PTYPE_PMAP = 0x83
PTYPE_FPMAP = 0x85

# PMap: one per 8 AMap pages.
PMAP_AMAP_INTERVAL = 8
# FMap: 496 bytes, one byte per AMap page; header's rgbFM covers the first 128.
FMAP_FIRST_AMAP_INDEX = 128
FMAP_AMAP_INTERVAL = 496
# FPMap: 496 bytes = 3968 bits, one bit per PMap page; header's rgbFP (128
# bytes = 1024 bits) covers the first 1024 PMap pages.
FPMAP_FIRST_AMAP_INDEX = 1024 * PMAP_AMAP_INTERVAL  # 8192
FPMAP_AMAP_INTERVAL = 3968 * PMAP_AMAP_INTERVAL  # 31744


def legacy_pages_at_amap_index(amap_index):
    """Return the ptypes of deprecated map pages that follow AMap `amap_index`.

    Returned in the order they are laid out on disk, each occupying its own
    512-byte page immediately after the AMap page (and after each other when
    several coincide at the same AMap boundary - which they do, since all the
    intervals here are multiples of 8).
    """
    ptypes = []
    if amap_index % PMAP_AMAP_INTERVAL == 0:
        ptypes.append(PTYPE_PMAP)
    if (amap_index >= FMAP_FIRST_AMAP_INDEX
            and (amap_index - FMAP_FIRST_AMAP_INDEX) % FMAP_AMAP_INTERVAL == 0):
        ptypes.append(PTYPE_FMAP)
    if (amap_index >= FPMAP_FIRST_AMAP_INDEX
            and (amap_index - FPMAP_FIRST_AMAP_INDEX) % FPMAP_AMAP_INTERVAL == 0):
        ptypes.append(PTYPE_FPMAP)
    return ptypes


def reserved_bytes_at_amap_index(amap_index):
    """Total bytes reserved at an AMap boundary: the AMap page plus any legacy maps."""
    return PAGE_SIZE * (1 + len(legacy_pages_at_amap_index(amap_index)))


def legacy_page_positions(file_eof):
    """Enumerate every deprecated map page the file needs, as (offset, ptype).

    Positions depend only on file size, not on content, so this can be called
    at any point once the final EOF is known.
    """
    positions = []
    amap_index = 0
    while True:
        amap_offset = FIRST_AMAP_OFFSET + amap_index * AMAP_COVERAGE
        if amap_offset >= file_eof:
            break
        offset = amap_offset + PAGE_SIZE
        for ptype in legacy_pages_at_amap_index(amap_index):
            positions.append((offset, ptype))
            offset += PAGE_SIZE
        amap_index += 1
    return positions


def build_legacy_map_page(ptype, page_offset):
    """Build a 512-byte PMap/FMap/FPMap page.

    Args:
        ptype: PTYPE_PMAP, PTYPE_FMAP or PTYPE_FPMAP.
        page_offset: This page's own absolute file offset. Per [MS-PST]
            2.2.2.7.7.4 the trailer's bid field for a map page is the page's
            own file offset (same convention as AMap - see PATCHES.md).

    Returns:
        512 bytes of page data.
    """
    # 0xFF throughout: "fully allocated / no free space", the only content
    # consistent with these maps being unusable for allocation and with the
    # header's required cbPMapFree of zero. See module docstring.
    bitmap = b'\xFF' * MAP_DATA_SIZE

    crc = compute_crc(bitmap)
    trailer = struct.pack('<BB H I Q',
                          ptype,  # ptype
                          ptype,  # ptypeRepeat
                          0,  # wSig - always 0 for map pages
                          crc,  # dwCRC
                          page_offset)  # bid == own file offset

    page = bitmap + trailer
    if not (len(page) == PAGE_SIZE):
        raise PSTWriteError('legacy_maps.py: invariant failed: len(page) == PAGE_SIZE')
    return page
