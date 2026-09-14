"""Format writers for campaign reports.

Each writer turns one ``ReportContent`` into one file and knows nothing about campaigns,
tenants or storage. *What* goes into a report is decided in
``core.services.campaign_reports``; *how it is laid out* is decided here. Keeping those
apart is what makes the workbook and the PDF show the same numbers — there is one place
that computes them.
"""

from dataclasses import dataclass
from typing import Iterable

TEXT = "text"
NUMBER = "number"
DATETIME = "datetime"


@dataclass(frozen=True)
class Column:
    """One member column: its header, its width, and what kind of value it holds."""

    key: str
    header: str
    width: int = 16
    kind: str = TEXT


@dataclass
class ReportContent:
    """Everything a writer needs, and nothing about where it came from.

    ``rows`` is consumed **exactly once**. It is a streaming iterator over a queryset, not
    a list, and that is the whole reason a 100,000-member report fits in a worker. A
    writer that needed to read it twice would need redesigning, not a list.

    Datetimes arrive **naive and already in ``timezone_name``**. Excel has no notion of
    an aware datetime and openpyxl refuses one outright, so which zone a reader sees is
    decided once, by the service, rather than per cell by each writer.

    ``overview`` is ``[(section_title, [(label, value), ...]), ...]``.
    """

    title: str
    subtitle: str
    timezone_name: str
    overview: list
    columns: list
    rows: Iterable
    total_rows: int
