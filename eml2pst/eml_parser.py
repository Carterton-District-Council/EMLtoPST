"""EML file parser — extracts MAPI-compatible properties from .eml files.

Uses Python's built-in email module to parse RFC 5322 messages and
converts them into property dicts for the messaging layer.
"""

import email
import email.errors
import email.policy
import email.utils
import re
from email.header import decode_header, make_header
from pathlib import Path

from .utils import datetime_to_filetime
from .mapi.properties import MAPI_TO, MAPI_CC, MAPI_BCC


def _hget(msg, name, default=''):
    """Get a header value as a plain str.

    With policy=email.policy.compat32 (used throughout this module), header
    values containing raw 8-bit bytes come back as an email.header.Header
    object, not a plain str, and every downstream consumer here (and in the
    messaging/ltp layers) assumes str. This must be applied at every header
    read site, not just some of them (Content-Disposition below was already
    wrapped; nothing else was, which crashed on real archive data - see
    PATCHES.md).

    This does NOT decode RFC 2047 encoded-words; see _decode_words().
    """
    value = msg.get(name)
    if value is None:
        return default
    return str(value)


def _decode_words(value):
    """Decode RFC 2047 encoded-words (=?charset?b?...?=) to plain text.

    compat32 returns these undecoded, and upstream wrote them into the PST
    as-is: a subject of "Héllo" was stored as "=?utf-8?b?SMOpbGxv?=". In one
    real archive 8.2% of messages had a subject like this (see PATCHES.md).
    A malformed encoded-word is left exactly as found rather than guessed at.
    """
    if '=?' not in value:
        return value
    try:
        return str(make_header(decode_header(value)))
    except _BAD_PARAM:
        # UnicodeError is a ValueError; a non-ASCII or NUL charset name raises
        # CharsetError (a MessageError) or a bare ValueError - found by fuzzing.
        return value


# A malformed charset parameter (e.g. one containing NUL) makes these
# email-package calls raise rather than return. Upstream let that escape, so
# one bad parameter lost the whole message. Found by fuzzing.
_BAD_PARAM = (ValueError, LookupError, email.errors.MessageError)


def _charset(part):
    """part.get_content_charset(), or utf-8 if missing or unusable."""
    try:
        return part.get_content_charset() or 'utf-8'
    except _BAD_PARAM:
        return 'utf-8'


def _filename(part):
    """part.get_filename() as a decoded str, or None if missing or unusable."""
    try:
        filename = part.get_filename()
    except _BAD_PARAM:
        return None
    if filename is None:
        return None
    return _decode_words(str(filename))  # can be an email.header.Header with compat32 policy


def parse_eml_bytes(data):
    """Parse raw EML content and extract properties for PST message creation.

    Args:
        data: Raw EML content as bytes.

    Returns:
        Same dict as parse_eml_file().
    """
    msg = email.message_from_bytes(data, policy=email.policy.compat32)
    return _parse_message(msg)


def parse_eml_file(filepath):
    """Parse an EML file and extract properties for PST message creation.

    Args:
        filepath: Path to the .eml file.

    Returns:
        Dict with keys:
            subject, body_text, body_html, message_class,
            sender_name, sender_email, delivery_time, submit_time,
            importance, sensitivity, has_attachments,
            recipients: [{name, email, recipient_type}],
            attachments: [{filename, data, mime_type, size}]
    """
    filepath = Path(filepath)
    with open(filepath, 'rb') as f:
        msg = email.message_from_binary_file(f, policy=email.policy.compat32)
    return _parse_message(msg)


def _parse_date(value):
    """RFC 5322 date string -> FILETIME, or None if it can't be used.

    Upstream let parsedate_to_datetime() raise, which rejected the whole
    message over one bad header ("Thu, 31 Feb 2025", "garbage"). A date
    before the FILETIME epoch can't be stored either.
    """
    try:
        dt = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError, OverflowError):
        return None
    if dt is None:
        return None
    ft = datetime_to_filetime(dt)
    return ft if ft >= 0 else None


def _parse_message(msg):
    """Extract properties from a parsed email.Message object."""
    result = {
        'subject': (_decode_words(_hget(msg, 'Subject')) or '(No Subject)'),
        'internet_message_id': (_hget(msg, 'Message-ID') or _hget(msg, 'Message-Id')).strip(),
        'message_class': 'IPM.Note',
        'body_text': None,
        'body_html': None,
        'sender_name': '',
        'sender_email': '',
        'delivery_time': None,
        'submit_time': None,
        'importance': 1,  # Normal
        'priority': 0,
        'sensitivity': 0,
        'has_attachments': False,
        'recipients': [],
        'attachments': [],
    }

    # Parse sender. Addresses are split on the raw header and only the
    # display name decoded afterwards: decoding first could expose a comma
    # or angle bracket from inside an encoded-word and mis-split the list.
    from_header = _hget(msg, 'From')
    if from_header:
        parsed = email.utils.parseaddr(from_header)
        result['sender_name'] = _decode_words(parsed[0]) or parsed[1]
        result['sender_email'] = parsed[1]

    # If sender_email is not a valid SMTP address (no @), try alternatives
    if result['sender_email'] and '@' not in result['sender_email']:
        for alt_header in ('Return-Path', 'Sender', 'Reply-To'):
            alt = _hget(msg, alt_header)
            if alt:
                alt_name, alt_addr = email.utils.parseaddr(alt)
                if alt_addr and '@' in alt_addr:
                    result['sender_email'] = alt_addr
                    if not result['sender_name']:
                        result['sender_name'] = _decode_words(alt_name) or alt_addr
                    break
        # MAILER-DAEMON or other non-SMTP placeholder — clear it
        if '@' not in result.get('sender_email', ''):
            result['sender_email'] = ''

    # Parse date: the Date header, else when the nearest server received it
    # (the date part of a Received header follows its last ';'), else now.
    date_header = _hget(msg, 'Date')
    ft = _parse_date(date_header) if date_header else None
    if ft is None:
        for received in msg.get_all('Received') or []:
            _, sep, when = str(received).rpartition(';')
            if sep:
                ft = _parse_date(when.strip())
                if ft is not None:
                    break
    if ft is None:
        from .utils import filetime_now
        ft = filetime_now()
    result['delivery_time'] = ft
    result['submit_time'] = ft

    # Parse importance / priority
    importance = _hget(msg, 'Importance').lower()
    x_priority = _hget(msg, 'X-Priority').strip()
    if importance == 'high' or x_priority in ('1', '2'):
        result['importance'] = 2
        result['priority'] = -1
    elif importance == 'low' or x_priority in ('4', '5'):
        result['importance'] = 0
        result['priority'] = 1

    # Parse recipients
    for header, rtype in [('To', MAPI_TO), ('Cc', MAPI_CC), ('Bcc', MAPI_BCC)]:
        addr_header = _hget(msg, header)
        if addr_header:
            addrs = email.utils.getaddresses([addr_header])
            for name, addr in addrs:
                if addr:
                    # If addr has no @, it's a bare display name (e.g. "To: Administrator")
                    if '@' in addr:
                        recip_email = addr
                    else:
                        recip_email = ''
                    result['recipients'].append({
                        'name': _decode_words(name) or addr,
                        'email': recip_email,
                        'recipient_type': rtype,
                    })

    # Parse body and attachments
    if msg.is_multipart():
        _process_multipart(msg, result)
    else:
        content_type = msg.get_content_type()
        charset = _charset(msg)
        if content_type == 'text/plain':
            result['body_text'] = _decode_payload(msg, charset)
        elif content_type == 'text/html':
            result['body_html'] = _decode_payload(msg, charset)
        else:
            # Single non-text part — treat as attachment
            _add_attachment(msg, result)

    result['has_attachments'] = len(result['attachments']) > 0

    return result


def _leaf_parts(part):
    """Yield every leaf MIME part in walk() order, with message/* parts as leaves.

    walk() descends into a message/rfc822 part and hands back the forwarded
    message's own body parts as if they belonged to the outer message, which
    loses the forwarded message's headers and, when the outer message already
    has a body, its text too. Keeping message/* parts whole means they are
    attached intact instead.
    """
    if part.get_content_maintype() == 'message':
        yield part
    elif part.is_multipart():
        for sub in part.get_payload():
            yield from _leaf_parts(sub)
    else:
        yield part


def _process_multipart(msg, result):
    """Process a multipart MIME message. Every leaf part ends up somewhere.

    The first text/plain and first text/html parts become the bodies;
    everything else becomes an attachment. Upstream kept only the bodies,
    `attachment`-disposition parts and image/* or application/* parts, and
    silently discarded the rest: calendar invites (text/calendar), forwarded
    messages (message/rfc822), audio, video and second text parts. In one
    real archive that was 9% of messages (see PATCHES.md).
    """
    for part in msg.get_payload():
        for leaf in _leaf_parts(part):
            content_type = leaf.get_content_type()
            disposition = _hget(leaf, 'Content-Disposition').lower()

            if 'attachment' in disposition:
                _add_attachment(leaf, result)
            elif content_type == 'text/plain' and result['body_text'] is None:
                result['body_text'] = _decode_payload(leaf, _charset(leaf))
            elif content_type == 'text/html' and result['body_html'] is None:
                result['body_html'] = _decode_payload(leaf, _charset(leaf))
            else:
                _add_attachment(leaf, result)


def _decode_payload(part, charset='utf-8'):
    """Decode a MIME part's payload to string."""
    payload = part.get_payload(decode=True)
    if payload is None:
        return ''
    try:
        return payload.decode(charset)
    except (ValueError, LookupError):
        # UnicodeDecodeError is a ValueError; so is a charset name containing
        # NUL, which upstream let escape and lose the message (found by fuzzing).
        return payload.decode('utf-8', errors='replace')


# Default extensions for part types that carry no filename. Anything not
# listed keeps upstream's rule of using the MIME subtype.
_DEFAULT_EXTENSIONS = {
    'message/rfc822': 'eml',
    'text/calendar': 'ics',
    'text/plain': 'txt',
    'text/html': 'html',
    'text/rfc822-headers': 'txt',
    'message/delivery-status': 'txt',
    'message/disposition-notification': 'txt',
}

# Characters Windows does not allow in a filename, plus control characters.
_UNSAFE_FILENAME = re.compile(r'[\x00-\x1f<>:"/\\|?*]')


def _message_part_bytes(part):
    """The content of a message/* part as bytes.

    compat32 parses message/* bodies into a list of sub-messages (one for
    message/rfc822, one per header block for delivery-status and similar),
    so get_payload(decode=True) returns None for them.
    """
    if not part.is_multipart():
        return part.get_payload(decode=True)
    return b''.join(sub.as_bytes() for sub in part.get_payload())


def _add_attachment(part, result):
    """Extract attachment data from a MIME part."""
    content_type = part.get_content_type()
    if part.get_content_maintype() == 'message':
        payload = _message_part_bytes(part)
    else:
        payload = part.get_payload(decode=True)
    if payload is None:
        return

    filename = _filename(part)
    if not filename and content_type == 'message/rfc822' and part.is_multipart():
        # Name a forwarded message after its subject, as mail clients do.
        filename = _decode_words(_hget(part.get_payload(0), 'Subject')).strip()
        if filename:
            filename = filename[:100] + '.eml'
    if filename:
        filename = _UNSAFE_FILENAME.sub('_', filename)
    if not filename:
        ext = _DEFAULT_EXTENSIONS.get(content_type, content_type.split('/')[-1])
        filename = f'attachment_{len(result["attachments"])}.{ext}'

    result['attachments'].append({
        'filename': filename,
        'data': payload,
        'mime_type': content_type,
        'size': len(payload),
    })
