"""Heap-on-Node (HN) implementation.

HN provides a variable-size allocator within a data block.
It's the foundation for Property Context and Table Context.

Supports multi-page HN where allocations span multiple data blocks
connected via an XBLOCK data tree.

See [MS-PST] 2.3.1.
"""

import struct
from ..ndb.block import MAX_BLOCK_DATA
from ..errors import PSTWriteError

# HN heap signature (always 0xEC for any HN block)
HN_SIG = 0xEC

# HN client signatures (bClientSig)
HN_CLIENT_TC = 0x7C  # Table Context
HN_CLIENT_BTH = 0xB5  # BTree-on-Heap
HN_CLIENT_PC = 0xBC  # Property Context

# HNHDR: 12 bytes (page 0 only)
HNHDR_SIZE = 12

# HNPAGEHDR: 2 bytes (pages after page 0)
HNPAGEHDR_SIZE = 2

# HNBITMAPHDR: ibHnpm(2) + rgbFillLevel(64) = 66 bytes. Per [MS-PST] 2.3.1.4 a
# bitmap header's fill-level map covers the next 128 data blocks, where the
# HNHDR's own 4-byte map covers only the first 8 - so bitmap headers appear at
# block 8, then every 128 blocks after that. Confirmed against a real
# Microsoft-written multi-block HN (117 blocks): blocks 1, 7 and 9 start their
# first allocation at offset 2, and block 8 starts at 66.
HNBITMAPHDR_SIZE = 66
_FIRST_BITMAP_PAGE = 8
_BITMAP_PAGE_INTERVAL = 128


def _is_bitmap_page(page_index):
    return (page_index >= _FIRST_BITMAP_PAGE
            and (page_index - _FIRST_BITMAP_PAGE) % _BITMAP_PAGE_INTERVAL == 0)

# Maximum individual allocation on an HN page per [MS-PST] 2.3.1
MAX_HN_ALLOC = 3580


def make_hid(block_index, index):
    """Create a Heap ID (HID).

    HID layout (32-bit):
    - bits 0-4: hidType (always 0 for HN allocations)
    - bits 5-15: hidIndex (1-based allocation index within the page)
    - bits 16-31: hidBlockIndex (0-based data block index)
    """
    return ((block_index & 0xFFFF) << 16) | ((index & 0x7FF) << 5) | 0


def hid_index(hid):
    return (hid >> 5) & 0x7FF


def hid_block_index(hid):
    return (hid >> 16) & 0xFFFF


class HeapOnNode:
    """Builds a Heap-on-Node data structure, potentially spanning multiple pages.

    Usage:
        hn = HeapOnNode(client_sig=HN_CLIENT_BTH)
        hid1 = hn.allocate(some_data)
        hid2 = hn.allocate(more_data)
        pages = hn.build()  # list of bytes, one per page
    """

    def __init__(self, client_sig=HN_CLIENT_BTH, user_root_hid=0):
        self.client_sig = client_sig
        self.user_root_hid = user_root_hid
        # Per-page allocation tracking: list of lists of bytes objects
        self._pages = [[]]
        self._current_page = 0

    def _page_header_size(self, page_index):
        """Header size for a page: HNHDR, HNBITMAPHDR or HNPAGEHDR."""
        if page_index == 0:
            return HNHDR_SIZE
        if _is_bitmap_page(page_index):
            return HNBITMAPHDR_SIZE
        return HNPAGEHDR_SIZE

    def _can_fit(self, page_index, allocs, new_data_len):
        """Check if adding new_data to a page with existing allocs fits."""
        header_sz = self._page_header_size(page_index)
        current_data = sum(len(a) for a in allocs)
        new_count = len(allocs) + 1
        # HNPAGEMAP: cAlloc(2) + cFree(2) + (cAlloc+1)*2 offsets
        pagemap_sz = 4 + (new_count + 1) * 2
        total = header_sz + current_data + new_data_len + pagemap_sz
        return total <= MAX_BLOCK_DATA

    def allocate(self, data: bytes) -> int:
        """Allocate data on the heap and return its HID.

        Allocations are stored at their exact length. Only ibHnpm - the offset
        of the HNPAGEMAP - has to land on a 2-byte boundary, and that is
        handled once per page in _build_page0/_build_page_n rather than by
        rounding up every allocation.

        An earlier version padded each allocation to an even length, which
        fixed the "bad ibHnpm" errors but corrupted anything whose stored
        length is meaningful: a TC's Row Matrix must be exactly
        rows x rowsize, and a single 55-byte row was being stored as 56. A
        real Outlook-written PST stores that same allocation at 55 bytes with
        an even ibHnpm, confirming per-allocation padding is not what the
        format wants. See PATCHES.md.
        """
        page = self._current_page
        allocs = self._pages[page]

        if not self._can_fit(page, allocs, len(data)) and len(allocs) > 0:
            # Start a new page
            self._pages.append([])
            self._current_page += 1
            page = self._current_page
            allocs = self._pages[page]

        idx = len(allocs) + 1  # 1-based within this page
        hid = make_hid(page, idx)
        allocs.append(data)
        return hid

    def allocate_placeholder(self, size: int) -> int:
        """Reserve an allocation of `size` bytes now, to be filled in later.

        Lets a caller claim heap space for structures whose size is known up
        front but whose contents depend on allocations made afterwards (a
        TC's TCINFO, Row Index BTH and Row Matrix all reference HIDs of the
        per-row strings). Reserving them first keeps them on page 0, which is
        where real PSTs put them - see build_tc_node() and PATCHES.md.
        """
        return self.allocate(b'\x00' * size)

    def patch(self, hid: int, data: bytes) -> None:
        """Replace the contents of a previously reserved allocation."""
        page = (hid >> 16) & 0xFFFF
        index = (hid >> 5) & 0x7FF
        slot = self._pages[page][index - 1]
        if len(data) < len(slot):
            data = data + b'\x00' * (len(slot) - len(data))
        if not (len(data) == len(slot)):
            raise PSTWriteError(f"patch size mismatch: reserved {len(slot)}, got {len(data)}")
        self._pages[page][index - 1] = data

    def set_user_root(self, hid):
        """Set the user root HID (typically the BTH header)."""
        self.user_root_hid = hid

    def build(self) -> list:
        """Build the raw data blocks for all HN pages.

        Every page except the last is padded out to exactly MAX_BLOCK_DATA.
        In a multi-block HN a reader locates page N purely by block index, and
        a real Microsoft-written multi-block HN has *every* block except the
        last at exactly 8176 bytes (verified against a 117-block Contents
        Table in a PST that Outlook accepts). Emitting short intermediate
        pages made everything past page 0 unreachable, and Outlook silently
        discarded the affected messages rather than reporting an error -
        see PATCHES.md.

        Returns:
            List of bytes objects, one per page/block (without block trailers).
        """
        result = []
        last = len(self._pages) - 1
        for page_idx, allocations in enumerate(self._pages):
            if page_idx == 0:
                page = self._build_page0(allocations)
            else:
                page = self._build_page_n(allocations, page_idx)
            if page_idx != last:
                if not (len(page) <= MAX_BLOCK_DATA):
                    raise PSTWriteError(f"HN page {page_idx} is {len(page)} bytes, over the "
                    f"{MAX_BLOCK_DATA}-byte block limit")
                page = page + b'\x00' * (MAX_BLOCK_DATA - len(page))
            result.append(page)
        return result

    def _build_page0(self, allocations):
        """Build page 0 with full HNHDR (12 bytes)."""
        num_alloc = len(allocations)
        data_start = HNHDR_SIZE

        # Compute allocation offsets (relative to data_start)
        offsets = []
        current = 0
        for alloc in allocations:
            offsets.append(current)
            current += len(alloc)
        offsets.append(current)  # Sentinel

        # Pad the data area, not the allocations, so ibHnpm stays even.
        alloc_end = data_start + current
        pad = alloc_end % 2
        data_area_size = current + pad
        ib_hnpm = data_start + data_area_size

        # HNHDR (12 bytes)
        hnhdr = struct.pack('<H BB I I',
                            ib_hnpm,
                            HN_SIG,
                            self.client_sig,
                            self.user_root_hid,
                            0)  # dwFill

        data_area = b''.join(allocations) + b'\x00' * pad

        # HNPAGEMAP (cFree counts the alignment byte, if any)
        # cFree is the count of *freed allocations*, not spare bytes - nothing
        # here is ever freed, so it is always 0. Setting it to the alignment
        # pad byte instead produced "HN free alloc count doesn't match
        # (computed=0, expected=1)" and crashed scanpst outright. A real
        # Outlook-written page confirms the shape: sentinel 293, ibHnpm 294,
        # a 1-byte gap, and cFree 0. See PATCHES.md.
        pagemap = struct.pack('<HH', num_alloc, 0)
        for off in offsets:
            pagemap += struct.pack('<H', off + data_start)

        return hnhdr + data_area + pagemap

    def _build_page_n(self, allocations, page_index):
        """Build page N>0 with an HNPAGEHDR or, every 128 blocks from block 8,
        an HNBITMAPHDR."""
        num_alloc = len(allocations)
        data_start = self._page_header_size(page_index)

        offsets = []
        current = 0
        for alloc in allocations:
            offsets.append(current)
            current += len(alloc)
        offsets.append(current)

        # Pad the data area, not the allocations, so ibHnpm stays even.
        alloc_end = data_start + current
        pad = alloc_end % 2
        data_area_size = current + pad
        ib_hnpm = data_start + data_area_size

        # HNPAGEHDR is just ibHnpm; HNBITMAPHDR adds a 64-byte fill-level map
        # (left zeroed - 0 means "at least 3584 bytes free / block does not
        # exist" per [MS-PST] 2.3.1.2, which nothing reads back here).
        header = struct.pack('<H', ib_hnpm)
        if data_start == HNBITMAPHDR_SIZE:
            header += b'\x00' * (HNBITMAPHDR_SIZE - HNPAGEHDR_SIZE)

        data_area = b''.join(allocations) + b'\x00' * pad

        # cFree is the count of *freed allocations*, not spare bytes - nothing
        # here is ever freed, so it is always 0. Setting it to the alignment
        # pad byte instead produced "HN free alloc count doesn't match
        # (computed=0, expected=1)" and crashed scanpst outright. A real
        # Outlook-written page confirms the shape: sentinel 293, ibHnpm 294,
        # a 1-byte gap, and cFree 0. See PATCHES.md.
        pagemap = struct.pack('<HH', num_alloc, 0)
        for off in offsets:
            pagemap += struct.pack('<H', off + data_start)

        return header + data_area + pagemap
