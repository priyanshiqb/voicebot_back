"""The campaign PDF: the overview on portrait pages, then the members in landscape.

ReportLab rather than an HTML-to-PDF engine: the image is ``python:3.13-slim`` with a
deliberately thin apt layer, and WeasyPrint would need Pango, Cairo and a font stack added
to it. ReportLab is pure Python and carries the standard PDF fonts itself.

**Laid out in memory, not streamed.** ReportLab cannot emit a table row by row the way the
workbook does, which is why the member table is capped (``CAMPAIGN_REPORT_PDF_MAX_ROWS``)
and says so on the page when it is. The workbook is the complete record; this is the
version for reading and forwarding.

**Standard fonts only, and they are Latin-1.** A name written in a non-Latin script does
not render properly here, while it is correct in the workbook. The fix is embedding a
Unicode TTF, which means shipping a font file in the image.
"""

import datetime as dt
import numbers
import re
from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.platypus import (
    BaseDocTemplate,
    Frame,
    LongTable,
    NextPageTemplate,
    PageBreak,
    PageTemplate,
    Paragraph,
    Table,
    TableStyle,
)

from core.services.report_writers import NUMBER

PORTRAIT = A4
LANDSCAPE = landscape(A4)
MARGIN = 14 * mm
FOOTER_SPACE = 8 * mm
OVERVIEW_LABEL_WIDTH = 62 * mm

INK = colors.HexColor("#111827")
MUTED = colors.HexColor("#4B5563")
HEADER_FILL = colors.HexColor("#1F2937")
RULE = colors.HexColor("#E5E7EB")
STRIPE = colors.HexColor("#F3F4F6")

DATETIME_FORMAT = "%d %b %Y %H:%M"

#: XML 1.0 forbids these, and Paragraph text is parsed as XML. Tab, LF and CR are allowed.
CONTROL_CHARACTERS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")

#: A single cell taller than a page is a LayoutError, and one LayoutError fails the whole
#: document. Free text is cut well short of that; the workbook keeps it in full.
MEMBER_TEXT_LIMIT = 300
OVERVIEW_TEXT_LIMIT = 2000

TITLE = ParagraphStyle(
    "ReportTitle", fontName="Helvetica-Bold", fontSize=18, leading=22,
    textColor=INK, spaceAfter=4,
)
SUBTITLE = ParagraphStyle(
    "ReportSubtitle", fontName="Helvetica", fontSize=9, leading=12,
    textColor=MUTED, spaceAfter=6,
)
SECTION = ParagraphStyle(
    "ReportSection", fontName="Helvetica-Bold", fontSize=11, leading=14,
    textColor=INK, spaceBefore=10, spaceAfter=4,
)
NOTE = ParagraphStyle(
    "ReportNote", fontName="Helvetica-Oblique", fontSize=8, leading=10,
    textColor=MUTED, spaceAfter=6,
)
LABEL = ParagraphStyle(
    "OverviewLabel", fontName="Helvetica", fontSize=9, leading=11, textColor=MUTED
)
VALUE = ParagraphStyle(
    "OverviewValue", fontName="Helvetica", fontSize=9, leading=11, textColor=INK
)
CELL = ParagraphStyle(
    "MemberCell", fontName="Helvetica", fontSize=6.5, leading=8, textColor=INK
)
HEADER = ParagraphStyle(
    "MemberHeader", parent=CELL, fontName="Helvetica-Bold", textColor=colors.white
)


def write_pdf(content, target, *, on_progress=None, progress_every=500):
    """Write ``content`` to ``target`` (a path or a binary file). Returns rows written.

    ``content.rows`` is expected to be capped by the caller already; this writes every row
    it is given. The progress callback covers gathering the rows, which is the part that
    reads the database. Layout comes after, and has no row count to report.
    """
    rows = _collect_rows(content, on_progress, progress_every)

    document = BaseDocTemplate(
        target,
        pagesize=PORTRAIT,
        title=_plain(content.title),
        leftMargin=MARGIN,
        rightMargin=MARGIN,
        topMargin=MARGIN,
        bottomMargin=MARGIN,
    )
    # The first template is the one the document opens on.
    document.addPageTemplates([
        _page_template("portrait", PORTRAIT, content.title),
        _page_template("landscape", LANDSCAPE, content.title),
    ])

    story = [
        Paragraph(_markup(content.title), TITLE),
        Paragraph(_markup(content.subtitle), SUBTITLE),
    ]
    for section, pairs in content.overview:
        if not pairs:
            continue
        story.append(Paragraph(_markup(section), SECTION))
        story.append(_overview_table(pairs))

    # Thirteen columns do not fit across a portrait page at any readable size.
    story += [NextPageTemplate("landscape"), PageBreak()]
    story.append(Paragraph("Members", SECTION))
    if len(rows) < content.total_rows:
        story.append(Paragraph(
            _markup(
                f"Showing the first {len(rows):,} of {content.total_rows:,} members. "
                "The Excel report includes everyone."
            ),
            NOTE,
        ))
    story.append(_member_table(content.columns, rows))

    document.build(story)
    return len(rows)


# --------------------------------------------------------------------------- #
# Layout
# --------------------------------------------------------------------------- #

def _page_template(template_id, pagesize, title):
    width, height = pagesize
    frame = Frame(
        MARGIN,
        MARGIN + FOOTER_SPACE,
        width - 2 * MARGIN,
        height - 2 * MARGIN - FOOTER_SPACE,
        id=template_id,
        leftPadding=0,
        rightPadding=0,
        topPadding=0,
        bottomPadding=0,
    )
    footer_title = _plain(title)[:150]

    def draw_footer(canvas, document):
        canvas.saveState()
        canvas.setFont("Helvetica", 7)
        canvas.setFillColor(MUTED)
        canvas.drawString(MARGIN, MARGIN, footer_title)
        canvas.drawRightString(width - MARGIN, MARGIN, f"Page {document.page}")
        canvas.restoreState()

    return PageTemplate(
        id=template_id, frames=[frame], onPage=draw_footer, pagesize=pagesize
    )


def _overview_table(pairs):
    available = PORTRAIT[0] - 2 * MARGIN
    data = [
        [
            Paragraph(_markup(label), LABEL),
            Paragraph(_markup(_truncate(_display(value), OVERVIEW_TEXT_LIMIT)), VALUE),
        ]
        for label, value in pairs
    ]
    table = Table(
        data,
        colWidths=[OVERVIEW_LABEL_WIDTH, available - OVERVIEW_LABEL_WIDTH],
        hAlign="LEFT",
    )
    table.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LINEBELOW", (0, 0), (-1, -1), 0.25, RULE),
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 6),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
    ]))
    return table


def _member_table(columns, rows):
    """A LongTable, which splits across pages and repeats its header on each one.

    Widths are the workbook's column widths scaled to the page, so the two formats keep
    the same proportions without a second set of numbers to maintain.
    """
    available = LANDSCAPE[0] - 2 * MARGIN
    total_width = sum(column.width for column in columns) or 1
    widths = [available * column.width / total_width for column in columns]

    data = [[Paragraph(_markup(column.header), HEADER) for column in columns]]
    data.extend(rows)

    style = [
        ("BACKGROUND", (0, 0), (-1, 0), HEADER_FILL),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        # Numbers are plain strings rather than Paragraphs; this is their font.
        ("FONT", (0, 1), (-1, -1), "Helvetica", 6.5),
        ("TEXTCOLOR", (0, 1), (-1, -1), INK),
        ("LINEBELOW", (0, 0), (-1, -1), 0.25, RULE),
        ("LEFTPADDING", (0, 0), (-1, -1), 3),
        ("RIGHTPADDING", (0, 0), (-1, -1), 3),
        ("TOPPADDING", (0, 0), (-1, -1), 2),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
    ]
    for index, column in enumerate(columns):
        if column.kind == NUMBER:
            style.append(("ALIGN", (index, 1), (index, -1), "RIGHT"))
    if rows:
        style.append(("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, STRIPE]))

    table = LongTable(data, colWidths=widths, repeatRows=1, hAlign="LEFT")
    table.setStyle(TableStyle(style))
    return table


# --------------------------------------------------------------------------- #
# Values
# --------------------------------------------------------------------------- #

def _collect_rows(content, on_progress, progress_every):
    rows = []
    for row in content.rows:
        rows.append([_cell(column, value) for column, value in zip(content.columns, row)])
        if on_progress is not None and len(rows) % progress_every == 0:
            on_progress(len(rows))
    return rows


def _is_number(value):
    return isinstance(value, numbers.Number) and not isinstance(value, bool)


def _cell(column, value):
    if value is None or value == "":
        return ""
    if column.kind == NUMBER and _is_number(value):
        # Short by nature, so a plain string: a Paragraph per number is thousands of
        # objects that never need to wrap.
        return _display(value)
    return Paragraph(_markup(_truncate(_display(value), MEMBER_TEXT_LIMIT)), CELL)


def _display(value):
    if value is None:
        return ""
    if isinstance(value, bool):
        return "Yes" if value else "No"
    if isinstance(value, dt.datetime):
        return value.strftime(DATETIME_FORMAT)
    if isinstance(value, int):
        return f"{value:,}"
    return str(value)


def _truncate(text, limit):
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _markup(text):
    """Plain text as Paragraph markup.

    Paragraph parses its input as a small XML dialect. Unescaped, a contact named
    ``<b>Ada`` turns the rest of the cell bold and a stray ``&`` fails the parse — and a
    failed parse fails the entire document, not one cell.
    """
    cleaned = CONTROL_CHARACTERS.sub("", str(text)).replace("\r\n", "\n")
    return escape(cleaned).replace("\n", "<br/>")


def _plain(text):
    """Text for drawing straight onto the canvas, where markup is not parsed at all."""
    return CONTROL_CHARACTERS.sub("", str(text)).replace("\r", " ").replace("\n", " ")
