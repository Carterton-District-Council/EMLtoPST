"""Message objects for PST Messaging layer.

A message has:
- Message PC: Property Context with all message properties
- Recipients TC (subnode): Table of recipients
- Attachments TC (subnode): Table of attachments

See [MS-PST] 2.4.5.
"""

import hashlib

from ..ltp.heap import MAX_HN_ALLOC
from ..ltp.pc import build_pc_node
from ..ltp.tc import build_tc_node
from ..mapi.properties import (
    NID_TYPE_RECIPIENT_TABLE, NID_TYPE_ATTACHMENT_TABLE,
    NID_TYPE_ATTACHMENT, make_nid,
    PR_SUBJECT, PR_BODY, PR_HTML, PR_MESSAGE_CLASS,
    PR_MESSAGE_FLAGS, PR_MESSAGE_SIZE, PR_IMPORTANCE,
    PR_INTERNET_CPID, PR_MESSAGE_CODEPAGE,
    PR_PRIORITY, PR_SENSITIVITY, PR_HASATTACH,
    PR_MESSAGE_DELIVERY_TIME, PR_CLIENT_SUBMIT_TIME,
    PR_CREATION_TIME, PR_LAST_MODIFICATION_TIME,
    PR_SENDER_NAME, PR_SENDER_EMAIL_ADDRESS, PR_SENDER_ADDRTYPE,
    PR_SENT_REPRESENTING_NAME, PR_SENT_REPRESENTING_EMAIL,
    PR_SENT_REPRESENTING_ADDRTYPE,
    PR_DISPLAY_NAME_W, PR_EMAIL_ADDRESS, PR_ADDRTYPE,
    PR_RECIPIENT_TYPE, PR_ROWID,
    PR_RESPONSIBILITY, PR_OBJECT_TYPE, PR_SEARCH_KEY, PR_DISPLAY_TYPE,
    PR_SEND_RICH_INFO, PR_ADDRESS_BOOK_DISPLAY_NAME,
    MAPI_MAILUSER, DT_MAILUSER, PR_ENTRYID, PR_RECORD_KEY,
    PR_DISPLAY_TO, PR_DISPLAY_CC, PR_DISPLAY_BCC, PR_INTERNET_MESSAGE_ID,
    PR_ATTACH_NUM, PR_ATTACH_METHOD, PR_ATTACH_FILENAME,
    PR_ATTACH_LONG_FILENAME, PR_ATTACH_SIZE, PR_ATTACH_DATA_BIN,
    PR_ATTACH_MIME_TAG, PR_RENDERING_POSITION,
    MSGFLAG_READ, MSGFLAG_HASATTACH, ATTACH_BY_VALUE,
    MAPI_TO, MAPI_CC, MAPI_BCC,
)
from ..utils import filetime_now, make_one_off_entry_id


def message_stored_size(parsed_eml):
    """PR_MESSAGE_SIZE for a message, computed once and used everywhere.

    The Contents Table row and the message's own PC are cross-checked against
    each other, so this must be a single shared computation rather than two
    that happen to agree. They previously did not: the PC computed the body's
    UTF-16 length while the Contents row read a `message_size` key the EML
    parser never produces, so every row silently carried 0. Real Outlook
    reported it as "Contents Table for <nid>, row doesn't match sub-object" -
    once per message, all 4,158 of them.

    FALLBACK ONLY. The real value is the message's total stored data-block
    bytes, which only the writer can measure, so PSTFileBuilder.add_message()
    measures it and passes it in as `message_size`; this body-length estimate
    is what direct callers of build_message_pc() get when they don't. It is
    not correct - Outlook recomputes PR_MESSAGE_SIZE from the message's actual
    block usage and rewrites any value that disagrees, which it did for 4,157
    of 4,158 messages. See PATCHES.md patch 37.
    """
    body_text = parsed_eml.get('body_text') or ''
    return len(body_text.encode('utf-16-le'))


def message_row_values(parsed_eml, now, message_size=None):
    """Values that appear BOTH in a message's PC and in its Contents Table row.

    Outlook cross-checks every Contents Table row against the message it points
    at and reports any disagreement as "Contents Table for <nid>, row doesn't
    match sub-object". A column the row leaves absent while the PC carries the
    property counts as a disagreement, so the two must be produced from one
    computation - hence this map, consumed by build_message_pc() and by the
    Contents TC row builder in pst_file.py.

    Declaring a column and leaving the cell absent is only legitimate when the
    sub-object has no such property either (that is the PR_ATTACH_FILENAME case
    in patch 24, copied from a real Outlook-written attachment table). It is
    not a licence to omit a value the PC does have - four columns were absent
    that way (PR_SENSITIVITY, PR_CLIENT_SUBMIT_TIME, PR_DISPLAY_TO,
    PR_LAST_MODIFICATION_TIME), one error per message. See PATCHES.md patch 36.

    `now` is passed in rather than sampled here so the PC and the row get the
    identical timestamp; sampling filetime_now() independently in each would
    reintroduce exactly the drift this function exists to prevent.
    """
    flags = MSGFLAG_READ
    if parsed_eml.get('has_attachments', False):
        flags |= MSGFLAG_HASATTACH

    delivery_time = parsed_eml.get('delivery_time', now)
    submit_time = parsed_eml.get('submit_time', delivery_time)

    recipients = parsed_eml.get('recipients', [])

    def _display_names(recip_type):
        return [
            r.get('name') or r.get('email', '')
            for r in recipients
            if r.get('recipient_type', MAPI_TO) == recip_type
        ]

    values = {
        PR_SUBJECT: parsed_eml.get('subject', '(No Subject)'),
        PR_MESSAGE_CLASS: parsed_eml.get('message_class', 'IPM.Note'),
        PR_MESSAGE_FLAGS: flags,
        PR_MESSAGE_SIZE: (message_stored_size(parsed_eml)
                          if message_size is None else message_size),
        PR_IMPORTANCE: parsed_eml.get('importance', 1),
        PR_SENSITIVITY: parsed_eml.get('sensitivity', 0),
        PR_MESSAGE_DELIVERY_TIME: delivery_time,
        PR_CLIENT_SUBMIT_TIME: submit_time,
        PR_LAST_MODIFICATION_TIME: now,
    }

    to_names = _display_names(MAPI_TO)
    cc_names = _display_names(MAPI_CC)
    if to_names:
        values[PR_DISPLAY_TO] = '; '.join(to_names)
    if cc_names:
        values[PR_DISPLAY_CC] = '; '.join(cc_names)

    sender_name = parsed_eml.get('sender_name', '')
    if sender_name:
        values[PR_SENT_REPRESENTING_NAME] = sender_name

    return values


def build_message_pc(parsed_eml, now=None, message_size=None):
    """Build a message Property Context from parsed EML data.

    Args:
        parsed_eml: Dict with keys:
            subject, body_text, body_html, message_class,
            sender_name, sender_email, delivery_time,
            importance, sensitivity, has_attachments

    Returns:
        Raw bytes for message PC data block.
    """
    if now is None:
        now = filetime_now()
    shared = message_row_values(parsed_eml, now, message_size)
    props = []

    subject = shared[PR_SUBJECT]
    props.append((PR_SUBJECT, subject))
    props.append((PR_MESSAGE_CLASS, shared[PR_MESSAGE_CLASS]))

    internet_message_id = parsed_eml.get('internet_message_id', '')
    if internet_message_id:
        props.append((PR_INTERNET_MESSAGE_ID, internet_message_id))

    # PR_SEARCH_KEY: real Outlook's Inbox Repair Tool flags a message missing
    # this as invalid. [MS-PST]/[MS-OXCMSG] don't mandate a specific
    # derivation - it just needs to be a reasonably unique opaque binary
    # value - so hash the message's own identity (falling back to
    # subject+sender+delivery time when there's no Message-ID) rather than
    # using a random value, so re-running this pipeline on the same source
    # message is deterministic. See PATCHES.md.
    search_key_source = internet_message_id or f"{subject}|{parsed_eml.get('sender_email', '')}|{parsed_eml.get('delivery_time', '')}"
    props.append((PR_SEARCH_KEY, hashlib.md5(search_key_source.encode('utf-8', errors='replace'), usedforsecurity=False).digest()))

    props.append((PR_MESSAGE_FLAGS, shared[PR_MESSAGE_FLAGS]))

    body_html = parsed_eml.get('body_html')
    if body_html:
        if isinstance(body_html, str):
            body_html = body_html.encode('utf-8')
        props.append((PR_HTML, body_html))
        props.append((PR_INTERNET_CPID, 65001))  # UTF-8
        props.append((PR_MESSAGE_CODEPAGE, 65001))

    if parsed_eml.get('body_text'):
        props.append((PR_BODY, parsed_eml['body_text']))
    elif body_html:
        # Generate plain text fallback from HTML for HTML-only messages
        import re
        html_str = body_html.decode('utf-8', errors='replace')
        text = re.sub(r'<[^>]+>', '', html_str)
        text = re.sub(r'&nbsp;', ' ', text)
        text = re.sub(r'&amp;', '&', text)
        text = re.sub(r'&lt;', '<', text)
        text = re.sub(r'&gt;', '>', text)
        text = re.sub(r'\n\s*\n', '\n\n', text).strip()
        if text:
            props.append((PR_BODY, text))

    props.append((PR_IMPORTANCE, shared[PR_IMPORTANCE]))  # 1 = Normal
    props.append((PR_PRIORITY, parsed_eml.get('priority', 0)))  # 0 = Normal
    props.append((PR_SENSITIVITY, shared[PR_SENSITIVITY]))  # 0 = Normal
    props.append((PR_HASATTACH, parsed_eml.get('has_attachments', False)))

    props.append((PR_MESSAGE_DELIVERY_TIME, shared[PR_MESSAGE_DELIVERY_TIME]))
    props.append((PR_CLIENT_SUBMIT_TIME, shared[PR_CLIENT_SUBMIT_TIME]))
    props.append((PR_CREATION_TIME, now))
    props.append((PR_LAST_MODIFICATION_TIME, shared[PR_LAST_MODIFICATION_TIME]))

    props.append((PR_MESSAGE_SIZE, shared[PR_MESSAGE_SIZE]))

    # Sender
    sender_name = parsed_eml.get('sender_name', '')
    sender_email = parsed_eml.get('sender_email', '')
    if sender_name:
        props.append((PR_SENDER_NAME, sender_name))
        props.append((PR_SENT_REPRESENTING_NAME, sender_name))
    if sender_email and '@' in sender_email:
        props.append((PR_SENDER_EMAIL_ADDRESS, sender_email))
        props.append((PR_SENDER_ADDRTYPE, 'SMTP'))
        props.append((PR_SENT_REPRESENTING_EMAIL, sender_email))
        props.append((PR_SENT_REPRESENTING_ADDRTYPE, 'SMTP'))
    elif sender_name:
        # No SMTP address — use display name as email address with EX type
        props.append((PR_SENDER_EMAIL_ADDRESS, sender_name))
        props.append((PR_SENDER_ADDRTYPE, 'EX'))
        props.append((PR_SENT_REPRESENTING_EMAIL, sender_name))
        props.append((PR_SENT_REPRESENTING_ADDRTYPE, 'EX'))

    # Display recipient summary properties (PR_DISPLAY_TO/CC/BCC) - many readers
    # (e.g. readpst) reconstruct To:/Cc: headers from these rather than walking
    # the full Recipients Table, even though MS-OXCMSG documents them as optional.
    recipients = parsed_eml.get('recipients', [])

    def _display_names(recip_type):
        return [
            r.get('name') or r.get('email', '')
            for r in recipients
            if r.get('recipient_type', MAPI_TO) == recip_type
        ]

    bcc_names = _display_names(MAPI_BCC)
    if PR_DISPLAY_TO in shared:
        props.append((PR_DISPLAY_TO, shared[PR_DISPLAY_TO]))
    if PR_DISPLAY_CC in shared:
        props.append((PR_DISPLAY_CC, shared[PR_DISPLAY_CC]))
    if bcc_names:
        props.append((PR_DISPLAY_BCC, '; '.join(bcc_names)))

    return build_pc_node(props)


def build_recipients_tc(recipients):
    """Build a Recipients Table Context.

    Args:
        recipients: List of dicts with keys:
            name, email, recipient_type (MAPI_TO/CC/BCC)

    Returns:
        Raw bytes for recipients TC data block.
    """
    # Real Outlook's Inbox Repair Tool flags a Recipients Table missing any of
    # these as invalid, even though [MS-PST] itself doesn't spell out a
    # mandatory column list this specific - see PATCHES.md.
    column_tags = [
        PR_DISPLAY_NAME_W,
        PR_EMAIL_ADDRESS,
        PR_ADDRTYPE,
        PR_RECIPIENT_TYPE,
        PR_ROWID,
        PR_RESPONSIBILITY,
        PR_OBJECT_TYPE,
        PR_ENTRYID,
        PR_RECORD_KEY,
        PR_SEARCH_KEY,
        PR_DISPLAY_TYPE,
        PR_SEND_RICH_INFO,
        PR_ADDRESS_BOOK_DISPLAY_NAME,
    ]

    rows = []
    for i, recip in enumerate(recipients):
        recip_email = recip.get('email', '')
        recip_name = recip.get('name', recip_email)
        # Use EX addrtype for recipients without SMTP email
        if recip_email and '@' in recip_email:
            addrtype = 'SMTP'
        else:
            addrtype = 'EX'
            # Use display name as email fallback so Outlook shows something
            if not recip_email:
                recip_email = recip_name
        # Row IDs are 1-based. A TC's Row Index BTH is keyed by dwRowID, and
        # real Outlook's Inbox Repair Tool validates each key against a
        # running minimum that starts at 0 with a strict greater-than test -
        # so a first row with dwRowID 0 is rejected as "BTH node (nid=692,
        # hid=120), keys overlap (dwkey=0, dwkeyMin=0)". Recipients was the
        # only table using 0-based row IDs (every other one keys on a NID,
        # which is never 0), and it produced exactly one such complaint per
        # message. See PATCHES.md.
        row_id = i + 1
        entry_id = make_one_off_entry_id(recip_name, addrtype, recip_email)
        # SMTP one-off search keys are conventionally "SMTP:" + the address,
        # uppercased and null-terminated - a well-known real-world convention
        # for unresolved recipients, not literally spelled out in [MS-PST].
        search_key = ('SMTP:' + recip_email).upper().encode('ascii', errors='replace') + b'\x00'
        row = {
            '_nid': row_id,
            PR_DISPLAY_NAME_W: recip_name,
            PR_EMAIL_ADDRESS: recip_email,
            PR_ADDRTYPE: addrtype,
            PR_RECIPIENT_TYPE: recip.get('recipient_type', MAPI_TO),
            PR_ROWID: row_id,
            PR_RESPONSIBILITY: True,
            PR_OBJECT_TYPE: MAPI_MAILUSER,
            PR_ENTRYID: entry_id,
            PR_RECORD_KEY: entry_id,
            PR_SEARCH_KEY: search_key,
            PR_DISPLAY_TYPE: DT_MAILUSER,
            PR_SEND_RICH_INFO: True,
            PR_ADDRESS_BOOK_DISPLAY_NAME: recip_name,
        }
        rows.append(row)

    return build_tc_node(column_tags, rows)


def attachment_stored_size(attachment, attach_num):
    """PR_ATTACH_SIZE: total bytes of every property value on the attachment.

    [MS-OXPROPS] describes PidTagAttachSize only as "the size consumed by the
    Attachment object", which this code previously read as the raw payload
    length. Real Outlook computes something larger, and the exact rule was
    derived by reading six attachments out of an Outlook-accepted PST and
    solving for the difference - it is exactly
    `len(data) + len(long filename) + len(MIME tag) + 16`, matching all six
    samples with no residual (payload 43959 -> 44089, 54065 -> 54321,
    9992 -> 10118, 1636519 -> 1636635, 2582 -> 2624).

    The 16 is the four fixed 4-byte properties the attachment PC carries
    (PR_ATTACH_SIZE, PR_ATTACH_NUM, PR_ATTACH_METHOD, PR_RENDERING_POSITION),
    so the rule generalises as "sum of the stored size of every property
    value on the object, including this one". String lengths are the UTF-16LE
    encoded byte counts actually written to the heap. See PATCHES.md.
    """
    data = attachment.get('data') or b''
    filename = attachment.get('filename', f'attachment_{attach_num}')
    mime_tag = attachment.get('mime_type', 'application/octet-stream')
    return (_stored_value_size(data)
            + _stored_value_size(filename.encode('utf-16-le'))
            + _stored_value_size(mime_tag.encode('utf-16-le'))
            + 16)


def _stored_value_size(encoded):
    """Bytes a property value actually occupies once written.

    Every value now occupies exactly its own length, on the heap or in a
    subnode alike, so this is a straight byte count.

    It stays a named helper because PR_ATTACH_SIZE is defined by what is
    *stored* rather than by what the value contains, and that distinction has
    already bitten once: while the heap padded odd-length allocations to even
    (patch 9), this had to add that byte back (patch 26). Patch 32 removed the
    per-allocation padding, so the compensation had to come back out with it -
    the two are strictly coupled and must not drift apart.
    """
    return len(encoded)


def build_attachments_tc(attachments):
    """Build an Attachments Table Context.

    Args:
        attachments: List of dicts with keys:
            filename, data (bytes), mime_type, size

    Returns:
        Raw bytes for attachments TC data block.
    """
    column_tags = [
        PR_ATTACH_NUM,
        PR_ATTACH_METHOD,
        PR_ATTACH_FILENAME,
        PR_ATTACH_LONG_FILENAME,
        PR_ATTACH_SIZE,
        PR_ATTACH_MIME_TAG,
        PR_RENDERING_POSITION,
    ]

    rows = []
    for i, att in enumerate(attachments):
        filename = att.get('filename', f'attachment_{i}')
        row = {
            '_nid': attachment_subnode_nid(i),
            PR_ATTACH_NUM: i,
            PR_ATTACH_METHOD: ATTACH_BY_VALUE,
            # PR_ATTACH_FILENAME stays *declared* as a column (below) but is
            # deliberately given no value here, so the row's Cell Existence
            # Bitmap marks it absent. That is exactly what a real
            # Outlook-accepted PST does: its attachment row's CEB is 0xF7 0x80
            # with the 0x3704 bit clear, and its attachment PC has no 0x3704
            # at all. See PATCHES.md.
            PR_ATTACH_LONG_FILENAME: filename,
            PR_ATTACH_SIZE: attachment_stored_size(att, i),
            PR_ATTACH_MIME_TAG: att.get('mime_type', 'application/octet-stream'),
            PR_RENDERING_POSITION: 0xFFFFFFFF,
        }
        rows.append(row)

    return build_tc_node(column_tags, rows)


def message_nid_recipients(msg_nid):
    """Get recipients TC local descriptor NID for a message.

    Per [MS-PST] and libpff, this is the well-known fixed NID 0x0692.
    """
    return 0x0692


def message_nid_attachments(msg_nid):
    """Get attachments TC local descriptor NID for a message.

    Per [MS-PST] and libpff, this is the well-known fixed NID 0x0671.
    """
    return 0x0671


def attachment_subnode_nid(attach_num):
    """Get the subnode NID for an individual attachment object.

    Per [MS-PST] 2.4.6.2, each attachment is a subnode with its own PC.
    The NID must match the _nid (dwRowID) in the attachment TC row.
    """
    return make_nid(NID_TYPE_ATTACHMENT, attach_num)


def build_attachment_pc(attachment, attach_num):
    """Build a Property Context for a single attachment object.

    Per [MS-PST] 2.4.6.2, each attachment object has its own PC containing
    properties including PR_ATTACH_DATA_BIN with the actual file data.

    Args:
        attachment: Dict with keys: filename, data (bytes), mime_type, size
        attach_num: Attachment index number.

    Returns:
        Raw bytes for attachment PC data block.
    """
    # PR_ATTACH_FILENAME (0x3704, the legacy 8.3 short name) is deliberately
    # NOT written here. A real Outlook-accepted PST's attachment PC carries
    # exactly these properties - 0x0E20, 0x0E21, 0x3701, 0x3705, 0x3707,
    # 0x370B, 0x370E - with no 0x3704, even though its Attachments Table
    # still *declares* a 0x3704 column (the row simply marks that cell absent
    # in its Cell Existence Bitmap). Patch 21 added 0x3704 here on the theory
    # that a table column must exist on the sub-object; the error counts did
    # not move at all, and the reference PST shows the theory was wrong. See
    # PATCHES.md.
    filename = attachment.get('filename', f'attachment_{attach_num}')
    props = [
        (PR_ATTACH_NUM, attach_num),
        (PR_ATTACH_METHOD, ATTACH_BY_VALUE),
        (PR_ATTACH_LONG_FILENAME, filename),
        (PR_ATTACH_SIZE, attachment_stored_size(attachment, attach_num)),
        (PR_ATTACH_MIME_TAG, attachment.get('mime_type', 'application/octet-stream')),
        (PR_RENDERING_POSITION, 0xFFFFFFFF),
    ]
    if attachment.get('data'):
        props.append((PR_ATTACH_DATA_BIN, attachment['data']))

    return build_pc_node(props)
