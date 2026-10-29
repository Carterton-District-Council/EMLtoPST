"""Allocation Map (AMap) page for PST NDB layer.

The AMap tracks which bytes in the PST file are allocated.
Each bit in the AMap represents one 64-byte slot.
See [MS-PST] 2.2.2.7.4.
"""

import struct
from ..crc import compute_crc
from .btree import PAGE_SIZE, PTTYPE_AMAP
from ..errors import PSTWriteError

# AMap covers file bytes starting after the header
# First AMap starts at offset 0x4400 and covers bytes from 0x4400
# Each AMap page has 496 data bytes = 3968 bits = covers 3968 * 64 = 253952 bytes
AMAP_DATA_SIZE = 496  # 512 - 16 (page trailer)
AMAP_BITS = AMAP_DATA_SIZE * 8  # 3968 bits
AMAP_COVERAGE = AMAP_BITS * 64  # 253952 bytes per AMap page

# First AMap offset in file
FIRST_AMAP_OFFSET = 0x4400


def _set_slot(bitmap, slot):
    """Mark one 64-byte slot allocated, MSB-first within each byte.

    Slot N lives at bit (7 - N%8) of byte N//8 - i.e. the *most* significant
    bit first, the same convention a TC row's Cell Existence Bitmap uses.
    This code originally wrote LSB-first, which is invisible in any byte that
    is fully allocated (0xFF either way) and only shows at the boundary
    between allocated and free space - so it produced exactly one bad byte at
    the end of each AMap window's packed run, and real Outlook reported
    ~1,615 "Block is not allocated in AMAP!" complaints clustered precisely
    there. Confirmed against a real Outlook-accepted PST: over 412 partial
    bytes, MSB-first reproduces its bitmaps in 3296/3296 bit positions with
    zero mismatches, while LSB-first scores no better than chance. See
    PATCHES.md.
    """
    if 0 <= slot < AMAP_BITS:
        bitmap[slot // 8] |= (1 << (7 - (slot % 8)))


def build_amap_page(allocated_ranges, amap_offset, file_base_offset, bid):
    """Build a 512-byte AMap page.

    Args:
        allocated_ranges: List of (offset, size) tuples for allocated regions.
        amap_offset: File offset of this AMap page.
        file_base_offset: File offset that bit 0 of this AMap represents.
        bid: Page BID for this AMap.

    Returns:
        512 bytes of AMap page data.
    """
    bitmap = bytearray(AMAP_DATA_SIZE)

    for alloc_offset, alloc_size in allocated_ranges:
        # Calculate which bits this allocation covers
        if alloc_offset < file_base_offset:
            continue
        rel_offset = alloc_offset - file_base_offset
        start_slot = rel_offset // 64
        # Round up the size to 64-byte slots
        num_slots = (alloc_size + 63) // 64

        for slot in range(start_slot, start_slot + num_slots):
            _set_slot(bitmap, slot)

    # Mark the AMap page itself as allocated
    amap_rel = (amap_offset - file_base_offset) // 64
    amap_slots = PAGE_SIZE // 64  # 8 slots
    for slot in range(amap_rel, amap_rel + amap_slots):
        _set_slot(bitmap, slot)

    # Compute CRC and build page trailer
    crc = compute_crc(bytes(bitmap))
    trailer = struct.pack('<BB H I Q',
                          PTTYPE_AMAP,  # ptype
                          PTTYPE_AMAP,  # ptypeRepeat
                          0,  # wSig
                          crc,  # dwCRC
                          bid)  # bid

    page = bytes(bitmap) + trailer
    if not (len(page) == 512):
        raise PSTWriteError('amap.py: invariant failed: len(page) == 512')
    return page


def compute_amap_free(allocated_ranges, amap_offset, file_base_offset, file_eof=None):
    """Compute free space in an AMap page's coverage area.

    file_eof, when given, caps the coverage area at the end of the file. The
    last AMap page's nominal AMAP_COVERAGE window almost always extends past
    EOF, and space that doesn't exist isn't free space - counting it inflates
    the header's cbAMapFree. Real Outlook's Inbox Repair Tool caught exactly
    this: "Computed cbAMapFree of 33267136, but header has 33346496" - a
    79,360-byte difference that was precisely the last window's overhang past
    EOF. See PATCHES.md.
    """
    coverage_end = file_base_offset + AMAP_COVERAGE
    if file_eof is not None:
        coverage_end = min(coverage_end, file_eof)
    if coverage_end <= file_base_offset:
        return 0

    total_allocated = 0
    for alloc_offset, alloc_size in allocated_ranges:
        if alloc_offset >= coverage_end or alloc_offset + alloc_size <= file_base_offset:
            continue
        # Clip to coverage area
        start = max(alloc_offset, file_base_offset)
        end = min(alloc_offset + alloc_size, coverage_end)
        total_allocated += end - start

    # AMap page itself is allocated
    total_allocated += PAGE_SIZE

    return (coverage_end - file_base_offset) - total_allocated
