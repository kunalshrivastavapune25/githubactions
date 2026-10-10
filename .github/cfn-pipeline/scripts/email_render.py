"""Tiny email builder: one API, two outputs (HTML for mail clients incl. Outlook, plain text fallback).

Layout uses tables and inline styles only, because most corporate mail clients ignore <style>.
"""
from __future__ import annotations

import html
import re
from typing import Any, Iterable

FONT = "Segoe UI,-apple-system,Roboto,Helvetica,Arial,sans-serif"
MONO = "Consolas,SFMono-Regular,Menlo,monospace"
INK, MUTED, LINE, PANEL = "#101828", "#475467", "#EAECF0", "#F9FAFB"

HEADER_COLORS = {"danger": "#B42318", "warn": "#B54708", "info": "#175CD3", "success": "#067647", "neutral": "#475467"}
TONES = {  # background, border, text
    "danger": ("#FEF3F2", "#F04438", "#912018"),
    "warn": ("#FFFAEB", "#F79009", "#93370D"),
    "info": ("#EFF8FF", "#2E90FA", "#194185"),
    "success": ("#ECFDF3", "#12B76A", "#05603A"),
    "neutral": ("#F9FAFB", "#98A2B3", "#344054"),
}
BADGE_TONES = {
    "HIGH": "danger", "MEDIUM": "warn", "LOW": "success", "NONE": "success",
    "Add": "success", "Modify": "info", "Remove": "danger", "Dynamic": "warn", "Import": "info",
    "SyncWithActual": "warn", "MODIFIED": "warn", "DELETED": "danger",
    "ADOPT_LIVE": "info", "REVERT_TO_TEMPLATE": "danger", "NEEDS_REVIEW": "warn",
}


def esc(value: Any) -> str:
    return html.escape("" if value is None else str(value), quote=True)


class Raw:
    """Pre-built HTML (with a plain-text twin) that should not be escaped."""

    def __init__(self, html_text: str, text: str | None = None):
        self.html = html_text
        self.text = text if text is not None else re.sub(r"<[^>]+>", "", html_text)


def badge(label: str, tone: str | None = None) -> Raw:
    bg, _border, fg = TONES[tone or BADGE_TONES.get(label, "neutral")]
    return Raw(f'<span style="display:inline-block;padding:2px 8px;border-radius:999px;background:{bg};color:{fg};'
               f'font-size:11px;font-weight:700;letter-spacing:.02em;white-space:nowrap;">{esc(label)}</span>', label)


def code_inline(value: Any) -> Raw:
    return Raw(f'<code style="font-family:{MONO};font-size:12px;background:#F2F4F7;padding:1px 5px;border-radius:4px;'
               f'color:#1D2939;">{esc(value)}</code>', str(value))


def link(label: str, url: str) -> Raw:
    return Raw(f'<a href="{esc(url)}" style="color:#175CD3;text-decoration:underline;">{esc(label)}</a>', f"{label} <{url}>")


def join(*parts: Any, sep: str = " ") -> Raw:
    return Raw(sep.join(p.html if isinstance(p, Raw) else esc(p) for p in parts),
               sep.join(p.text if isinstance(p, Raw) else str(p) for p in parts))


def _h(value: Any) -> str:
    return value.html if isinstance(value, Raw) else esc(value)


def _t(value: Any) -> str:
    return value.text if isinstance(value, Raw) else ("" if value is None else str(value))


class Email:
    def __init__(self, subject: str, title: str, subtitle: str, tone: str = "info", preheader: str = ""):
        self.subject, self.title, self.subtitle, self.tone, self.preheader = subject, title, subtitle, tone, preheader
        self._html: list[str] = []
        self._text: list[str] = []
        self.attachments: list[tuple[str, bytes, str]] = []

    # ---- building blocks
    def heading(self, text: str, level: int = 2) -> None:
        size = 17 if level == 2 else 15
        top = 26 if level == 2 else 18
        self._html.append(f'<div style="font-size:{size}px;font-weight:700;color:{INK};margin:{top}px 0 10px;">{esc(text)}</div>')
        underline = ("=" if level == 2 else "-") * len(text)
        self._text.append(f"{text.upper() if level == 2 else text}\n{underline}")

    def paragraph(self, value: Any, muted: bool = False) -> None:
        color = MUTED if muted else INK
        self._html.append(f'<div style="font-size:14px;line-height:1.55;color:{color};margin:0 0 10px;">{_h(value)}</div>')
        self._text.append(_t(value))

    def callout(self, value: Any, tone: str = "info", title: str | None = None) -> None:
        bg, border, fg = TONES[tone]
        head = f'<div style="font-weight:700;margin-bottom:4px;">{esc(title)}</div>' if title else ""
        self._html.append(
            f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="margin:6px 0 14px;">'
            f'<tr><td style="background:{bg};border-left:4px solid {border};padding:12px 14px;color:{fg};font-size:14px;'
            f'line-height:1.55;border-radius:6px;">{head}{_h(value)}</td></tr></table>')
        self._text.append(("[" + title + "] " if title else "") + _t(value))

    def facts(self, rows: Iterable[tuple[str, Any]]) -> None:
        cells, text = [], []
        for label, value in rows:
            if value in (None, ""):
                continue
            cells.append(f'<tr><td style="padding:6px 12px 6px 0;color:{MUTED};font-size:13px;white-space:nowrap;'
                         f'vertical-align:top;width:150px;">{esc(label)}</td><td style="padding:6px 0;font-size:13px;'
                         f'color:{INK};word-break:break-word;">{_h(value)}</td></tr>')
            text.append(f"{label}: {_t(value)}")
        self._html.append('<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
                          f'style="border-top:1px solid {LINE};border-bottom:1px solid {LINE};margin:4px 0 14px;">'
                          + "".join(cells) + "</table>")
        self._text.append("\n".join(text))

    def table(self, headers: list[str], rows: list[list[Any]], note: str | None = None) -> None:
        head = "".join(f'<th align="left" style="padding:8px 10px;background:{PANEL};border-bottom:1px solid {LINE};'
                       f'font-size:12px;color:{MUTED};font-weight:600;">{esc(h)}</th>' for h in headers)
        body = []
        for row in rows:
            body.append("<tr>" + "".join(
                f'<td style="padding:8px 10px;border-bottom:1px solid {LINE};font-size:13px;color:{INK};'
                f'vertical-align:top;word-break:break-word;">{_h(cell)}</td>' for cell in row) + "</tr>")
        self._html.append(f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
                          f'style="border:1px solid {LINE};border-radius:6px;border-collapse:separate;margin:4px 0 6px;">'
                          f"<tr>{head}</tr>{''.join(body)}</table>")
        if note:
            self._html.append(f'<div style="font-size:12px;color:{MUTED};margin:0 0 12px;">{esc(note)}</div>')
        self._text.append(" | ".join(headers))
        self._text += [" | ".join(_t(cell) for cell in row) for row in rows]
        if note:
            self._text.append(note)

    def code(self, text: str, title: str | None = None, max_lines: int = 160) -> None:
        lines = (text or "").rstrip().splitlines()
        clipped = len(lines) > max_lines
        shown = "\n".join(lines[:max_lines]) + ("\n# … truncated — see the attachment for the full text" if clipped else "")
        label = (f'<div style="font-size:12px;font-weight:600;color:{MUTED};margin:10px 0 4px;">{esc(title)}</div>'
                 if title else "")
        self._html.append(
            f'{label}<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="margin:0 0 12px;">'
            f'<tr><td bgcolor="#0B1220" style="background:#0B1220;border-radius:6px;padding:12px 14px;">'
            f'<pre style="margin:0;font-family:{MONO};font-size:12px;line-height:1.5;color:#E6EDF3;white-space:pre-wrap;'
            f'word-break:break-word;">{esc(shown)}</pre></td></tr></table>')
        self._text.append((f"{title}:\n" if title else "") + "\n".join("    " + line for line in shown.splitlines()))

    def bullets(self, items: Iterable[Any], ordered: bool = False) -> None:
        items = list(items)
        if not items:
            return
        tag = "ol" if ordered else "ul"
        lis = "".join(f'<li style="margin:0 0 6px;">{_h(i)}</li>' for i in items)
        self._html.append(f'<{tag} style="margin:4px 0 12px 20px;padding:0;font-size:14px;line-height:1.55;color:{INK};">'
                          f"{lis}</{tag}>")
        self._text += [f"{n + 1}. {_t(i)}" if ordered else f"- {_t(i)}" for n, i in enumerate(items)]

    def buttons(self, items: Iterable[tuple[str, str, str]]) -> None:
        cells, text = [], []
        for label, url, tone in items:
            if not url:
                continue
            primary = tone == "primary"
            bg = HEADER_COLORS.get(self.tone, "#175CD3") if primary else "#FFFFFF"
            fg = "#FFFFFF" if primary else "#344054"
            border = bg if primary else "#D0D5DD"
            cells.append(f'<td style="padding:0 8px 8px 0;"><a href="{esc(url)}" style="display:inline-block;padding:10px 16px;'
                         f'background:{bg};color:{fg};border:1px solid {border};border-radius:6px;font-size:14px;'
                         f'font-weight:600;text-decoration:none;">{esc(label)}</a></td>')
            text.append(f"{label}: {url}")
        if cells:
            self._html.append('<table role="presentation" cellpadding="0" cellspacing="0" style="margin:6px 0 10px;">'
                              f"<tr>{''.join(cells)}</tr></table>")
            self._text.append("\n".join(text))

    def divider(self) -> None:
        self._html.append(f'<div style="border-top:1px solid {LINE};margin:20px 0;"></div>')
        self._text.append("-" * 60)

    def attach(self, filename: str, data: bytes | str, mime: str = "text/plain") -> None:
        payload = data.encode("utf-8") if isinstance(data, str) else data
        self.attachments.append((filename, payload, mime))

    # ---- rendering
    def render_html(self, footer: Any) -> str:
        header_bg = HEADER_COLORS.get(self.tone, "#175CD3")
        return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{esc(self.subject)}</title></head>
<body style="margin:0;padding:0;background:#F2F4F7;">
<div style="display:none;max-height:0;overflow:hidden;opacity:0;">{esc(self.preheader)}</div>
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#F2F4F7;padding:24px 8px;">
<tr><td align="center">
<table role="presentation" width="760" cellpadding="0" cellspacing="0" style="width:760px;max-width:100%;background:#FFFFFF;border:1px solid {LINE};border-radius:10px;font-family:{FONT};color:{INK};">
<tr><td bgcolor="{header_bg}" style="background:{header_bg};padding:22px 28px;border-radius:10px 10px 0 0;">
<div style="font-size:12px;letter-spacing:.08em;text-transform:uppercase;color:#FFFFFF;opacity:.85;">CloudFormation deployment pipeline</div>
<div style="font-size:22px;line-height:1.3;font-weight:700;color:#FFFFFF;margin-top:6px;">{esc(self.title)}</div>
<div style="font-size:14px;color:#FFFFFF;opacity:.92;margin-top:6px;">{esc(self.subtitle)}</div>
</td></tr>
<tr><td style="padding:22px 28px 8px;">{''.join(self._html)}</td></tr>
<tr><td style="padding:14px 28px 20px;border-top:1px solid {LINE};font-size:12px;line-height:1.5;color:{MUTED};">{_h(footer)}</td></tr>
</table>
</td></tr></table>
</body></html>"""

    def render_text(self, footer: Any) -> str:
        bar = "=" * 72
        return f"{bar}\n{self.title}\n{self.subtitle}\n{bar}\n\n" + "\n\n".join(self._text) + f"\n\n{bar}\n{_t(footer)}\n"
