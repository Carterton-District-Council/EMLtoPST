"""Folder objects for PST Messaging layer.

Each folder has 4 nodes:
- Folder PC (nid): Property Context with folder properties
- Hierarchy TC (nid | 0x0D): Table Context listing subfolders
- Contents TC (nid | 0x0E): Table Context listing messages
- Associated Contents TC (nid | 0x0F): Table Context for FAI messages

See [MS-PST] 2.4.4.
"""

from ..ltp.pc import build_pc_node
from ..ltp.tc import build_tc_node
from ..mapi.properties import (
    NID_TYPE_HIERARCHY_TABLE, NID_TYPE_CONTENTS_TABLE,
    NID_TYPE_ASSOC_CONTENTS_TABLE,
    PR_DISPLAY_NAME, PR_CONTENT_COUNT, PR_CONTENT_UNREAD_COUNT,
    PR_SUBFOLDERS, PR_CONTAINER_CLASS,
    PR_CREATION_TIME, PR_LAST_MODIFICATION_TIME,
    PR_SUBJECT, PR_MESSAGE_CLASS, PR_MESSAGE_FLAGS,
    PR_MESSAGE_SIZE, PR_MESSAGE_DELIVERY_TIME,
    PR_IMPORTANCE, PR_SENSITIVITY, PR_HASATTACH, PR_SENDER_NAME,
    PR_VIEW_DESCRIPTOR_FLAGS,
)
from ..utils import filetime_now


# Columns real Outlook requires each folder table to *declare*, over and above
# the ones this pipeline populates. scanpst names every tag it wants when it
# reports "TC (nid=...) missing required column", and these lists are taken
# verbatim from those reports.
#
# They are declared as columns but never given a per-row value, so each row's
# Cell Existence Bitmap marks them absent. That is exactly what a real
# Outlook-written PST does with a column it has no data for - see PATCHES.md
# patch 24, where Microsoft's own attachment table declares PR_ATTACH_FILENAME
# and marks the cell absent. Declaring a column costs 8 bytes of TCOLDESC plus
# its width in every row; leaving the cell absent means no heap allocation.
REQUIRED_HIERARCHY_COLUMNS = [
    0x0E300102, 0x0E330014, 0x0E340102, 0x0E380003,
    0x3613001F, 0x66350003, 0x66360003,
]
REQUIRED_CONTENTS_COLUMNS = [
    0x00360003, 0x00390040, 0x0042001F, 0x0057000B, 0x0058000B,
    0x0070001F, 0x00710102, 0x0E03001F, 0x0E04001F, 0x0E170003,
    0x0E300102, 0x0E330014, 0x0E340102, 0x0E380003, 0x0E3C0102,
    0x0E3D0102, 0x10970003, 0x30080040, 0x30130102, 0x65C60003,
]
REQUIRED_ASSOC_COLUMNS = [
    0x6800001F, 0x6803000B, 0x68051003, 0x682F001F,
    0x70040102, 0x70050102, 0x7006001F, 0x70070003,
]

def folder_row_values(display_name, content_count=0, has_subfolders=False,
                      container_class="IPF.Note"):
    """Values shared by a folder's PC and its row in the parent's Hierarchy TC.

    Same rule as message_row_values(): a Hierarchy Table row is cross-checked
    against the folder it points at, so a column the row leaves absent while
    the folder PC carries the property is reported as "Hierarchy Table for
    <nid>, row doesn't match sub-object". PR_CONTAINER_CLASS was absent that
    way - one error, for the one folder with a parent. See PATCHES.md patch 36.

    Note PR_CREATION_TIME / PR_LAST_MODIFICATION_TIME are deliberately not
    here: they are in the folder PC but are not Hierarchy Table columns, so
    nothing cross-checks them and no shared timestamp is needed.
    """
    return {
        PR_DISPLAY_NAME: display_name,
        PR_CONTENT_COUNT: content_count,
        PR_CONTENT_UNREAD_COUNT: 0,
        PR_SUBFOLDERS: has_subfolders,
        PR_CONTAINER_CLASS: container_class,
    }


def build_folder_pc(display_name, content_count=0, has_subfolders=False,
                    container_class="IPF.Note"):
    """Build the folder Property Context.

    Returns:
        Raw bytes for folder PC data block.
    """
    now = filetime_now()
    shared = folder_row_values(display_name, content_count, has_subfolders,
                               container_class)
    properties = [
        (PR_DISPLAY_NAME, shared[PR_DISPLAY_NAME]),
        (PR_CONTENT_COUNT, shared[PR_CONTENT_COUNT]),
        (PR_CONTENT_UNREAD_COUNT, shared[PR_CONTENT_UNREAD_COUNT]),
        (PR_SUBFOLDERS, shared[PR_SUBFOLDERS]),
        (PR_CONTAINER_CLASS, shared[PR_CONTAINER_CLASS]),
        (PR_CREATION_TIME, now),
        (PR_LAST_MODIFICATION_TIME, now),
    ]
    return build_pc_node(properties)


def build_hierarchy_tc(subfolder_rows=None):
    """Build the Hierarchy Table Context (subfolder list).

    Args:
        subfolder_rows: List of dicts with subfolder properties.
            Each dict should have: _nid, PR_DISPLAY_NAME, etc.

    Returns:
        Raw bytes for hierarchy TC data block.
    """
    column_tags = [
        PR_DISPLAY_NAME,
        PR_CONTENT_COUNT,
        PR_CONTENT_UNREAD_COUNT,
        PR_SUBFOLDERS,
        *REQUIRED_HIERARCHY_COLUMNS,
    ]
    rows = subfolder_rows or []
    return build_tc_node(column_tags, rows)


def build_contents_tc(message_rows=None):
    """Build the Contents Table Context (message list).

    Args:
        message_rows: List of dicts with message summary properties.
            Each dict should have: _nid, PR_SUBJECT, PR_MESSAGE_CLASS, etc.

    Returns:
        Raw bytes for contents TC data block.
    """
    column_tags = [
        PR_SUBJECT,
        PR_MESSAGE_CLASS,
        PR_MESSAGE_FLAGS,
        PR_MESSAGE_SIZE,
        PR_MESSAGE_DELIVERY_TIME,
        PR_IMPORTANCE,
        # NB: no PR_HASATTACH and no PR_SENDER_NAME here. A real
        # Outlook-written Contents Table carries exactly 28 columns and
        # neither of those is among them - it conveys the sender via
        # PidTagSentRepresentingName (0x0042001F), which is already in
        # REQUIRED_CONTENTS_COLUMNS below. Declaring extras made this table
        # 30 columns wide where Microsoft's is 28; matching its column set
        # exactly is the principle that has resolved every other table.
        *REQUIRED_CONTENTS_COLUMNS,
    ]
    rows = message_rows or []
    return build_tc_node(column_tags, rows)


def build_assoc_contents_tc():
    """Build an empty Associated Contents (FAI) Table Context.

    The table has no rows - this pipeline never creates folder-associated
    items - but it still needs its required columns declared. Real Outlook's
    Inbox Repair Tool reported "TC (nid=40F) missing required column" for
    exactly these three tags against a column-less table. See PATCHES.md.

    Returns:
        Raw bytes for associated contents TC data block.
    """
    column_tags = [
        PR_MESSAGE_CLASS,
        PR_DISPLAY_NAME,
        PR_VIEW_DESCRIPTOR_FLAGS,
        *REQUIRED_ASSOC_COLUMNS,
    ]
    return build_tc_node(column_tags, [])


def folder_nid_hierarchy(folder_nid):
    """Get hierarchy TC NID for a folder."""
    return (folder_nid & ~0x1F) | NID_TYPE_HIERARCHY_TABLE


def folder_nid_contents(folder_nid):
    """Get contents TC NID for a folder."""
    return (folder_nid & ~0x1F) | NID_TYPE_CONTENTS_TABLE


def folder_nid_assoc(folder_nid):
    """Get associated contents TC NID for a folder."""
    return (folder_nid & ~0x1F) | NID_TYPE_ASSOC_CONTENTS_TABLE
