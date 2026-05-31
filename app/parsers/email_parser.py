"""Email parsers: .eml (stdlib), .msg (markitdown), .mbox/.mbx (stdlib).

Mirrors the backend's email strategy — RFC 2047 header decoding, a
multi-charset body fallback ladder, HTML-body→markdown, threading headers
(In-Reply-To/References) and Message-ID preserved, and a 10k-message /
50 MB mbox cap — while emitting markdown instead of the backend's
structured ParsedEmail. The structured representation exposes the same
fields as a dict.
"""

from __future__ import annotations

import io
import logging
import re
from pathlib import Path
from typing import Any

from ..config import settings
from ..exceptions import MboxValidationError
from ..metadata import clean
from .base import BaseParser, ParseResult, normalize_markdown

log = logging.getLogger("parser.email")

# Charsets to try when a body part's declared charset fails. Matches the
# backend's ordered fallback; covers ~99% of real-world legacy email.
_BODY_DECODE_FALLBACKS = ("utf-8", "windows-1252", "iso-8859-1")


def _decode_mime_header(value: str | None) -> str:
    """Decode RFC 2047 encoded-word headers (`=?UTF-8?Q?...?=`). Without
    this, non-ASCII subjects come through as literal `=?...?=` garbage."""
    if not value:
        return value or ""
    try:
        from email.header import decode_header

        decoded_parts = decode_header(value)
        return "".join(
            part.decode(enc or "utf-8", errors="replace") if isinstance(part, bytes) else part
            for part, enc in decoded_parts
        )
    except Exception:
        log.debug("RFC2047 header decode failed, using raw value: %r", value, exc_info=True)
        return value


def _decode_part_payload(part: Any) -> str:
    """Decode a body part with multi-charset fallback. Some legacy emails
    declare utf-8 but contain windows-1252 bytes (or vice versa); try the
    declared charset, then the fallback ladder, then utf-8/replace."""
    declared = part.get_content_charset() or "utf-8"
    try:
        payload = part.get_payload(decode=True)
    except Exception:
        log.debug("email body part decode failed, skipping part", exc_info=True)
        return ""
    if payload is None:
        return ""
    if isinstance(payload, str):
        return payload
    candidates: list[str] = [declared]
    for enc in _BODY_DECODE_FALLBACKS:
        if enc.lower() != declared.lower():
            candidates.append(enc)
    for enc in candidates:
        try:
            return payload.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return payload.decode("utf-8", errors="replace")


def _html_to_md(html: str) -> str:
    """HTML email body → markdown via markdownify (a markitdown dep)."""
    import markdownify

    md = markdownify.markdownify(html, heading_style="ATX", strip=["script", "style"])
    return re.sub(r"\n{3,}", "\n\n", md).strip()


def _email_fields(msg: Any) -> dict:
    """Extract structured fields (headers, body markdown, attachments) from
    a stdlib EmailMessage."""
    fields: dict[str, Any] = {
        "subject": _decode_mime_header(msg.get("Subject")) or "(no subject)",
        "from": _decode_mime_header(msg.get("From")) or "(unknown)",
        "to": _decode_mime_header(msg.get("To")) or "",
        "cc": _decode_mime_header(msg.get("Cc")),
        "bcc": _decode_mime_header(msg.get("Bcc")),
        "reply_to": _decode_mime_header(msg.get("Reply-To")),
        "date": msg.get("Date", ""),
        "message_id": msg.get("Message-ID", ""),
        "in_reply_to": msg.get("In-Reply-To", ""),
        "references": msg.get("References", ""),
    }

    body_text = ""
    body_html = ""
    attachments: list[dict] = []
    if msg.is_multipart():
        for part in msg.walk():
            ctype = part.get_content_type()
            disp = (part.get("Content-Disposition") or "").lower()
            filename = part.get_filename()
            is_attachment = "attachment" in disp or (
                "inline" in disp and filename and not ctype.startswith("text/")
            )
            if is_attachment and filename:
                payload = part.get_payload(decode=True) or b""
                attachments.append({"name": filename, "size": len(payload)})
                continue
            if ctype == "text/plain" and not body_text:
                body_text = _decode_part_payload(part)
            elif ctype == "text/html" and not body_html:
                body_html = _decode_part_payload(part)
    else:
        ctype = msg.get_content_type()
        body = _decode_part_payload(msg)
        if ctype == "text/html":
            body_html = body
        else:
            body_text = body

    if body_text:
        fields["body"] = body_text.strip()
    elif body_html:
        fields["body"] = _html_to_md(body_html)
    else:
        fields["body"] = "(no body)"
    fields["attachments"] = attachments
    return fields


def _email_metadata(msg: Any) -> dict:
    """Normalized metadata from message headers (cheap — no body decode):
    subject→title, from→author, Date→created, plus Message-ID for citation."""
    return clean(
        {
            "title": _decode_mime_header(msg.get("Subject")),
            "author": _decode_mime_header(msg.get("From")),
            "created": msg.get("Date", ""),
            "message_id": msg.get("Message-ID", ""),
        }
    )


def _format_email(msg: Any, message_index: int | None = None) -> str:
    """Render one EmailMessage as RAG-friendly markdown (headers block,
    `---`, body, `---`, attachments). `## Message N` prefix when from mbox."""
    f = _email_fields(msg)
    parts: list[str] = []
    if message_index is not None:
        parts.append(f"## Message {message_index}\n")
    parts.append(f"# Subject: {f['subject']}\n")
    meta: list[str] = [f"**From:** {f['from']}", f"**To:** {f['to']}"]
    if f["cc"]:
        meta.append(f"**Cc:** {f['cc']}")
    if f["bcc"]:
        meta.append(f"**Bcc:** {f['bcc']}")
    if f["reply_to"]:
        meta.append(f"**Reply-To:** {f['reply_to']}")
    if f["date"]:
        meta.append(f"**Date:** {f['date']}")
    if f["message_id"]:
        meta.append(f"**Message-ID:** {f['message_id']}")
    if f["in_reply_to"]:
        meta.append(f"**In-Reply-To:** {f['in_reply_to']}")
    if f["references"]:
        meta.append(f"**References:** {f['references']}")
    parts.append("  \n".join(meta))
    parts.append("\n---\n")
    parts.append(f["body"])
    if f["attachments"]:
        parts.append("\n---\n")
        att_lines = ", ".join(
            f"{a['name']} ({a['size'] // 1024} KB)"
            if a["size"] >= 1024
            else f"{a['name']} ({a['size']} bytes)"
            for a in f["attachments"]
        )
        parts.append(f"**Attachments:** {att_lines}")
    return "\n".join(parts)


class EmlParser(BaseParser):
    name = "stdlib-email-eml"
    extensions = (".eml",)
    isolation = True

    def parse(self, path: Path, mode: str) -> ParseResult:
        import email
        from email import policy

        with path.open("rb") as f:
            msg = email.message_from_binary_file(f, policy=policy.default)
        md = _format_email(msg)
        structured = _email_fields(msg) if self.wants_structured(mode) else None
        return ParseResult(
            parser=self.name, markdown=md, structured=structured, metadata=_email_metadata(msg)
        )


class MsgParser(BaseParser):
    name = "markitdown-msg"
    extensions = (".msg",)
    isolation = True

    def parse(self, path: Path, mode: str) -> ParseResult:
        from markitdown import StreamInfo
        from markitdown.converters import OutlookMsgConverter

        with path.open("rb") as f:
            buf = io.BytesIO(f.read())
        result = OutlookMsgConverter().convert(
            file_stream=buf,
            stream_info=StreamInfo(extension=".msg", filename=path.name),
        )
        md = normalize_markdown(result.text_content or "")
        # markitdown's msg converter yields text only, no structured fields.
        structured = {"markdown": md} if self.wants_structured(mode) else None
        return ParseResult(parser=self.name, markdown=md, structured=structured)


class MboxParser(BaseParser):
    name = "stdlib-mailbox-mbox"
    extensions = (".mbox", ".mbx")
    isolation = True

    def parse(self, path: Path, mode: str) -> ParseResult:
        self._validate(path)
        import email
        import mailbox
        from email import policy

        cap_bytes = settings.mbox_max_output_mb * 1024 * 1024
        max_messages = settings.mbox_max_messages
        mbox = mailbox.mbox(str(path))
        parts: list[str] = []
        messages: list[dict] = []
        want_struct = self.wants_structured(mode)
        total_bytes = 0
        truncated_after: int | None = None
        try:
            for i, raw in enumerate(mbox, start=1):
                if i > max_messages:
                    raise MboxValidationError(
                        f"mbox exceeds {max_messages} message limit "
                        "— please split into smaller archives"
                    )
                try:
                    msg = email.message_from_string(str(raw), policy=policy.default)
                except Exception as e:
                    log.warning("mbox message %d failed to parse: %s", i, e)
                    parts.append(f"## Message {i}\n\n(parse error: {type(e).__name__}: {e})")
                    continue
                formatted = _format_email(msg, message_index=i)
                new_total = total_bytes + len(formatted) + 2  # "\n\n" joiner
                if new_total > cap_bytes:
                    truncated_after = i - 1
                    break
                parts.append(formatted)
                if want_struct:
                    messages.append({"index": i, **_email_fields(msg)})
                total_bytes = new_total
        finally:
            mbox.close()

        if not parts:
            raise MboxValidationError("mbox contains no parseable messages")

        if truncated_after is not None:
            header = (
                f"# Mailbox: {path.name} "
                f"({len(parts)} messages extracted; output truncated at message "
                f"{truncated_after} — {settings.mbox_max_output_mb}MB cap reached, "
                f"remaining messages omitted)\n"
            )
        else:
            suffix = "s" if len(parts) != 1 else ""
            header = f"# Mailbox: {path.name} ({len(parts)} message{suffix})\n"
        md = header + "\n\n".join(parts)
        structured = (
            {"messages": messages, "truncated_after": truncated_after} if want_struct else None
        )
        metadata = {"message_count": len(parts)}
        return ParseResult(parser=self.name, markdown=md, structured=structured, metadata=metadata)

    @staticmethod
    def _validate(path: Path) -> None:
        """Reject empty files and files missing the `From ` signature before
        handing to mailbox.mbox (which would OOM or produce one bogus
        message). Mirrors the backend's MboxContentExtractor.validate()."""
        if path.stat().st_size == 0:
            raise MboxValidationError("empty mbox file (0 bytes)")
        with path.open("rb") as f:
            head = f.read(256)
        if not (head.startswith(b"From ") or b"\nFrom " in head):
            raise MboxValidationError("missing mbox signature: file does not start with 'From '")
