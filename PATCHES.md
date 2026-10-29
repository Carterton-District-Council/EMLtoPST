# Changes from upstream

Forked from https://github.com/igrbtn/EMLtoPST at commit `6fe9025` ("Fix sender
and recipient handling for non-SMTP addresses"), MIT licensed. See `LICENSE`.

Carterton District Council used this library as the PST writer for a bulk
email-archive migration into Microsoft 365, because no free PST-writing library
existed. Its output was tested against Outlook, Outlook's Inbox Repair Tool
(scanpst) and the Microsoft 365 importer rather than trusted, and every defect
found was fixed and recorded here.

This is a working log, not a polished changelog. Entries were written as each
defect was found, and some later entries correct earlier ones; those
corrections are kept because they show what the format actually requires.
"Real archive" and "the migration" refer to the council's own archived mail.
"The reference" or "the repaired file" is a PST that Microsoft's own tools
wrote, used for byte-level comparison.

## Patches 1-42: making the output acceptable to Outlook and Microsoft 365 (2026-08 to 2026-10)

### 1. Missing `PR_DISPLAY_TO`/`PR_DISPLAY_CC`/`PR_DISPLAY_BCC`
Upstream never wrote the message-level display-recipient summary properties
(MAPI tags 0x0E04/0x0E03/0x0E02), even though the underlying Recipients Table
was correctly populated. Confirmed via `readpst` (libpst): `To:`/`Cc:` never
reconstructed from generated PSTs. Fixed in `eml2pst/mapi/properties.py` (added
the three tag constants) and `eml2pst/messaging/message.py`
(`build_message_pc` now derives semicolon-joined display-name strings per
recipient type from `parsed_eml['recipients']` and writes them).

### 2. Crash on large attachments (>~8MB): missing two-level XXBLOCK
`build_xblock()` only ever built a single-level XBLOCK page, which can hold at
most `(8176-8)//8 = 1021` block-ID references (~7.96MB of referenced data)
per [MS-PST] 2.2.2.8.3.2. Any subnode value larger than that (e.g. a real
attachment above ~8MB) produced an XBLOCK page that itself exceeded the
8176-byte single-block limit, hitting an `AssertionError` in `pack_block`.
Reproduced with a real 49MB archived message (2 attachments) from this
project's real archive data — not a synthetic edge case.

Fixed by implementing the two-level tree the spec requires:
`eml2pst/ndb/xblock.py`'s `build_xblock()` now takes a `level` parameter and
raises if asked to build a page with too many entries (forcing callers to
handle chunking) rather than silently producing an invalid oversized block.
`eml2pst/pst_file.py` gained `_store_xblock_tree()`, used by both
`_store_subnode_data` and `_store_node_pages`: when leaf BID count exceeds
`MAX_ENTRIES_PER_BLOCK` (1021), it groups them into multiple level-1 XBLOCKs
and references those from a single level-2 XXBLOCK. (A third tree level,
needed only past ~8GB for a single value, is asserted against rather than
silently mishandled.)

Verified: the real 49MB message now converts without error, `readpst`
extracts it with 0 items skipped, and the round-tripped size (~48.7MB)
closely matches the original (attachments are stored as raw binary in PST vs.
base64 in the source EML, so some size reduction is expected and correct).

### 3. Missing `PR_INTERNET_MESSAGE_ID`
The original RFC822 `Message-ID` header was never even parsed out of the
source EML, let alone written to the PST. It is the
key for tracing a migrated message back to its record in the source system,
and for any later deduplication. Fixed in `eml2pst/eml_parser.py`
(extracts `Message-ID`/`Message-Id` into `parsed_eml['internet_message_id']`)
and `eml2pst/messaging/message.py` (writes it as tag 0x1035, PT_UNICODE).
Verified present in the output PST by searching the raw file bytes for the
known Message-ID's UTF-16LE encoding (readpst's own header reconstruction
doesn't happen to surface this property, even though the data is genuinely
stored correctly — checked at the byte level to be sure, not just by pointing
a reader tool at the file and hoping the reader displays everything present).

### 4. Whole-mailbox in-memory build caused a full-desktop hang
`PSTFileBuilder` held every data block (message bodies, decoded attachment
binary, ...) for the entire mailbox in a Python list (`self._data_blocks`) and
only wrote to disk once, at the very end of `write()`. Separately,
`PSTFileBuilder.add_message()` retained the *full* `parsed_eml` dict per
message (including decoded attachment bytes) in `self._messages`, for
building folder Contents-TC rows later, when only a handful of small summary
fields (subject, sender, size, importance, ...) were actually needed.

Testing `pack` against a real mailbox on 2026-08-07 exhausted memory on the
6.8GB/4GB-swap desktop it was run on and froze the whole machine hard enough
to need a power-cycle (no OOM-kill or clean shutdown in the journal - just
swap thrashing until the desktop stopped responding).

Fixed in `eml2pst/pst_file.py`: `PSTFileBuilder` now takes `output_path` in
its constructor, opens it immediately, and `_write_block()` assigns each
block's file offset and writes it to disk as soon as it's produced - offsets
only ever depend on blocks already written, so this doesn't require waiting
until the whole mailbox is known. Only small per-block metadata
`(bid, offset, size)` is kept in memory afterwards, used later by `write()`
(now argument-less) to build the header/B-tree/AMap pages, which do need
every block's final position first. `add_message()` now stores only the
small summary dict Contents-TC rows need, not the full parsed EML.

Verified: rebuilding the vendored `test_emails/` fixture round-trips through
`readpst` identically to before (3 messages, 2 folders, 0 items skipped).

### 5. Wrong wSig on every B-tree page, and on the ComputeSig formula itself
Real Microsoft Outlook refused every PST this builder produced ("Outlook Data File Corruption"),
even though `readpst`/libpst read them fine — because `readpst` doesn't validate the page/block
signature (`wSig`) and Outlook does. Outlook's own error names the exact expected vs. actual
value for a specific `(offset, bid)`, which is what exposed this: two bugs, not one.

1. `ndb/btree.py`'s `build_btpage()` hardcoded `wSig=0` for every page it built. Per [MS-PST]
   2.2.2.7.7.4, `wSig=0` is only correct for AMap/PMap/FMap/FPMap pages (built separately in
   `ndb/amap.py`, unaffected) - NBT and BBT pages, the only ptypes `build_btpage()` is ever used
   for, need a real computed signature.
2. `ndb/block.py`'s `block_signature()` (the shared ComputeSig implementation, also used for
   every leaf data block) implemented the wrong bit-shuffling: `ib ^= bid; ib >>= 16; bid >>= 16;
   ib ^= bid` instead of the correct `value = (ib ^ bid) & 0xFFFFFFFF; wSig = (value >> 16) ^
   (value & 0xFFFF)`.

Fixed both; `build_btree_pages()`/`build_btpage()` now thread the page's own file offset through
so it can compute its signature.

Verified two ways: (1) the corrected formula reproduces the *exact* `wSig` Outlook's own error
dialog named for a known `(offset, bid)` pair from the previously-broken PST; (2) walked every
page and data-block trailer in a rebuilt real 4,158-message PST and recomputed `wSig` for all of
them independently (4,935 pages, 88,143 blocks - 0 mismatches).

### 6. AMap page trailer.bid must equal the page's own file offset
After patch 5's `wSig` fix, a second real-Outlook attempt hit a different "Outlook Data File
Corruption" error, again naming the exact expected vs. actual mismatch:
`Read(@2068A400): Expected (bid=2068A400, ptype=84, dwCRC=..., wSig=0000), but read
(bid=374D, ptype=84, dwCRC=..., wSig=0000)` — `ptype=0x84` is `PTTYPE_AMAP`. Note the expected
`bid` is literally the same value as the page's own file offset (`@2068A400`); what was actually
written was an arbitrary small sequentially-allocated page BID (`0x374D`).

Per [MS-PST] 2.2.2.7.7.4, AMap/PMap/FMap/FPMap page trailers must use the page's own absolute
file offset as their `bid` field - not a BID from the normal allocation space (these pages aren't
tracked via the BBT at all). `pst_file.py`'s AMap-building loop (`write()`, Phase 5) was calling
`self._alloc_page_bid()` and passing that instead. Fixed by passing the page's own `amap_offset`
directly as the trailer `bid`.

Verified: rebuilt the same 4,158-message mailbox and independently checked every AMap page's
trailer against its own file offset (2,142 AMap pages, 0 mismatches) alongside re-checking all
4,935 NBT/BBT pages and 88,143 data blocks from patch 5 (all still correct).

### 7. Internal blocks (XBLOCK/SLBLOCK) also need a computed wSig, not 0
After patch 6's AMap-bid fix, real Outlook gave up with a generic "Something is wrong with the
[Outlook Data File]" and auto-launched the Inbox Repair Tool (`scanpst.exe`), which produced a
1.4MB recovery log. The vast majority of it (tens of thousands of lines) was `!!Invalid block
(bid=..., ib=..., cb=..., cRef=...): Sig mismatch (read 0000, computed ...)` for blocks with the
internal-BID bit set (XBLOCK/SLBLOCK), plus large numbers of downstream `!!Block is not allocated
in AMAP!` / `!!XBlock not allocated in AMAP!` complaints that turned out to be *consequences* of
those signature failures (scanpst's own BBT-rebuild/scavenge process treating sig-invalid blocks
as suspect), not a separate real AMap bug - see the investigation note below.

The code (both here and in the original vendor commit) assumed "[MS-PST] 2.2.2.8.1: internal
blocks MUST have wSig=0" - that rule is wrong. `ComputeSig(ib, bid)` reproduced scanpst's named
expected value exactly for multiple real internal blocks (e.g. expected `0x50C6` for
`bid=6, ib=20672`). Fixed in `ndb/block.py`'s `pack_block()`: every block, leaf or internal, now
gets a real computed signature - there is no `wSig=0` exception for blocks (there still is one
for AMap/PMap/FMap/FPMap *pages*, which is correct and unrelated).

**Investigation note on the AMap complaints**: before finding the real bug, spent significant
effort trying to find a second, AMap-specific bug (windowing anchor, MSB/LSB bit order) to
explain the "not allocated in AMAP" volume - confirmed via Microsoft's own published AMap page
spec that the windowing/self-mapping logic here was already correct, and an independent
from-scratch AMap bitmap reconstruction (walking the BBT/pages ourselves, not reusing this code's
own formula) byte-matched the actual file exactly once the wSig fix was applied. The AMap
complaints were entirely a downstream symptom of the block-signature bug, not a separate defect.

Verified: rebuilt the same 4,158-message mailbox; independently recomputed wSig for all 4,935
NBT/BBT pages and all 88,143 blocks (leaf and internal, 0 mismatches now), AMap page bid for all
2,142 AMap pages (0 mismatches), and independently rebuilt every AMap page's bitmap content from
a from-scratch BBT/page walk and byte-compared it against the actual file (0 mismatches).

### 8. AMap-building performance: O(blocks × AMap pages) instead of O(blocks + AMap pages)
Separate from correctness: building the AMap pages passed the *entire* `allocated_ranges` list
(one entry per block/page in the whole file) to `build_amap_page()`/`compute_amap_free()` once
per AMap page, and both functions scan the full list internally. For the real 4,158-message
mailbox above (88,143 blocks, 2,142 AMap pages) that's on the order of 10⁸ iterations, and it
showed: packing that one mailbox took ~15 minutes. This would not remotely scale to a full
production run of hundreds of mailboxes.

`allocated_ranges` is already sorted by offset (data blocks are appended in the strictly
increasing order `_write_block` wrote them, followed by NBT then BBT pages at strictly higher
offsets), so each AMap page's relevant slice can be found with a binary search
(`bisect.bisect_left`) instead of a full scan. Fixed in `pst_file.py`'s AMap-building loop (Phase
5 of `write()`). Re-verified byte-for-byte identical AMap page content before/after (via the
same independent from-scratch bitmap reconstruction as patch 7) - this is a pure performance fix,
not a behavior change. Packing the same mailbox dropped from ~15 minutes to ~3m14s (~4.7x).

### 9. Heap-on-Node allocations must be 2-byte aligned ("bad ibHnpm")
After patches 7/8, real Outlook again said the file was corrupt and auto-ran the Inbox Repair
Tool, which crashed again ("Fatal Error: 80040818") - but this time much further in (its own log
was ~1/3 the size: 10,673 lines vs. 35,806 before), confirming the earlier fixes were real
progress, not wasted effort. The crash itself was in "Beginning PST/OST recovery" while
re-inserting recovered messages, hitting `!!HN (nid=692) bad ibHnpm (ibHnpm=231, cbPage=251)` on
the Recipients Table heap for several messages, plus long lists of `!!TC (nid=692) missing
required column (...)` - including tags for columns this code *already writes* (e.g.
`3001001F` = `PR_DISPLAY_NAME_W`), which was the tell that these weren't really missing: an
odd `ibHnpm` (231, not a multiple of 2) meant scanpst's parser was reading the Heap-on-Node page
at the wrong byte offsets entirely, so it couldn't reliably find *any* column, present or not.

Confirmed via a real worked example in Microsoft's own HN-parsing blog post (parsing a sample HN
block from the spec byte-by-byte): every one of its allocation offsets (`0x0C, 0x14, 0x6C, 0x7C,
0x8C, 0xA4, 0xBC, 0xD4`) and its `ibHnpm` (`0xEC`) land on an even byte. `ltp/heap.py`'s
`HeapOnNode.allocate()` never padded odd-length allocations (e.g. odd-length `PT_BINARY`/
`PT_STRING8` values), so the cumulative offset - and eventually `ibHnpm` itself - could drift
odd. Fixed by padding every allocation to an even length with a single trailing `0x00` when
needed; the pad byte is invisible to anything reading a property's *logical* value, since real
readers determine that from an explicit declared size (e.g. `PR_ATTACH_SIZE`) or a null
terminator (strings), never from the raw heap-allocation boundary.

### 10. Recipients/Attachments Tables were missing several columns real Outlook requires
Independent of patch 9's parsing-corruption issue, some of the "missing required column" tags in
that same log genuinely weren't being written at all: this vendored library's Recipients Table
only had 5 columns (display name, email, addrtype, recipient type, row ID) and Attachments Table
was missing the short `PR_ATTACH_FILENAME` (it only wrote the long-filename variant). Real
Outlook's repair tool expects considerably more on Recipients specifically - `PR_RESPONSIBILITY`,
`PR_OBJECT_TYPE`, `PR_ENTRYID`, `PR_RECORD_KEY`, `PR_SEARCH_KEY`, `PR_DISPLAY_TYPE`,
`PR_SEND_RICH_INFO`, and an address-book display name - none of which [MS-PST] itself spells out
as strictly mandatory, but which real-world PSTs always carry. Added all of them in
`messaging/message.py`, including a proper one-off `PR_ENTRYID` (per [MS-OXCDATA] 2.2.5.1,
`utils.make_one_off_entry_id()`) for recipients that aren't resolved against a directory.

Separately, the recovery log's `!!Message (nid=X): Missing PR_SEARCH_KEY` was unambiguous (a
plain property-presence check on the Message PC itself, unrelated to any TC/HN parsing) - added
`PR_SEARCH_KEY` to `build_message_pc()`, deterministically derived (MD5) from the message's own
Internet Message-ID (or subject+sender+delivery-time as a fallback) so re-running this pipeline
on the same source message produces the same value rather than a random one each time.

**Still unresolved as of this patch**: a separate, real, and so far unexplained discrepancy where
scanpst's *original* (pre-rebuild) BBT validation reports specific legitimate blocks as "not
allocated in AMAP" - a fixed ~1,562-entry set that hasn't moved across the last two rebuilds
(confirming it's unrelated to any of the wSig/HN/column fixes above), clustered in roughly the
last 20% of nearly every AMap page's coverage window. Extensive investigation (alternate AMap
coverage-window anchors, MSB/LSB bit order, positional clustering analysis, cross-checking
against Microsoft's own AMap spec and an independent from-scratch bitmap reconstruction that
byte-matches the real file exactly) has not found the cause. This does not appear to be what
crashes scanpst (the crash is patch 9's HN issue, now fixed) but may still cause Outlook to treat
the file as needing repair even after a clean open would otherwise succeed. Needs another
real-Outlook round to determine whether it's still a live blocker.

### 11. TCINFO's `rgib` array had every entry shifted one slot - affected every table
A fifth real-Outlook round (patch 9/10's fixes applied) got further still - past the crash point
entirely this time, into "Attempting to walk all folders" during the Inbox Repair Tool's
recovery pass - but still failed, this time with `!!TCINFO (nid=692) invalid TCI_4b (0)`
alongside the same "missing required column" list from before, including columns patch 10 had
already added (confirming again this was a parsing-corruption symptom, not truly-missing columns).

Checked the actual [MS-PST] 2.3.4.1 TCINFO field table (not summaries/blog posts this time,
the authoritative structure reference): `rgib[0]` (`TCI_4b`) is documented as the **ending**
offset of the 8-/4-byte column group - not the *starting* offset this code assumed (hardcoded to
`0`, per an incorrect inline comment). Every `rgib` slot here was off by one: `rgib[0]` held what
should have been in `rgib[1]`, `rgib[1]` held what should have been in `rgib[2]`, and `rgib[2]`
(the actual end of the 4-byte group) was never stored anywhere. Fixed in `ltp/tc.py`'s
`build_tc_node()` - this function is shared by *every* TC in the file (Recipients, Attachments,
and all three per-folder tables), so this was a broad, high-impact bug, not one scoped to
Recipients/Attachments specifically.

Verified: rebuilt the same 4,158-message mailbox; independently re-verified all pages, blocks,
AMap bids, AMap bitmap content, and message count clean (unchanged counts from patch 10's build,
as expected - this fix only touches row-layout metadata, not block content or count).

**Also newly observed, not yet fixed**: the same recovery log flagged
`!!Header NID high-water mark has incorrect value` for every NID type - the header's `rgnid[32]`
per-type high-water-mark array (`ndb/header.py` `build_header()`) is never populated at all,
left as the all-zero initial buffer. This validation happens late in NDB recovery, after the
BBT/NBT phases that actually trigger repair mode, and is the kind of bookkeeping a repair tool
typically just recalculates - likely lower severity (probably only matters when *adding* new
items to the PST, not opening/reading an existing one) but not yet confirmed either way. Left
unfixed pending confirmation it's still relevant after the fixes above.

### 12. TC column grouping put variable-length columns in the wrong bucket
A sixth real-Outlook round (patch 11's `rgib` fix applied) got past `TCI_4b` entirely, into a new
error: `!!TCINFO (nid=692) invalid TCI_Bm (58, TCI_1b=28)` - and, as with every previous "the
columns look wrong" symptom, the same "missing required column" list including columns already
written. This time confirmed directly with our own code (built a Recipients TC and parsed the raw
TCINFO bytes back out) rather than hand-deriving: `rgib[TCI_1b]` came out as `28`, not the `56` a
manual calculation predicted.

Root cause: `build_tc_node()` grouped columns for row-layout purposes by **fixed-vs-variable**,
keeping all `PT_UNICODE`/`PT_STRING8`/`PT_BINARY` columns in a separate trailing `group_var`
list appended *after* the 4-/2-/1-byte groups. But a variable-length property still stores a
plain 4-byte HID *in the row itself* - it belongs in the same "storage size >= 4 bytes" group as
directly-stored fixed values, sorted together by tag, per [MS-PST] 2.3.4.2. Recipients (7 of its
13 columns are variable-type) hit this hard: `offset_after_4b`/`offset_after_2b`/
`offset_after_1b` all froze at the boundary *before* any variable column instead of after them
(28, from only the 6 truly-fixed columns), while the actual row data was laid out correctly at
the true width (58, including the variable columns' HIDs) - an internal inconsistency exactly
matching scanpst's complaint. Fixed by classifying every column purely by its row storage size
(`_column_storage_size()`), with no separate variable-type bucket.

Verified two ways: (1) directly parsed the raw TCINFO bytes our own code now produces for a
Recipients table and confirmed `rgib[TCI_bm] - rgib[TCI_1b] == ceil(cCols/8)` (the CEB size) - the
exact relationship scanpst's error implies it checks; (2) rebuilt the same 4,158-message mailbox
and independently re-verified all pages, blocks, AMap bids, AMap bitmap content, and message
count clean (unchanged from patch 11's build, as expected).

### 13. TCOLDESC array must be sorted by tag globally - PidTagLtpRowId isn't special-cased first
A seventh real-Outlook round (patch 12's grouping fix applied) got past `TCI_Bm` into:
`!!TCINFO (nid=692) tags not all increasing (tag=0C150003, prev=67F20003)` - `0x67F2` is
`PidTagLtpRowId`'s tag, and it's numerically *larger* than `0x0C15`, so it sorting "before" 0x0C15
in the array is exactly backwards.

Root cause: this code conflated two genuinely separate orderings. Per [MS-PST] 2.3.4.1, "the
entries in [rgTCOLDESC] MUST be sorted by the tag field" - across *every* column, including
`PidTagLtpRowId`, whose tag (`0x67F2`) is large and normally sorts *last*. But `PidTagLtpRowId`'s
*value* (`dwRowID`) is separately required to always live at row byte offset 0 (2.3.4.4) -
regardless of where its TCOLDESC entry falls in the tag-sorted array. This code force-placed
`PidTagLtpRowId` first in the array (`iBit=0`) because that's also where its value lives in the
row, incorrectly treating "byte offset in the row" and "position in the TCOLDESC array" as the
same ordering. Fixed in `ltp/tc.py`'s `build_tc_node()` by computing them separately: byte offsets
(`ib_data`) still come from the size-based grouping (patch 12, unaffected), while the TCOLDESC
array order and `iBit` now come from sorting *all* columns (including `PidTagLtpRowId`) by tag
value. The CEB-marking code (which previously hardcoded `PidTagLtpRowId` to `iBit=0`) now uses its
real, computed `iBit`.

Verified directly against our own code (as with patch 12): built a Recipients TC, parsed the raw
TCOLDESC bytes back out, and confirmed tags are now strictly increasing across all 14 columns,
with `PidTagLtpRowId` correctly sorting last (`iBit=13`) while its `ib_data` stays `0`.

### 14. Row Matrix rows must never span a block boundary
While researching patch 13 (the [MS-PST] Row Matrix section, 2.3.4.4), found a related but
distinct requirement this code didn't meet: "each data block MUST store an integral number of
rows" when a table's Row Matrix is too large for one block and needs a data tree - "rows cannot
span across two blocks." `pst_file.py`'s generic subnode chunker
(`_store_subnode_data()`) only chunked by raw byte count (`MAX_BLOCK_DATA`), with no notion of row
boundaries, so any table whose row size doesn't evenly divide `MAX_BLOCK_DATA` (8176) would get a
row split across two blocks - corrupt per spec. This wasn't caught by a real-Outlook test (it
hadn't been reached yet - the crash was still happening earlier, in per-message Recipients/
Attachments tables which are small enough to never need chunking), but the mailbox's own single
folder has a 4,158-row Contents Table (~174KB of row data, definitely multi-block), so this was a
live latent bug about to surface.

Fixed by giving `_store_subnode_data()` an optional `chunk_size` override, and having
`build_tc_node()` compute a row-aligned chunk size (`(MAX_BLOCK_DATA // row_size) * row_size`,
i.e. as many whole rows as fit in one block) for its row-data subnode specifically - other
subnode data (e.g. a PC's own oversized property values) is unaffected and keeps the original
byte-count chunking, since it isn't row-shaped.

**Not yet verified against real Outlook** - proactively fixed from reading the spec while working
on patch 13, not from an observed error. Confirm in the next real-Outlook round.

### 15. Patch 13 was subtly wrong: LtpRowId/LtpRowVer are a fixed reserved pair, not tag-sorted
An eighth real-Outlook round (patch 13/14 applied) got past "tags not all increasing" but hit two
new errors: `!!TCINFO (nid=692) COLDESC (tag=0E0F000B) has invalid cbData (4, expect=1)` and
`!!TCINFO (nid=671) COLDESC for PR_LTP_ROW_ID invalid (ibData=0, cbData=4, wBit=7)`.

The second one meant patch 13's theory - "sort every column including PidTagLtpRowId by tag
value" - was wrong, just wrong in a way that happened to also silence the previous "not
increasing" symptom (sorting `PidTagLtpRowId`, tag `0x67F2`, to the *end* trivially satisfies a
"must be increasing" check, since nothing follows it). Went back to primary sources instead of
re-guessing: [MS-PST]'s own "Sample Folder Object" section (2.3.4.4.1 companion) contains full
byte-level dumps of *real* TC tables from an actual Outlook-created PST. Every one of them - two
independent tables - shows `PidTagLtpRowId` (`IB:0, iBit:0`) immediately followed by
`PidTagLtpRowVer` (`IB:4, iBit:1`), *unconditionally first*, regardless of their own tag values
being numerically large. This code had never implemented `PidTagLtpRowVer` at all - patch 13's
"tags not all increasing" error was really just scanpst noticing the second array slot wasn't
`PidTagLtpRowVer`, not a demand for full tag-sorted ordering.

The same real examples also settled the `cbData` question directly: `PidTagSubfolders` and
`PidTagMessageToMe` (both `PT_BOOLEAN`) show `CB: 1` in the dump - confirming boolean values are
1 byte in TC row storage specifically, not the 4 bytes `PROP_TYPE_SIZES` (shared with PC storage)
says. `ltp/tc.py` was using the shared 4-byte size here.

Fixed both in `ltp/tc.py`: `PidTagLtpRowId`/`PidTagLtpRowVer` are now always written first
(`iBit` 0/1, `ib_data` 0/4) with user columns starting at `iBit`/`ib_data`+2/+8, sorted only
among themselves by size-group (patch 12's grouping, otherwise unchanged); `_column_storage_size()`
now special-cases `PT_BOOLEAN` to 1 byte for TC rows, with the row-packing code updated from
`<I` to `<B` to match.

Verified directly against our own code (as with patches 12/13): built a Recipients TC and parsed
the raw TCOLDESC bytes back out - confirms `PidTagLtpRowId`/`PidTagLtpRowVer` at `iBit` 0/1 with
`ib_data` 0/4, `PT_BOOLEAN` columns now `cbData=1`, and `rgib` values remain internally consistent
(`52, 52, 54, 56` for a 15-column Recipients table: `ceil(15/8)=2` matches `56-54`). Then rebuilt
the same 4,158-message mailbox and independently re-verified all pages, blocks, AMap bids, AMap
bitmap content, and message count clean.

### 16. Array order, iBit, and row byte offset are three independent orderings, not two
A ninth real-Outlook round (patch 15 applied) brought back the *exact* error patch 15 was
supposed to have fixed: `!!TCINFO (nid=692) tags not all increasing (tag=0C150003,
prev=67F30003)`. This looked like a regression at first, but it revealed that patches 13 and 15
had each correctly diagnosed *half* of a three-way problem and then wrongly merged it into two:

- Patch 13 was right that the TCOLDESC **array** must be sorted by real tag value, including
  `PidTagLtpRowId`/`PidTagLtpRowVer` (0x67F2/0x67F3) - but wrong to also use that same order for
  `iBit`.
- Patch 15 was right that `iBit` for the reserved pair must always be 0/1 - but wrong to also use
  that fixed-first placement for the **array** order.

Per [MS-PST] 2.3.4.3, `iBit` is a separate 1-byte field stored *in* each TCOLDESC entry, not
something derived from the entry's position in the array. There is no requirement that array
order and `iBit` order match at all - they're independently meaningful: the array must be
tag-sorted (for lookup), while `iBit` just needs to consistently identify which CEB bit
corresponds to which column between the TCOLDESC array and each row's CEB.

Fixed in `ltp/tc.py` by fully separating three orderings that were previously conflated
pairwise: TCOLDESC array order (tag-sorted, all columns including the reserved pair - so
`PidTagLtpRowId`/`PidTagLtpRowVer` now sort near the *end*, wherever their real tag value
lands), `iBit` (`PidTagLtpRowId`=0, `PidTagLtpRowVer`=1 always, user columns 2+ in tag order),
and `ib_data` (size-grouped row byte layout from patch 12, with the reserved pair fixed at row
offset 0/4 - unchanged).

Verified directly against our own code (as with every TC-layout patch in this series): built a
Recipients TC and parsed the raw TCOLDESC bytes back out - the array is now strictly
tag-increasing end-to-end (including the reserved pair, which lands last), while `iBit` values
for the reserved pair remain 0/1 regardless of their array position (14, 15 out of 15 entries).
`rgib` values unchanged and still internally consistent. Then rebuilt the same 4,158-message
mailbox and independently re-verified all pages, blocks, AMap bids, AMap bitmap content, and
message count clean.

### 17. PMap pages were never written at all - likely the root of the long-running "AMAP" mystery
A tenth real-Outlook round (patch 16 applied) cleared the TCOLDESC errors entirely - no more
"tags not all increasing", no `cbData`/`TCI_*` complaints - and surfaced two new low-count ones
(`!!BTH node ... keys overlap`, 5 occurrences; `!!Attachment table ... sub-object not found`, 1).
Traced both against our own output directly and neither reproduced: `hidRowIndex` correctly
points at the real BTH header, and the `hid=120` scanpst named doesn't even decode to a valid HID
under this format's encoding (not a multiple of 32). That strongly suggested they were downstream
artifacts of scanpst's own scavenging/reconstruction mode rather than real defects - which meant
whatever *forces* scanpst into that mode was still unfixed.

That turned out to be the "PMap page @N: PTYPE mismatch / CRC mismatch / Sig mismatch / BID
mismatch" errors that had been present in **every** log since the first one, and which had been
repeatedly set aside as lower-priority noise. They were not noise: this library never wrote PMap
pages at all. What scanpst was reporting was ordinary message data sitting at the file offsets
where PMap pages are required to be.

Per [MS-PST] 2.2.2.7.3, the PMap is functionally deprecated by the Density List - but
"implementations MUST ensure the presence of PMaps at the correct intervals and maintain valid
checksums to ensure backward-compatibility with older clients," and scanpst validates them
directly regardless of the deprecation. A PMap is structurally an AMap where each bit tracks a
512-byte page instead of a 64-byte block: 496 bitmap bytes covering `496 * 8 * 512` = 2,031,616
bytes, i.e. one PMap page per eight AMap pages, with the first at absolute offset `0x4600`
(immediately after the first AMap page at `0x4400`), `ptype` `0x83`, and the same
`bid == own file offset` / `wSig == 0` trailer convention as AMap (patch 6).

Implemented in a new `ndb/pmap.py`, wired into `pst_file.py`: `_skip_amap_pages()` now also
reserves the PMap slot after every 8th AMap page (PMAP_COVERAGE is exactly 8 * AMAP_COVERAGE, so
these always line up), the initial write cursor starts after both the first AMap and its PMap,
PMap pages are generated in Phase 5 alongside AMap pages (reusing the same binary-search window
slicing from patch 8), and written in Phase 7.

**Second, subtler bug found by verification, not by Outlook**: AMap pages occupy real file space
that a PMap page's bitmap must mark as allocated - but AMap page positions had never been added
to `allocated_ranges`, because `build_amap_page()` sets an AMap page's *own* bit itself
(self-mapping). That works for AMap, but a PMap page covers eight AMap pages' worth of file and
has no such self-knowledge of the other seven, so every PMap bitmap was missing up to 7 bits.
Caught by extending the independent from-scratch bitmap reconstruction (see below) to PMap and
byte-comparing - it reported all 269 PMap pages mismatching at exactly one bit position, which
resolved to an AMap page's offset. Fixed by listing AMap positions explicitly.

Verified: rebuilt the same 4,158-message mailbox; independently re-verified 4,936 pages, 88,174
blocks, 2,152 AMap pages (bid + full bitmap content), **269 PMap pages** (ptype + bid + full
bitmap content, reconstructed from scratch rather than reusing the writer's own logic), and
message count - zero mismatches across all of it. The small vendored `test_emails` fixture also
still round-trips through `readpst` with 0 items skipped.

**Expected to resolve the long-open "not allocated in AMAP" mystery** (see patch 9's
investigation note): with no PMap pages present at all, scanpst's view of allocation was wrong
from the start, which plausibly explains ~1,562 such complaints that stayed at a fixed count
across every rebuild regardless of what else was fixed. Not yet confirmed - the next real-Outlook
round should show whether they're gone.

### 18. PMap contents must be "no free bits"; FMap pages were missing too
The eleventh real-Outlook round confirmed patch 17 worked: all the PMap `PTYPE`/`CRC`/`Sig`/`BID`
mismatches are gone, and scanpst's AMap-validation section shrank from 1,623 lines to 317. Two
follow-on problems remained, both in the same family.

**PMap contents.** 269 instances of `!!PMap page <@N> has free bits, but the header says it
doesn't` - one per PMap page. Patch 17 built PMap bitmaps by actually computing allocation (the
same way AMap does), which leaves most bits clear in a sparsely-allocated file. But [MS-PST]
2.2.2.5 (ROOT) says that because the PMap is deprecated, `cbPMapFree` "SHOULD be zero, and
creators of new PST files MUST initialize this value to zero" - which this code already did. An
all-but-empty PMap and a header declaring zero free space are contradictory, and scanpst checks
exactly that pair. Fixed by filling PMap pages with 0xFF (no free bits), which is both
self-consistent with the mandatory `cbPMapFree = 0` and matches what the header already does for
its own in-header copies of these deprecated maps (`rgbFM`/`rgbFP`, both 128 bytes of 0xFF).
This also removed patch 17's AMap-in-PMap-coverage complication entirely - with fixed content
there is no bitmap to compute.

**FMap pages.** The same log showed `!!FMap page @32524288: PTYPE mismatch (read C1, expected
82)` and friends - the identical failure mode as PMap, one structure over. [MS-PST] 2.2.2.7.5
gives FMap the same treatment as PMap: implementations SHOULD NOT *use* it, but "the presence of
FMap pages at the correct intervals MUST be preserved, and all corresponding checksums MUST be
maintained for a PST file to remain valid."

Working out *where* they go came from the header rather than from the prose: an FMap page is 496
bytes with one byte per AMap page, and the header's own `rgbFM` is 128 bytes - i.e. the header
covers the first 128 AMap pages itself, so the first on-disk FMap page picks up at AMap index
128, then every 496 after that. That prediction reproduced scanpst's five demanded FMap offsets
exactly (`0x1F04800`, `0x9724800`, `0x10F44800`, `0x18764800`, `0x1FF84800`) before rebuilding.

Both maps, plus FPMap, now live in `ndb/legacy_maps.py` (replacing patch 17's `ndb/pmap.py`),
which owns the placement rules and builds all three identically: 496 bytes of 0xFF, `wSig` 0,
`bid` = own file offset, differing only in ptype (FMap 0x82, PMap 0x83, FPMap 0x85 per [MS-PST]
2.2.2.7.1 - note `ndb/btree.py`'s pre-existing names for 0x82/0x83/0x85 do *not* match the
spec's meanings, so these are defined explicitly rather than imported and silently misread).

**FPMap is implemented but NOT verified.** By the same header-derived reasoning that FMap
confirmed: an FPMap bit covers one PMap page, `rgbFP` is 128 bytes = 1024 bits, so the first
on-disk FPMap page starts at PMap index 1024 = AMap index 8192, roughly 2.08 GB into the file,
then every 31744 AMap pages. The test mailbox is 546 MB, so no FPMap page is required in it and
scanpst has never had cause to check one - consistent with it reporting zero FPMap errors. This
matters for production: any mailbox that packs to over ~2 GB will be the first real test of that
placement. Verify one deliberately before trusting a large-mailbox run.

Verified: rebuilt the same 4,158-message mailbox; independently re-verified 4,936 pages, 88,174
blocks, 2,152 AMap pages (bid + full bitmap content), and all 274 legacy map pages (269 PMap +
5 FMap - ptype, ptypeRepeat, bid, wSig and full 0xFF content) - zero mismatches. Small vendored
fixture still round-trips through `readpst`.

### 19. SLBLOCK entries must be in ascending NID order; cbAMapFree counted past EOF
The twelfth real-Outlook round was the first where **scanpst ran to completion instead of
crashing**, and where the AMap-validation section collapsed to a single line - patches 17/18 are
confirmed good, and all PMap/FMap complaints are gone. Two clearly-diagnosed bugs remained.

**SLBLOCK ordering.** `!!Invalid block (bid=2E, ...): SBLOCK (leaf) keys are not ascending
<0, 692> <1, 671>` / `<1, 671> <2, 5>`. An SLBLOCK is a BTree leaf keyed by subnode NID -
readers *search* the sorted keys rather than scanning, so ascending order is structural, not
cosmetic. `_build_sl_bid()` appended entries in construction order: recipients (0x692), then
attachments (0x671), then each attachment's own subnode (0x5...) - strictly *descending*. Fixed
by sorting. This is also the most likely explanation for the message-level errors that had been
provisionally written off as scanpst scavenging artifacts (patch 17's "what's not verified"
note): `!!Attachment table for 4C4, sub-object not found` and `!!TC (nid=692) missing required
column (...)` are exactly what a failed key search over unsorted entries produces - the subnodes
are present and correct, but unfindable.

**cbAMapFree past EOF.** `!!Computed cbAMapFree of 33267136, but header has 33346496` - a
79,360-byte overcount. `compute_amap_free()` always credited a full `AMAP_COVERAGE` window, but
the last AMap page's nominal window runs past the end of the file, and space that doesn't exist
isn't free space. 79,360 is exactly that final window's overhang past EOF. Fixed by capping the
coverage area at `file_eof`.

Verified: rebuilt the same 4,158-message mailbox; independently re-verified 4,936 pages, 88,174
blocks, 2,152 AMap pages, and all 274 legacy map pages - zero mismatches; small fixture still
round-trips through `readpst`.

### 20. Row Index BTH keys must be > 0, so recipient row IDs are 1-based
The thirteenth real-Outlook round was the breakthrough: **scanpst completed and successfully
repaired the file**, and the repaired PST opened in Outlook. Patch 19 confirmed - the AMap
validation section reported zero errors, and the "SBLOCK keys are not ascending" complaints are
gone.

The dominant remaining error was `!!TC (nid=692) missing required column (...)` at 49,464
occurrences (~12 per message), but that is a cascade, not the fault. The actual fault is one line
above it: `!!BTH node (nid=692, hid=120), keys overlap (dwkey=0, dwkeyMin=0)`.

scanpst prints HIDs in hex without a prefix, so `hid=120` is `0x120` - rebuilding a one-recipient
Recipients TC locally and walking its allocations identified that as exactly the Row Index BTH's
leaf, whose single key is `0`. A TC's Row Index BTH is keyed by `dwRowID`, and scanpst validates
each key against a running minimum that starts at 0 using a strict greater-than test, so a first
row with `dwRowID = 0` fails. A broken row index means no column can be located, which produces
the 49,464 "missing required column" lines from a table whose columns are all present.

Recipients was the only table using 0-based row IDs - every other TC keys its rows on a NID
(folder NID, message NID, attachment subnode NID), which is never 0 - and correspondingly the
complaint only ever names `nid=692`, at ~1 per message (3,977 vs 4,079 messages recovered).
Fixed in `messaging/message.py`: recipient `dwRowID`/`PR_ROWID` are now `i + 1`. Confirmed by
dumping the rebuilt BTH leaf - its key is now `1`.

### 21. Attachment table/sub-object column parity; Associated Contents table had no columns
The fourteenth round confirmed patch 20 emphatically: scanpst's log fell from 88,545 lines to
30,837 and its error count from 67,331 to 13,745, with **all 49,464 "missing required column"
and all 3,977 "BTH keys overlap" errors gone**. The file still needed repair, so two further
defects were tracked down - both by dumping this code's own output rather than inferring.

**Attachment table row vs sub-object.** `!!Attachment table for 4C4, row doesn't match
sub-object: irow = 0, RowID = 5` (~2,000) and `!!Attachment (nid=5) missing or invalid
PR_ATTACH_SIZE` (~3,000). Building an attachment PC locally and walking its BTH showed
`PR_ATTACH_SIZE` present and correct (`0x1388` = 5000 for a 5000-byte attachment), so the size
complaint is a cascade, not the fault. What the PC was actually missing is `0x3704`
`PR_ATTACH_FILENAME` - a column the Attachments Table *does* declare. Patch 10 added that column
to the table (because scanpst demanded it there) but never to the attachment's own sub-object PC,
and that asymmetry is precisely what "row doesn't match sub-object" reports. Fixed by adding it
to `build_attachment_pc()`. General rule this implies: every column declared in a TC must also
exist on the sub-object each row points at.

**Associated Contents (FAI) table.** `!!TC (nid=40F) missing required column` for `001A001F`,
`3001001F` and `70030003`. `build_assoc_contents_tc()` built a table with *no columns at all* -
this pipeline never creates folder-associated items, so the table is legitimately empty, but it
still has to declare its required columns. Fixed by declaring `PR_MESSAGE_CLASS`,
`PR_DISPLAY_NAME` and `PR_VIEW_DESCRIPTOR_FLAGS` (new constant) on an otherwise row-less table.

Verified: both fixes confirmed by re-dumping the generated structures (attachment PC now carries
`0x3704`; the assoc TC declares `001A001F`, `3001001F`, `70030003` plus the reserved pair), then
rebuilt the same 4,158-message mailbox - 4,936 pages, 88,174 blocks, 2,153 AMap pages and 275
legacy map pages all verify clean.

Still outstanding after this round, from the same log: `!!Hierarchy Table for 122, row doesn't
match sub-object` (the root folder's hierarchy row for its child folder) - the same class of
complaint as the attachment one but the folder PC does appear to carry every column the
hierarchy table declares, so it needs its own investigation; and the AMAP cluster below.

### 22. **Silent message loss**: a TC's fixed structures must live on heap page 0
The most serious defect found in this whole series, and the only one that caused **silent data
loss**. It surfaced only because the operator passed on a number from scanpst's summary dialog:
it reported repairing *2 folders and 4,145 items*, against 4,158 messages packed. Nothing in the
1.1 MB log named the 13 missing messages or flagged any error for them - Outlook simply discarded
them.

Diffing the message NIDs in the log's "Adding message (nid=...) back" lines against a full NBT
walk of the file identified the 13 exactly. Mapping them back to their source `.eml` files by
allocation index showed no pattern in size (24 KB to 8 MB) or attachments (0 to 2), but a perfect
one in recipient count: **all 13 had >= 20 recipients, and all 4,145 survivors had <= 19, with no
exceptions** (header-only parse of all 4,158 messages to confirm).

Root cause: `build_tc_node()` allocated every per-row variable-length value (display names,
addresses, EntryIDs, search keys) onto the heap *first*, and only afterwards allocated the Row
Matrix, the Row Index BTH and finally TCINFO. So as soon as a table's heap outgrew a single
8176-byte block, all three of those structures landed on heap **page 1**, with `hidUserRoot`
pointing off page 0. ~20 recipients is simply where a real Recipients table crosses that
threshold.

Real PSTs do the reverse, which [MS-PST]'s own "Sample Folder Object" dump shows directly: a real
TC has its RowIndex BTH at HID `0x20` (allocation 1), TCINFO at `0x40` (allocation 2) and Row
Matrix at `0x80` (allocation 4) - all low allocations on page 0 - with variable-length values
after them.

Fixed by reserving those structures up front: `HeapOnNode` gained `allocate_placeholder(size)`
and `patch(hid, data)`, and `build_tc_node()` now claims slots for the BTH header, TCINFO, Row
Matrix and Row Index leaf before building any rows, then fills them in once the per-row HIDs are
known. Sizes are all computable in advance, and an assertion guards TCINFO against drift between
reservation and fill.

Verified at two levels: synthetically at 1/19/20/26/125 recipients (`hidUserRoot`, `hidRowIndex`
and `hnidRows` all resolve to page 0 in every case, where previously 20+ resolved to page 1), and
on the real mailbox by walking the rebuilt PST's NBT/BBT/SLBLOCKs down to each of the 13
previously-lost messages' Recipients TCs - all 13 now have `hidUserRoot` on page 0 (one of them
spanning 7 heap pages).

**Process lesson worth keeping:** every prior round was driven by the error log, and the log was
silent here. A count reported in a GUI dialog was the only signal. For the production run, the
message count in each output PST must be reconciled against the manifest explicitly - a clean
scanpst log is not evidence that nothing was dropped.

### 23. Multi-block HN pages MUST all be exactly 8176 bytes; block 8 needs an HNBITMAPHDR
Patch 22 was necessary but not sufficient: the next round recovered exactly **one** of the 13
lost messages (4,146 of 4,158). Re-measuring with the same NID diff isolated the remaining 12 and
showed the real discriminator was not recipient count but whether the Recipients TC's heap needed
more than one block: **12 of 13 multi-block heaps were lost, 34 of 34 single-block heaps were
fine**.

The cause came from ground truth rather than the spec. Reading a real Microsoft-written
multi-block HN out of the repaired reference PST - the 117-block Contents Table, `nid=0x40E`,
`bClientSig=0x7C` - showed:

| block | size | first allocation offset (= header size) |
| --- | --- | --- |
| 0 | 8176 | 12 (HNHDR) |
| 1, 7, 9 | 8176 | 2 (HNPAGEHDR) |
| **8** | 8176 | **66 (HNBITMAPHDR)** |

Two defects follow from that:

1. **Every block except the last must be exactly `MAX_BLOCK_DATA` (8176).** This code emitted
   pages at their natural length (e.g. 7390 + 1382). A reader locates page N in a multi-block HN
   purely by block index, so short intermediate pages make every allocation past page 0
   unreachable - and Outlook drops the affected messages silently rather than reporting anything.
   Note Microsoft's blocks carry `cFree = 0` with a few unused bytes after the HNPAGEMAP, so the
   fix is simply trailing zero-padding to 8176, not restructuring the page.
2. **Block 8 needs a 66-byte HNBITMAPHDR**, not a 2-byte HNPAGEHDR. Per [MS-PST] 2.3.1.4 its
   64-byte fill-level map covers the next 128 blocks (the HNHDR's own 4-byte map covers only the
   first 8), so bitmap headers land at block 8 and every 128 blocks after. This had never bitten
   because no heap had exceeded 8 pages in testing - but a folder Contents Table on any real bulk
   run certainly does (Microsoft's own is 117 blocks), so it would have corrupted production
   output.

Neither rule is stated in the spec's HN sections - the 8176 requirement is only spelled out for
the Row Matrix (2.3.4.4). Both came from reading bytes out of a PST that Outlook accepts.

Verified: synthetically at 20/26/125/700 recipients (all blocks except the last exactly 8176;
header sizes 12 / 2 / ... / 66 at block 8), and on the real mailbox by walking the rebuilt PST
down to each of the 12 previously-lost messages' Recipients TCs - all now uniform 8176 with
`hidUserRoot` on page 0.

### 24. Attachment layout matched against the reference; patch 21 reverted
With data loss resolved (patch 23), attachments became the largest remaining error family
(~5,900 of 13,689). Instead of theorising again - patch 21's theory had already been disproved -
the real structures were read out of the repaired reference PST and compared field by field.

**What matched already** (confirming patches 16 and 20): the Attachments Table has the same nine
columns in the same tag order, the same `iBit`/`ib_data` assignments, and the same 38-byte row
size as Microsoft's.

**What did not:**

1. **Patch 21 was wrong and is reverted.** Microsoft's attachment sub-object PC carries exactly
   `0x0E20, 0x0E21, 0x3701, 0x3705, 0x3707, 0x370B, 0x370E` - with **no `0x3704`** - while its
   Attachments Table still *declares* a `0x3704` column and simply marks that cell absent in the
   row's Cell Existence Bitmap (`0xF7 0x80`, iBit 4 clear). So patch 21's premise, that every
   column a TC declares must exist on the sub-object, is false: a declared column may legitimately
   be absent per-row. `PR_ATTACH_FILENAME` is no longer written to either the PC or the row value,
   and our generated PC property set and row CEB are now byte-identical to the reference.
2. **`PR_ATTACH_SIZE` means something different from what this code writes - unresolved.**
   Microsoft stores 44089 where the attachment payload is 43959, i.e. **data + 130 bytes**; this
   code writes the raw payload length. The obvious hypothesis (payload + the attachment PC's own
   heap page) was tested and is wrong - that page is 204 bytes, not 130. The formula has *not*
   been determined and is deliberately not guessed at here.

Only the CEB/PC alignment ships in this patch, keeping one variable in play: if the next log
clears "row doesn't match sub-object" but keeps "missing or invalid PR_ATTACH_SIZE", the two are
independent and the size formula can be attacked on its own.

### 25. PR_ATTACH_SIZE is the sum of all property values on the attachment, not the payload length
Patch 24's CEB/`0x3704` alignment made **no difference at all** - the attachment error counts came
back byte-for-byte identical for the second round running (2905 / 711 / 300 / 275 / 275 / 168 /
139 / 95). Two attachment theories were now disproved, which left only the one field known to
differ from the reference: `PR_ATTACH_SIZE`.

Rather than guess at the formula, six attachments were read out of the repaired reference PST
and the difference solved directly:

| payload | filename bytes | MIME bytes | Outlook's PR_ATTACH_SIZE | difference |
| --- | --- | --- | --- | --- |
| 43959 | 84 | 30 | 44089 | 130 |
| 54065 | 110 | 130 | 54321 | 256 |
| 9992 | 62 | 48 | 10118 | 126 |
| 1636519 | 70 | 30 | 1636635 | 116 |
| 2582 | 8 | 18 | 2624 | 42 |

`difference == filenameBytes + mimeBytes + 16` on every sample with no residual, so:

```
PR_ATTACH_SIZE = len(data) + len(PR_ATTACH_LONG_FILENAME) + len(PR_ATTACH_MIME_TAG) + 16
```

The 16 is the four fixed 4-byte properties the attachment PC carries (`PR_ATTACH_SIZE`,
`PR_ATTACH_NUM`, `PR_ATTACH_METHOD`, `PR_RENDERING_POSITION`), so the rule generalises as "the
total stored size of every property value on the attachment object, including this one" - which
matches [MS-OXPROPS]'s vague "size consumed by the Attachment object" and explains why the raw
payload length this code wrote was consistently short. Implemented as
`attachment_stored_size()` in `messaging/message.py` and used for both the Attachments Table
row and the attachment PC, so the two can never diverge.

This is the first attachment change derived from measurement rather than hypothesis, and it may
account for both error families at once: "missing or invalid PR_ATTACH_SIZE" directly, and
"row doesn't match sub-object" if scanpst compares the row's declared size against its own
computed object size.

### 26. PR_ATTACH_SIZE must count *stored* bytes, including heap even-padding
Patch 25 cleared roughly three quarters of the attachment errors - total log errors fell from
13,762 to 9,154 and the log from 28,346 to 16,826 lines - confirming the formula was right. What
remained were 815 `PR_ATTACH_SIZE` and 816 `row doesn't match sub-object` complaints; the two
counts tracking each other showed they were the same attachments.

The residue was a rounding detail rather than a new theory. Among the still-failing messages was
one whose attachments were `logo` (2582 bytes - literally one of the reference samples patch 25
reproduces exactly), `systemIcon` (313) and `ownerIcon` (337). The latter two are **odd-length**,
and `HeapOnNode.allocate()` pads every odd-length heap allocation to an even length (patch 9), so
those values occupy one more byte than they contain and scanpst measures one more byte than
patch 25 declared.

Fixed with `_stored_value_size()`: a value that fits on the heap counts as its length rounded up
to even; a value too large for the heap goes to a subnode and is stored verbatim, so it counts
as-is. Applied to the payload, filename and MIME tag alike.

Worth noting *why* the reference samples couldn't have revealed this: all six were either
even-length or larger than `MAX_HN_ALLOC`, so not one of them exercised the padding path. The
formula was correct and the sample set simply didn't cover the case - a reminder that agreement
with every available sample is not the same as coverage. Re-verified that all six reference
values still reproduce exactly after the change.

### 27. Header NID high-water marks (rgnid) populated
Patch 26 cleared the attachment errors completely - `PR_ATTACH_SIZE` went from ~3,100 complaints
to **zero**, and "row doesn't match sub-object" from ~2,000 to one (which is the folder Hierarchy
Table, not an attachment). Total errors fell 9,154 -> 7,524.

That left the 32 `!!Header NID high-water mark has incorrect value` complaints, deferred since
patch 11. scanpst prints its own computed value alongside the file's, which gives the rule
directly:

```
rgnid[type] = max(0x400, highest nidIndex allocated for that type)
```

Its `calculated=0x400` for every unused type establishes the floor (not zero, which is what this
code left the whole array at); `calculated=0x105E` for `NID_TYPE_NORMAL_MESSAGE` matches this
build's 4,190th and highest message index exactly; and `calculated=0x3701` for `NID_TYPE_LTP`
matches the `PR_ATTACH_DATA_BIN` subnode NID. That last one is the informative part - it is never
a top-level node, so **subnode NIDs count toward the high-water marks too**, and every NID that
reaches the file is now routed through `_note_nid()` rather than just the allocated top-level
ones.

Verified: the rebuilt header has `rgnid[4] = 0x105E`, `rgnid[31] = 0x3701` and `0x400` everywhere
else - exactly scanpst's computed values.

### Correction: patch 21's attachment fix did not work
The attachment error counts came back byte-for-byte identical after patch 21 (2,893 / 711 / 296 /
275 / 270 / 168 / 137 / 95), so adding `PR_ATTACH_FILENAME` to the attachment sub-object PC did
*not* resolve "row doesn't match sub-object" or "missing or invalid PR_ATTACH_SIZE". The
column-parity theory was wrong. The change is harmless and spec-consistent so it stays, but the
real cause is still unknown and needs a fresh look - deliberately not re-theorised here, and the
next log should be read as a test of patch 22 alone.

### Ground truth: what Microsoft's own repaired PST confirmed
The repaired file scanpst produced is the first sample this project has of a PST that real
Outlook accepts, so several long-uncertain assumptions were checked directly against it by
reading its raw pages (over SMB, without downloading 583 MB). All of the following are now
**confirmed correct** in this implementation, not just assumed:

- **AMap anchoring.** Its `AMap0` at `0x4400` begins `ff ff 00 00 ...` - bits 0-7 are the AMap
  page itself and bits 8-15 the PMap page that follows it. So bit 0 anchors at the AMap page's
  *own* file offset, exactly as this code does.
- **AMap positions.** Its `ibAMapLast` is `0x4400 + 2295 * AMAP_COVERAGE` exactly, and its
  `AMap1` sits at `0x42400` (one `AMAP_COVERAGE` on).
- **PMap content and interval.** `0x4600` is an all-`0xFF` PMap (ptype `0x83`), matching patch
  18's content decision; `0x42600` is *not* a PMap, confirming one PMap per eight AMap pages.

This directly **refutes the leading hypothesis recorded under patch 19** - that scanpst anchors
AMap coverage at absolute `k * AMAP_COVERAGE`. It does not. That change was deliberately held
back from patch 19 rather than bundled, which turned out to matter: shipping it would have
corrupted every AMap page in the file for no reason.

A second check killed the follow-up theory too: the tail region those complaints cluster in (the
last `0x4400` bytes of each AMap window) is *not* reserved - 65 of 66 sampled AMap pages in
Microsoft's repaired file have allocations there.

### 28. **SOLVED: AMap bits are MSB-first** - the "not allocated in AMAP" cluster
The ~1,615 `!!Block is not allocated in AMAP!` complaints that survived every previous patch and
three separately refuted hypotheses were caused by writing each AMap byte's bits in the wrong
order. Slot N belongs at bit `7 - (N % 8)` of byte `N // 8` - **most** significant bit first, the
same convention a TC row's Cell Existence Bitmap already used in this codebase - not the
least-significant-first order `ndb/amap.py` used.

Measured, not inferred: across 412 partial (neither `0x00` nor `0xFF`) bytes in
the repaired reference PST, reconstructing the allocation set from its BBT and comparing
bit-by-bit gives

| interpretation | matching bit positions | mismatches |
| --- | --- | --- |
| LSB-first (what this code wrote) | 1464 | 1832 |
| **MSB-first** | **3296** | **0** |

**Why it hid for so long, and why the clustering made sense all along:** a byte covering eight
fully-allocated slots is `0xFF` under either convention, so the bug is invisible everywhere except
where allocated space meets free space. In this writer that happens exactly once per AMap window -
at the end of each packed run, just before the next AMap page - which is precisely the "last
`0x4400` bytes of every window" pattern measured several patches ago and never explained. ~1,615
complaints over ~2,152 windows is about one per window, exactly as that predicts.

**The verification lesson.** The independent from-scratch AMap check reported these bitmaps as
byte-perfect in every single round, including the rounds where three different hypotheses were
being tested and discarded. It could not have caught this: it rebuilt the bitmap using *the same
LSB-first assumption as the writer*. A verifier that shares an assumption with the code it checks
proves self-consistency, not correctness - and the more confident the "independent verification
clean" line looks in a report, the more worth asking which assumptions the two sides hold in
common. The verifier has been corrected too.

### 29. Data-tree middle blocks must be *full*, not merely row-aligned (fixes patch 14)
With the AMap fixed, the log collapsed from 12,751 lines to 8,614 and - more tellingly - the BBT
now validates with **no rebuild at all**. What remained were two `!!Invalid node` lines, and they
named the problem precisely:

```
!!Invalid node (nid=40E, ...): Block size does not match parent's.  Middle page not full:
    cEnt=22, iEnt=0, cbTotal=178794, cbBid=8170
```

`nid=0x40E` is the folder's Contents Table and 8170 is 95 rows x 86 bytes - patch 14's row-aligned
chunking. That patch satisfied one half of [MS-PST] 2.3.4.4 ("each data block MUST store an
integral number of rows") while breaking the other half stated in the same section ("each block
except the last one MUST have a size of 8192 bytes"). Both hold simultaneously because the
leftover is *dead space*: the section explicitly tells readers there "might be unused space at the
end of the data block" which they "MUST ignore".

Fixed in `_store_subnode_data()`: chunks are still cut on row boundaries, but every chunk except
the last is now padded out to `MAX_BLOCK_DATA`, and the XBLOCK's `lcbTotal` counts the padded
total. Rows per block remain exactly `floor(MAX_BLOCK_DATA / row size)`, which is what a reader
computes.

Verified structurally rather than by eye: walked every XBLOCK in the rebuilt file and confirmed
all 4,101 data trees have every middle block at exactly 8176 bytes.

Note this was a self-inflicted regression - patch 14 was written proactively from the spec without
a real-Outlook test to confirm it, and it took until the surrounding noise cleared for the defect
to become visible. Proactive spec-derived changes deserve the same "what does a real file do"
check as reactive ones.

### 30. Folder tables declare their full required column sets
With the NDB layer clean (patches 28/29), the only remaining errors were
`!!TC (nid=...) missing required column`, and scanpst names every tag it wants. Added to
`messaging/folder.py` as `REQUIRED_HIERARCHY_COLUMNS` (7 tags), `REQUIRED_CONTENTS_COLUMNS`
(20) and `REQUIRED_ASSOC_COLUMNS` (8), taken verbatim from those reports.

They are **declared but never given a per-row value**, so each row's Cell Existence Bitmap marks
them absent - the pattern patch 24 established from Microsoft's own attachment table, which
declares `PR_ATTACH_FILENAME` and marks the cell absent. Declaring costs 8 bytes of TCOLDESC plus
the column width in each row and no heap allocation; the Contents Table went from 9 to 30 columns
and 86- to 131-byte rows.

Verified: all three tables' TCOLDESC arrays remain strictly tag-increasing; full rebuild clean at
4,158/4,158.

### 31. Contents Table matched to the reference exactly (28 columns, not 30)
Patch 30 dropped the missing-column complaints from 88 to 49 and cleared every *empty* table.
What remained was a clean split: the tables still reporting were exactly the ones **with rows**
(root Hierarchy with 1 row, Contents with 4,158, Recipients), while every row-less table passed.

Dumping our Hierarchy row byte-for-byte showed nothing wrong with it - layout identical to
Microsoft's `rgib(52,52,53,55)`, and the CEB (`c3c0`) correctly marking exactly the four populated
columns present. The rows are well-formed.

One genuine divergence did turn up: our Contents Table declared **30** columns where a real
Outlook-written one has **28**. We added `PR_HASATTACH` and `PR_SENDER_NAME`; the reference
carries neither, conveying the sender through `PidTagSentRepresentingName` (0x0042001F) which was
already in the required set. Both are now dropped and the sender is written to 0x0042001F, giving
a byte-identical column set and `rgib(120,120,122,126)`.

**Open hypothesis for the remainder** (explicitly a hypothesis - several before it were refuted):
the row-bearing-table complaints may be a cascade from the missing store scaffolding below,
particularly `!!HMP not found - creating`. If scanpst reconstructs store-level objects before
walking folders, its folder-table validation may run against structures it just created rather
than the ones in the file - which would explain why structurally identical empty tables pass and
populated ones do not. If these persist now that the Contents Table matches the reference exactly,
that hypothesis gains weight and the special objects become the clear next target.

### 32. Patch 9 over-padded: only ibHnpm needs alignment, not every allocation
Patch 31 changed nothing - the log came back **byte-identical** (same MD5). More usefully, it
showed scanpst reports *every* user column missing on row-bearing tables regardless of what is
declared, while row-less tables pass. So the complaint was never about which columns exist; the
row data itself was unreadable.

Comparing our root Hierarchy Table against the reference's, byte for byte:

| | Microsoft | ours |
| --- | --- | --- |
| Row Matrix allocation | **55** bytes (1 row x 55) | **56** bytes |
| CEB | `ffa0` | `c3c0` |

Patch 9 padded *every* heap allocation to an even length to fix "bad ibHnpm". Only `ibHnpm` - the
offset of the HNPAGEMAP - actually needs 2-byte alignment, and Microsoft achieves it while storing
that same 55-byte Row Matrix at exactly 55 bytes. The over-padding is harmless for opaque values
but corrupts anything whose stored length is semantically meaningful, and a TC's Row Matrix must
be exactly `rows x rowsize` - so any reader checking that relationship rejects the table, which is
precisely the observed symptom.

Fixed in `ltp/heap.py`: allocations are stored at their exact length, and each page pads its data
area once so `ibHnpm` lands even, with `cFree` counting the alignment byte. Verified against the
reference: our Row Matrix is now 55 bytes for a 55-byte row, `ibHnpm` even, `cFree=1`.

**Patch 26 is reverted as part of this**, and the two must stay coupled: patch 26 existed only to
add back the byte patch 9's padding consumed. Removing the padding without removing the
compensation would make every odd-length inline attachment's `PR_ATTACH_SIZE` one byte too large -
reintroducing the ~800-error class patch 26 fixed, inverted. The dependency is written into
`_stored_value_size()`'s docstring so it cannot drift.

**Near miss worth recording:** the scripted edit that was meant to revert patch 26 failed with a
syntax error and did nothing, so a build briefly existed with padding removed but the
compensation still in. The reference-sample check printed five OKs and did not catch it, because
all five samples are even-length or subnode-stored and none exercises the padding branch - the
exact blind spot noted when patch 26 was derived. A check that cannot fail on the code path being
changed is not evidence about that change; an explicit odd-length case is now asserted alongside
the samples.

### 33. cFree counts freed *allocations*, not spare bytes (regression from patch 32)
Patch 32 removed the per-allocation padding and, in doing so, set the HNPAGEMAP's `cFree` to the
number of alignment pad bytes (0 or 1). `cFree` is the count of **freed allocations**, so
declaring 1 asserted a freed slot that does not exist. scanpst reported
`!!HN (nid=...) free alloc count doesn't match (computed=0, expected=1)` and then **crashed**
(`Fatal Error: 800408C3`), taking the attachment heaps down with it - the
`missing PR_ATTACH_METHOD` / `PR_RENDERING_POSITION` / `PR_ATTACH_SIZE` errors in that log are all
downstream of an unparseable page map, not real regressions.

Reading a real page settled the whole shape at once and confirmed patch 32's approach was right
apart from this one field:

```
Microsoft 0x12D HN page 0:
  ibHnpm=294 (even)  cAlloc=8  cFree=0
  rgibAlloc=[12, 40, 95, 103, 111, 237, 261, 277, 293]
  sentinel=293 (odd) -> 1-byte gap before ibHnpm
  alloc sizes=[28, 55, 8, 8, 126, 24, 16, 16]
```

So Microsoft leaves exactly the same 1-byte alignment gap patch 32 introduced, stores the Row
Matrix at its true 55 bytes, and simply keeps `cFree = 0`. Fixed to match. Building the same
allocation sequence now reproduces `rgibAlloc=[12, 40, 95, 103, 111]` - byte-identical to the
first five entries of Microsoft's page.

### 34. Row Matrix subnode NID must be 0x3F (index 1), not 0x1F (index 0)
Patch 33 removed the crash and cleared the root Hierarchy Table, leaving an unusually sharp
signal: the only tables still reporting every column missing were `0x40E` (Contents, 4,158 rows)
and `0x692` (Recipients) - precisely the ones whose Row Matrix spills into a **subnode**. Every
table small enough to keep its matrix on the heap now passed.

Comparing the Contents Table against the reference found everything else identical: 28 columns,
row size 126, 65 blocks all 8176 but the last, matching `lcbTotal`, 64 rows per block with 112
dead bytes, and the same two-level Row Index BTH (`bIdxLevels=1`). The single difference was the
subnode NID:

| | `hnidRows` | NID type | index |
| --- | --- | --- | --- |
| Microsoft | `0x3F` | NID_TYPE_LTP | **1** |
| ours | `0x1F` | NID_TYPE_LTP | **0** |

Index 0 is reserved - the same trap as row ID 0 in a Row Index BTH (patch 20), where a zero index
is rejected rather than treated as a valid identifier. That explains the split exactly: only a
subnode-backed Row Matrix carries this NID, so heap-backed tables were never affected.

Fixed to `(1 << 5) | NID_TYPE_LTP`. Verified: `hnidRows=0x3F` and the message's subnode list now
reads `['0x3f']`, matching the reference.

**Worth noting as a recurring shape:** this is the third defect in the series caused by using
index 0 where the format reserves it (patch 20's row IDs, and the near-identical reasoning here).
When a NID, row ID or similar identifier is constructed with a zero index, treat that as suspect
by default.

### 35. PR_MESSAGE_SIZE computed twice, disagreeing on every row
Patch 34 landed cleanly: `missing required column: 0` and - for the first time in the series -
`messages re-added: 0`, meaning scanpst had to recover nothing at all. The NDB layer validated
with zero rebuilds. One error category replaced the old ones, at exactly one per message:

```
   4158  !!Contents Table for #, row doesn't match sub-object
```

This is the same error family as the earlier Attachment Table mismatch: scanpst cross-checks each
table row's cells against the sub-object the row points at, and every one of the 4,158 disagreed.

The cause was not structural. `PR_MESSAGE_SIZE` was being produced in two independent places:

| | source | value |
| --- | --- | --- |
| message PC | `len(body_text.encode('utf-16-le'))` | body size |
| Contents row | `parsed_eml.get('message_size', 0)` | **always 0** |

The EML parser never emits a `message_size` key, so the `.get()` default silently won every time -
a mismatch on every message, with no error anywhere in our own pipeline because both writers were
individually "working". Fixed by making it a single shared function, `message_stored_size()`, used
by both call sites, so the two cannot drift again.

The value's *semantics* are unchanged (body size), which the reference PST shows Outlook accepts
without complaint. That is deliberately separate from the open M365 question of whether
`PR_MESSAGE_SIZE` should carry the true item size for quota/size reporting - a fidelity change to
make on purpose, and now changeable in one place.

**Method note - the verifier bug that was actually a correctness proof.** The independent
cross-check written for this indexed the Row Matrix linearly (`irow * rowsize`) and reported 3,646
rows pointing at NIDs that don't exist. That was the *verifier* being wrong, not the file: rows
never straddle a block boundary ([MS-PST] 2.3.4.4), so each 8176-byte block holds
`floor(8176/126) = 64` rows followed by 112 dead bytes. The tell was that failures began at
exactly row 64 and the matrix was 7,168 bytes larger than `rows x rowsize` - precisely
`64 blocks x 112`. Once indexed block-aligned, all 4,158 rows agreed. Two things worth keeping:
a verifier that disagrees with the writer is not automatically right, and "off by exactly one
block's worth" arithmetic is a fast way to tell a reader bug from a writer bug.

Verified on the rebuilt file: 4,158/4,158 rows agree with their message PC across
`PR_MESSAGE_SIZE`, `PR_MESSAGE_FLAGS`, `PR_IMPORTANCE` and `PR_MESSAGE_DELIVERY_TIME`; pages 0
bad, blocks 0 wSig mismatches, AMap 0 bad, legacy maps 0 bad.

### 36. Every cross-checked column must come from one shared map (5 more columns)
Patch 35 fixed `PR_MESSAGE_SIZE` and the resulting scanpst log came back **byte-identical** to the
previous one - same 4,158 `!!Contents Table for #, row doesn't match sub-object`, same md5. That
looked at first like the run had used the wrong file, but the log text never names the offending
column, so fixing one of *several* mismatching columns changes nothing about the output. The
`.bak` (scanpst's pre-repair copy) settled it: it contained our patch-35 row values and none of
the repair-created templates, so the run really had used the new build.

Rather than fix one column per repair cycle, the check was generalised: for every declared column,
compare the row's Cell Existence Bitmap against whether the message PC carries that property. Five
columns were declared but left absent while the PC had them:

| tag | property |
| --- | --- |
| `0x00360003` | PR_SENSITIVITY |
| `0x00390040` | PR_CLIENT_SUBMIT_TIME |
| `0x0E04001F` | PR_DISPLAY_TO |
| `0x0E03001F` | PR_DISPLAY_CC (only where the message has Cc) |
| `0x30080040` | PR_LAST_MODIFICATION_TIME |

`PR_LAST_MODIFICATION_TIME` could not have been fixed by copying a value across: the PC sampled
`filetime_now()` internally, so the row had no way to obtain the same instant.

**The rule this establishes.** Declaring a column and leaving the cell absent is legitimate *only*
when the sub-object has no such property either - that is the `PR_ATTACH_FILENAME` case in patch
24, copied from a real Outlook-written attachment table. It is not licence to omit a value the PC
does carry.

Fixed structurally rather than value-by-value: `message_row_values(parsed_eml, now)` is now the
single source for every value appearing in both a message's PC and its Contents Table row, with
`now` sampled once by the caller and passed to both. `build_message_pc()` consumes the same map.
The map is computed in `add_message()`, where the full parsed EML is still in scope, and only the
small result is retained - the summary dict must not start holding recipients again, which was the
original cause of the whole-machine memory exhaustion.

The same defect existed once at folder level: the Hierarchy Table row omitted `PR_CONTAINER_CLASS`
(`0x3613001F`) while the folder PC carried it - exactly the one
`!!Hierarchy Table for #, row doesn't match sub-object` in the log. Fixed the same way, via
`folder_row_values()`.

Verified on the rebuilt file: all 4,158 contents rows agree with their message PC on **all 28
declared columns** (presence parity plus value equality for every scalar type), the hierarchy row
is clean, and NDB is unchanged - 4,940 pages / 88,245 blocks / 2,155 AMap / 275 legacy pages, 0
bad.

**Method note.** Two lessons here. An identical log is not proof the input was identical - when
the diagnostic doesn't name the specific field, a partial fix is invisible in it, and the
pre-repair `.bak` is better evidence than the log about what was actually repaired. And checking
only the fields you already suspect costs one repair cycle per miss; enumerating *all* of them
against the sub-object found four more in a single pass.

### 37. PR_MESSAGE_SIZE is the message's total data-block bytes
Patch 36 cleared the Hierarchy Table error and took Contents from 4,158 to **4,157** - one
message stopped failing. That single survivor was the whole diagnosis. The log names every
failing row (`irow = N, RowID = ...`), so the one row *not* listed could be identified: `irow
1688`, `RowID 0xD724` - and it is precisely the message whose `PR_MESSAGE_SIZE` we happened to
write correctly by coincidence. That pinned the compared field beyond argument.

It also showed the field is not compared row-to-PC at all. Our row and our PC agreed exactly
(1806); Outlook overwrote **both** with 3112. It recomputes the value from the message itself, so
no amount of row/PC consistency could ever have satisfied it.

**The formula, confirmed exactly.** `PR_MESSAGE_SIZE` is the total number of data-block bytes
the message occupies: its own PC pages, plus every subnode's blocks, recursively (recipients
table, attachment table, each attachment PC, and any property values spilled to subnodes).
XBLOCK tree blocks are not counted - only leaf payloads.

This was fitted offline against ground truth rather than by trial and error: the repaired file's
Contents Table holds Outlook's own corrected value for all 4,158 messages, so the whole
`{nid: correct size}` map could be harvested in one pass and the formula tested locally, with no
repair cycles. Three hypotheses were checked that way in minutes:

| model | result |
| --- | --- |
| body UTF-16 length (what we had) | wrong for ~every message |
| sum of property values + sub-objects + overhead | fit ~74% exactly; residual 204-243, no clean structure |
| **total data-block bytes of message + subnodes** | **exact, 4,157/4,157** |

The second model was seductive - it produced tight residual clusters (214, 228, 230) that looked
like a per-property constant waiting to be fitted. It was a dead end. Measuring what the format
actually stores beat modelling what it ought to cost.

**Implementation.** The value can only be measured after the message and its sub-objects are
built, yet it lives inside the very PC it measures. That is not circular: it is a fixed 4-byte
inline property that is always present, so its value cannot change any size. `add_message()`
builds the PC once with a zero placeholder to measure, then rebuilds it with the real value, and
**asserts** the size did not move - so the reasoning is checked at build time rather than
assumed. The block-chunking used for the measurement is the same `subnode_data_chunks()` the
writer stores with, not a reimplementation of it; duplicating that logic is exactly what caused
patches 35 and 36.

`message_stored_size()` remains only as a documented fallback for direct `build_message_pc()`
callers that cannot measure. It is labelled as incorrect.

Verified offline against all 4,158 ground-truth values: **4,157 exact matches, 0 differences.**
The one remaining message is `0xD724` - the row that passed last round, whose recorded "target" is
our own old body-size value rather than an Outlook-computed one, so it is not authoritative. Our
value for it (19,724 vs 17,994) sits within what appears to be a tolerance, since Outlook accepted
that row while it was 1,730 bytes out. NDB and column parity unchanged: 4,940 pages / 88,245
blocks / 2,155 AMap / 275 legacy pages 0 bad, and all 28 columns still agree row-to-PC.

This also fixes size reporting for the eventual M365 import, which was independently wrong: the
old value counted the plain-text body only, so every message with an HTML body or attachments
under-reported - a large attachment could report a few KB.

### 38. Standard top-level store tables (receive folder, outgoing queue, templates)
Patch 37 cleared every message-level error. The scanpst log went from **12,563 lines to 92**:
zero Contents Table errors (from 4,157), zero rows flagged, zero missing columns, zero messages
re-added, zero rebuilds, and NDB validating clean end to end. Everything left was the
missing-feature category this section had been tracking.

Eleven standard tables are now built (`messaging/special.py`), all transcribed from a real
Outlook-repaired PST rather than spec prose - NIDs, exact column sets, and row counts:

| NID | table | columns | rows |
| --- | --- | --- | --- |
| 0x60D | hierarchy template | 13 | 0 |
| 0x60E | contents template | 28 | 0 |
| 0x60F | associated contents template | 15 | 0 |
| 0x610 | search contents template | 19 | 0 |
| 0x62B | **receive folder table** | 4 | **1** |
| 0x64C | outgoing queue table | 5 | 0 |
| 0x671 | attachment template | 6 | 0 |
| 0x692 | recipient template | 14 | 0 |
| 0x6B6/0x6D7/0x6F8 | templates (NID types 0x16/0x17/0x18) | 5/9/4 | 0 |

All are empty except the receive folder table, whose single row is what
`!!Receive folder table missing default message class` refers to: message class `""` (the empty
string *is* the default) mapped to the root folder, NID 0x122.

Note 0x60E's 28 columns are identical to the Contents Table column set patch 16 arrived at
independently - the templates are where the format states its canonical column sets, so they are
worth consulting directly if another table's column set is ever in doubt.

**NID collision, fixed pre-emptively.** These NIDs are fixed by the format, but their index part
(`nid >> 5`) comes from the same space `_alloc_nid()` hands out, and a folder given a matching
index derives its hierarchy table NID as 0x60D - colliding with the template. A single-folder
test never reaches it; a real mailbox easily has more folders than that, so a
17th folder would have hit it. `RESERVED_NID_INDEXES` makes the allocator skip them. This
shifts every message NID by a few slots, which is invisible in the output but does invalidate
any previously harvested `{nid: value}` ground-truth map - the patch-37 size check had to be
re-expressed as an invariant (`PR_MESSAGE_SIZE == actual block bytes`, 4,158/4,158 exact) rather
than a per-NID comparison.

One known cosmetic difference from the reference: the empty message-class cell allocates a
zero-length string where Microsoft's stores HID 0. Both mean "empty", and our Contents Table rows
already carry zero-length allocations that Outlook accepted without comment, so this is left
alone - but it is the first thing to change if the receive folder table is ever flagged.

Verified: all 11 tables match the reference's column count, row size and row count exactly; NDB
4,941 pages / 88,256 blocks / 2,155 AMap / 275 legacy pages 0 bad; all 28 columns still agree
row-to-PC across 4,158 messages.

### 39. An empty cell value is HID 0, not a zero-length allocation
Patch 38's store tables were accepted - all nine `!!Missing template` lines, `!!Missing the
outgoing queue` and `!!Receive folder table missing` disappeared at once. Three new errors
replaced them, all on the one table that carries a row:

```
!!HN (nid=62B) free alloc count doesn't match (computed=1, expected=0)
!!TC (nid=62B) missing required column (66050003)
!!TC (nid=62B) missing required column (001A001F)
```

The receive folder table's default message class is the *empty string*, and `build_tc_node()`
allocated zero bytes on the heap for it. A zero-length heap entry is indistinguishable from a
freed one, so the reader counted one free allocation against a page map declaring none - and
then could not parse the table at all, which is why both user columns were also reported
missing. (Two symptoms, one cause: the same shape as patch 34, where an unparseable table
reported every column missing.)

Fixed in `ltp/tc.py`: an empty value is stored as **HID 0 with the cell still marked present in
the CEB**, and nothing is allocated. That is exactly what a real Outlook-written receive folder
table contains.

**This was a self-inflicted round.** The difference was already visible in patch 38's own
verification - our row had a real HID where the reference had 0 - and it was noted in that patch
and deliberately left alone, on the reasoning that Contents Table rows already carry zero-length
allocations Outlook accepts. That reasoning was wrong in a specific way worth recording: the
Contents Table never actually exercises the case, because `message_row_values()` omits empty
strings rather than storing them (`PR_SENT_REPRESENTING_NAME` and `PR_DISPLAY_TO` are only added
when non-empty). So the "precedent" for zero-length allocations being fine had no instances in
it. A precedent with no instances is not evidence - and a noted deviation from the reference
costs a full repair cycle to re-learn.

Verified: `0x62B` now has `cAlloc=4, cFree=0` with no zero-length allocations, the row reads
`0x001A001F` present with HID 0 and `0x66050003 = 290` (root folder 0x122), and nothing else
moved - PR_MESSAGE_SIZE invariant 4,158/4,158, all 28 columns still agree row-to-PC, NDB 0 bad.

### 40. Search scaffolding and the HMP - Outlook refuses a store without them
Patch 39 cleared the receive folder table, leaving four log lines that had been assumed cosmetic:
search update queues, the search activity list, and `!!HMP not found - creating`. That assumption
was tested directly - the PST was opened in real Outlook without repairing it first - and Outlook
**rejected it immediately and launched the Inbox Repair Tool**. So these objects are required, not
cosmetic, and the "no message data depends on them" reasoning was beside the point.

Diffing our store against the repaired reference PST (a PST real Outlook *did* open) gave the
complete gap - seven nodes and one property:

| node | form in the accepted reference |
| --- | --- |
| 0x1E1 search management queue | **empty** - NBT entry with `bidData = 0, bidSub = 0` |
| 0x201 search activity list | empty |
| 0xEC1 / 0xEE1 / 0xF21 | empty |
| 0xF01 | 104 bytes, only two non-zero (byte 0 = 8, byte 32 = 1) |
| 0xC01 (HMP) | **56 bytes** |

The empty ones are genuinely empty: present in the NBT, pointing at nothing. Giving them an empty
HN page would be a different structure entirely, so `_add_empty_node()` writes the NBT entry
directly.

**The HMP is not as bad as it looked.** In the *newer* repaired file it is ~105 KB, which had made
it look infeasible to reproduce - but that is a copy Outlook had grown through use. In the
reference, the minimal accepted form is a single 56-byte HN: client signature `0x9C`, holding a
BTH that maps one 16-byte GUID to `0x00000402`. `build_hmp()` reproduces it **byte-for-byte**
(verified by comparison against the reference's own bytes). [MS-PST] does not document the HMP at
all, so this is transcription, not interpretation.

Also added `PidTagReplVersionHistory` (0x0E340102) to the message store - the only remaining
property difference from the accepted reference. No error message ever named it. It is written
because patch 39 was caused precisely by noticing a difference from the reference and choosing to
leave it, and that lesson is worth applying rather than re-learning: **close known deviations from
a working reference before shipping the build.**

After this, our store has the same property set as the reference and there are **no non-message
nodes missing from it at all**. Verified with no regressions: NDB 88,258 blocks / 2,155 AMap / 275
legacy pages 0 bad, all 28 columns agree row-to-PC across 4,158 messages, and PR_MESSAGE_SIZE
matches actual block bytes 4,158/4,158.

**Method note.** The scanpst log was a good guide but a poor specification: it named four
problems, and fixing them meant reproducing seven nodes and a property, only some of which it
mentioned. Diffing against a known-good file found the rest. And the earlier judgement that these
were cosmetic came from reading the log rather than testing the actual goal - opening the file -
which is a reminder to test the real acceptance criterion early, not at the end.

Not yet built: the search-related internals (NIDs 0x1E1, 0x201, 0xEC1, 0xEE1, 0xF01, 0xF21) and
the HMP (0xC01, ~105 KB in the repaired file). These produce the remaining log lines:

```
!!Search folder (nid=EC1) missing update queue
!!Search folder (nid=1E1) missing update queue
!!Search activity list missing
!!HMP not found - creating
```

They are Outlook client-side search scaffolding rather than anything message data depends on,
which is why they were deprioritised behind the store tables above. Whether they matter for an
M365 import is untested.

### 41. Root self-parenting is CORRECT; the missing level was the IPM subtree
M365's import failed both PSTs at zero items (`MaxDepthSurpassed` / `MRSUnknownError`). The root
folder's NBT `dwParentNid` pointing at itself looked like an obvious cycle, and it was changed to 0.

**That change was wrong and has been reverted.** A genuine Outlook-authored PST
(an Outlook-authored reference PST, supplied specifically as a reference) contains exactly the same thing:

```
0x122  root   parent=0x122      <- Microsoft writes a self-parented root
```

This is the lesson the project keeps re-learning, ignored this time: a rule was inferred from spec
prose and shipped without a Microsoft-written file to check it against. Every earlier structural
question settled by reading a real file was settled correctly - AMap bit order, TC column layout,
the 56-byte HMP, PR_MESSAGE_SIZE. The one settled by inference was wrong. **Do not "fix" the
self-parented root again.**

**What the reference did reveal** is a real difference - Outlook's tree has three levels where
ours had two:

| level | Outlook-authored | ours (before) |
| --- | --- | --- |
| store root | `0x122`, self-parented | `0x122`, self-parented |
| IPM subtree | `0x8022` "Outlook Data File", child of root | *absent - the root was the subtree* |
| user folders | `0x8062` "Inbox", child of the subtree | children of the root |

`PidTagIpmSubTreeEntryId` points at a **child** of the root in a real PST; ours pointed at the
root. Fixed: the builder creates an "Outlook Data File" folder under the root, the store's IPM
subtree EntryID points at it, and `add_folder()` puts user folders beneath it. Verified on rebuild:
`0x122 -> 0x402 subtree -> 0x422 user folder`, 4,158/4,158 reconciled, NDB clean.

Whether this is what M365 objected to is **not confirmed** - it is a demonstrated difference from
a working Microsoft file, not a proven cause.

**Also discovered:** the Outlook file sets `bCryptMethod = 0x01` (NDB_CRYPT_PERMUTE, "compressible
encryption") where this pipeline writes `0x00` (none). Both are legal and Outlook reads ours, but
it is another unexamined difference - and it is why raw block parsing of the reference fails, since
its block payloads are permuted. `lspst`/`readpst` handle it and were used instead.

### 42. The store must declare a wastebasket and finder folder
The control experiment settled where the fault lies. Through the **identical** mapping file
(same target mailbox, `IsArchive=TRUE`, blank `TargetRootFolder`), Microsoft 365 imported the
Outlook-authored an Outlook-authored reference PST successfully - **2,218 messages** - while both of ours
failed with `MRSUnknownError` and `Total Items 0`. The environment is fine; the file is ours to fix.

`Total Items 0` is the important part: nothing was ever enumerated, so this is not about message
content. It is about the store failing to open or present its folders.

Our message store declared **only** the IPM subtree:

| property | Outlook-authored store | ours (before) |
| --- | --- | --- |
| PidTagIpmSubTreeEntryId | present | present |
| PidTagIpmWastebasketEntryId | present | **absent** |
| PidTagFinderEntryId | present | **absent** |
| PidTagValidFolderMask | subtree + wastebasket + finder | subtree only (`0x01`) |

There was direct evidence for this and it was walked past twice: **when Outlook opened our PSTs it
created "Deleted Items" and "Search Root" itself** - visible as new folders `0x8042`/`0x8022` in
the files that came back off the share. Outlook was silently repairing a store that declared no
wastebasket and no finder. It was noted at the time as "folders Outlook added" and dismissed as
scaffolding.

Fixed: the builder now creates "Deleted Items" and "Search Root" under the root alongside the
"Outlook Data File" subtree, and the store carries all three EntryIDs with
`PidTagValidFolderMask = 0x89` (subtree | wastebasket | finder). Verified:

```
0x122 "Top of Personal Folders"   (root, self-parented)
  +- 0x402 "Outlook Data File"    <- IPM subtree
  |    +- 0x462 "2025 Jan - Feb"  <- user folder
  +- 0x422 "Deleted Items"        <- wastebasket
  +- 0x442 "Search Root"          <- finder
```

4,158/4,158 reconciled, NDB clean. Whether this is the whole cause is unconfirmed until an import
runs, but unlike the previous two attempts it is a difference from a file *known to import*,
not an inference.

## Patches from the pre-publication review (2026-10-29)

Found after the migration this library was built for had finished, while reviewing and
security-auditing it for publication. Each has a regression test in `tests/`.

### 43. MIME parts other than bodies, attachments, images and application/* were discarded
**The most serious defect found, and inherited unchanged from upstream.** `_process_multipart`
kept the first text/plain part, the first text/html part, parts with an `attachment`
disposition, and image/* or application/* parts. Every other leaf was skipped with no error and
no log line: calendar invites (text/calendar), audio and video, a second text part, and the
contents of forwarded messages. Because `walk()` descends into message/rfc822, a forwarded
message's body parts were judged as if they belonged to the outer message - so when the outer
message already had a body, the forwarded text was dropped, and the forwarded message's own
headers (From/To/Date/Subject) were never carried anywhere.

The PST verifier (`eml2pst/verify.py`) could not see this: it proves every *message* arrived,
and these messages all did - incomplete.

Measured on a 10,000-message random sample of the real archive (3.69M unique messages): 9.1% of
messages lost something substantive - calendar data from meeting invites 6.9%, the body of an
attached/forwarded email 2.1%, extra text parts 0.1% - plus 0.7% that lost only bounce or
read-receipt reports. The rate rose from about 3% before 2018 to 10-12% from 2019 on.

Fixed: every leaf part now goes somewhere. The first text/plain and text/html parts are the
bodies and **everything else is an attachment**. message/* parts are no longer descended into:
a forwarded message is attached whole as an `.eml` (named after its subject, as mail clients
do), delivery-status and disposition-notification reports as `.txt`, and calendar parts as
`.ics`. Ordinary messages (bodies, application/* and image/* attachments) produce exactly what
they did before.

One deliberate behaviour change: upstream could promote a forwarded message's text to be the
outer message's body when the outer message had none. That no longer happens - the forwarded
message is attached, and the outer message's body is its own.

Attachments are still written as plain by-value attachments (`ATTACH_BY_VALUE`) - a forwarded
message is an attached `.eml` file, not an embedded MAPI message object. Outlook opens these
directly, but they will not show as embedded items.

### 44. RFC 2047 encoded-words were stored undecoded
compat32 returns encoded-words as-is, so a subject of `Héllo` was written to the PST as
`=?utf-8?b?SMOpbGxv?=`. Same for sender and recipient display names and attachment filenames.
8.2% of the sampled archive had such a subject - anything with a macron, smart quote, accented
letter or emoji. Fixed with `_decode_words()`. Address lists are split on the raw header and
only the display names decoded afterwards, because decoding first can expose a comma from
inside an encoded-word and split one recipient into two. Malformed encoded-words are left as
found.

### 45. An unparseable Date header rejected the whole message
`parsedate_to_datetime()` raises on input such as `Thu, 31 Feb 2025` or `garbage`, and the
exception discarded the message. Now falls back to the date on the nearest `Received:` header,
then to the current time, as upstream already did for a *missing* Date.

### 46. Malformed charset parameters rejected the whole message (found by fuzzing)
A charset containing NUL or non-ASCII bytes - in an encoded-word, a `charset=` parameter or an
RFC 2231 `filename*=` - makes the email package raise ValueError or CharsetError. Upstream let
that escape from `_decode_payload` and `get_filename()`. These now fall back to UTF-8 with
replacement characters, or to a generated filename.

### 47. The command-line tool crashed on every run
The builder's constructor gained a required output path during patches 1-42, but `cli.py` was
never updated - the migration used the library directly and never ran the CLI. It also still
placed folders and messages under the store root rather than the IPM subtree (patch 41), where
Outlook and Microsoft 365 do not look for them. Both fixed.

### 48. The CLI exited 0 when messages failed, and never verified its output
It now reads every PST it writes back with `eml2pst/verify.py` (which this project used on every
production PST of the migration) and reconciles per-folder counts against what it added. It exits **2** if any
message failed to convert or the read-back disagrees. It builds to `<name>.pst.partial` and only
renames to the requested name once verification passes, so a crash or failed check never leaves
a file that looks like a usable PST. Usage errors exit 1: argparse's default of 2 would have
been indistinguishable from "do not use".

Directory mode also no longer follows symlinked directories: a link back to an ancestor recursed
until Python's recursion limit.

### 49. Invariant checks were `assert` statements
`python -O` strips asserts, after which the writer carried on past a size or layout violation
and produced a structurally invalid PST. All 15 are now explicit `PSTWriteError` raises
(`eml2pst/errors.py`).

### 50. Attachment filenames are made safe for Windows
Characters Windows forbids in filenames (`<>:"/\|?*` and control characters) are replaced with
`_`. A tool that saves attachments to disk under their stored name would otherwise be handed
names such as `..\..\x.exe`.

### Superseded: PST special top-level objects (built by patches 38 and 40)
The same log shows a category that is *not* corruption - a set of standard store objects a full
Outlook-created PST has and this pipeline never creates:

```
!!Receive folder table missing / missing default message class
!!Search folder (nid=EC1) missing update queue
!!Search folder (nid=1E1) missing update queue
!!Search activity list missing
!!Missing template (nid=60D, 60E, 60F, 610, 692, 671, 6B6, 6D7, 6F8)
!!Missing the outgoing queue
!!HMP not found - creating
```

Outlook's repair creates these, which is very likely what now forces repair given the structural
layers validate cleanly. Worth noting they are not equally important: the search infrastructure
and message templates are Outlook *client-side* scaffolding, whereas the Receive Folder Table and
outgoing queue are more fundamental store objects. If this is pursued, rank them by that
distinction rather than implementing all of it.

**Why it matters here:** these PSTs are destined for an M365 bulk import, whose ingestion pipeline
is stricter than Outlook and reports failures far less precisely than scanpst does. A file that
needs repair before Outlook will open it is a poor candidate to feed that pipeline, so closing
this out is risk reduction for the import rather than polish.

### Historical: the "not allocated in AMAP" investigation (resolved by patch 28)
~1,626 `!!Block/XBlock/SBlock is not allocated in AMAP!` complaints have persisted across every
round since the beginning, unchanged by every other fix. Patch 17 did *not* resolve them, so the
missing-PMap theory recorded there was wrong.

What is now established, by cross-referencing the log against a full BBT walk of the file:
- The affected blocks' AMap bits **are** set under this code's model - verified directly, bit by
  bit, for the reported blocks.
- Every single complained-about block satisfies `ib mod AMAP_COVERAGE < 0x4400`. Of all 88,174
  blocks in the file there are **zero** counterexamples.

That subset is precisely the set of blocks for which two different AMap anchoring models disagree
about *which* AMap page owns the block: anchoring each page's coverage window at the page's own
file offset (what this code does) versus anchoring page *k* at absolute file offset
`k * AMAP_COVERAGE` (which would make the header region part of page 0's window). Under the
latter, scanpst would be reading a bit ~272 slots away from where this code set it, and would
complain whenever that other location happens to be clear - consistent with only ~20% (818) of
the 3,976 blocks in that subset actually being reported.

**Both hypotheses above are now refuted** by direct comparison against Microsoft's repaired file
(see the ground-truth section): AMap anchoring is at each page's own offset (what this code
does), and the tail region is not reserved. So the cause is still unknown - what is established
is only that the affected blocks' AMap bits *are* set under a model that Microsoft's own file
agrees with.

Worth keeping in proportion: this affects ~1,617 of 88,174 blocks (~1.8%), it has never
prevented scanpst from completing, and as of the thirteenth round scanpst repairs the file
successfully with these still present. It should not be the next thing worked on unless it turns
out to block a clean (repair-free) open.

## What's still NOT verified

Status at publication (2026-10-29).

**Verified in production.** The writer at patch 42 built 983 PSTs (about 3 TB, over 6 million
messages, including files well above 2 GB, so the FPMap pages of patch 18 were exercised). All
were imported into Microsoft 365, and every message was accounted for by read-back counts at
each step. That run used the parser *before* patches 43-50.

**Not yet verified:**

- **The new attachment types from patch 43 in a real import.** Forwarded messages (`.eml`),
  calendar parts (`.ics`) and delivery reports are written with the same by-value attachment
  structure that imported successfully, and they read back correctly with this library's
  verifier and with readpst. No PST containing them has yet been opened in Outlook or imported
  into Microsoft 365.
- **Forwarded messages are not embedded items.** They are attached `.eml` files
  (`ATTACH_BY_VALUE`), not embedded MAPI messages (`ATTACH_EMBEDDED_MSG`). Outlook opens them,
  but they do not behave like an attached Outlook item.
- **Python versions.** Tests were run locally on Python 3.14. CI is configured for 3.9-3.13.
- **The search scaffolding of patch 40 is minimal.** It is enough for Outlook to open the file
  and for Microsoft 365 to import it. Outlook rebuilds its own search state.
- **ANSI (pre-2003) PST files** are not written and not supported.
- **Encryption.** Output uses `bCryptMethod = 0` (none). Outlook-written files usually use
  "compressible encryption", which is an obfuscation, not security. See `SECURITY.md`.

**Note on method.** Two traps recurred, and are worth knowing before changing the writer:

1. Patches 13, 15 and 16 are three successive corrections of the *same* TCOLDESC layout. Prose
   spec text and error messages took several iterations to converge, where real byte dumps
   settled it in one. Prefer a worked example when a structural fix doesn't hold.
2. A verifier that shares an assumption with the writer proves self-consistency, not
   correctness (patch 28). readpst also accepted files that Outlook rejected as corrupt, so it
   is not sufficient on its own.
