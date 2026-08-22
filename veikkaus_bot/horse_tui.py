"""`veikkaus horse` — browse one horse's registry starts in the terminal.

A reader over `archive.heppa_start`: search for a horse on the left, and the
right pane counts its starts and its first three placings four ways — overall,
by shoe combination, by cart, and by the gap since its previous start. All the
SQL, and the reasoning behind it, is in `horse_stats`; this module is the
widgets and nothing else.

Read-only, on a connection opened per query rather than per session, so a
browsing session cannot hold the archive against a concurrent `parse`. See
`archive_db.db_read`.

    uv run veikkaus horse
    uv run veikkaus horse 'com milton'
"""
import duckdb
from textual import on
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import DataTable, Footer, Header, Input, Label, Static

from . import horse_stats
from .archive_db import DEFAULT_DB, db_read

HINT = 'Type part of a horse name and press Enter.'

# A space, not '', so the spacer column keeps a width of its own: DataTable
# sizes a column from its widest cell, and nothing is not a width.
GAP = ' '

# The shoe and cart codes are Heppa's, kept verbatim through the pipeline, so
# the vocabulary is glossed once here rather than translated in two tables.
LEGEND = ('shoes / cart: K = shod / american sulky · E = barefoot / normal · X = not reported\n'
          'distance: ly < ke < kp < pi, short to long — the start type is the `a` prefix of the '
          'same code\n'
          'absent (withdrawn) starts are excluded throughout')


class HorseStatsApp(App):
    """Search a horse, count its Heppa starts."""

    CSS = """
    #body { height: 1fr; }
    #hits-pane { width: 46; }
    #hits { height: 1fr; }
    #stats-pane { padding: 0 1; }
    #horse { padding: 0 0 1 0; text-style: bold; }
    .bd-title { color: $text-muted; }
    #legend { padding: 1 0 0 0; color: $text-muted; }
    """

    TITLE = 'Heppa starts'

    BINDINGS = [
        ('/', 'search', 'Search'),
        ('escape', 'search', 'Search'),
        ('q', 'quit', 'Quit'),
    ]

    def __init__(self, db: str = DEFAULT_DB, name: str | None = None):
        super().__init__()
        self.db = db
        # Which archive this is looking at is worth having on screen: a mistyped
        # --db has already cost this project a day.
        self.sub_title = db
        self.prefill = name or ''

    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal(id='body'):
            with Vertical(id='hits-pane'):
                yield Input(value=self.prefill, placeholder='horse name…', id='search')
                yield DataTable(id='hits', cursor_type='row')
            with VerticalScroll(id='stats-pane'):
                yield Static(HINT, id='horse')
                for i, (title, _) in enumerate(horse_stats.BREAKDOWNS):
                    yield Label(title, classes='bd-title')
                    yield DataTable(id=f'bd{i}', cursor_type='row')
                yield Static(LEGEND, id='legend')
        yield Footer()

    def on_mount(self) -> None:
        # A prefilled term leaves the focus on its results, so the arrow keys
        # walk the hits straight away; without one there is nothing to walk.
        if self.prefill:
            self.search(self.prefill)
        else:
            self.query_one('#search', Input).focus()

    def action_search(self) -> None:
        """Start a new search, rather than editing the last one.

        Clearing is the point: the box keeps its text after Enter, so without
        this the next term is typed onto the end of the old one.
        """
        search = self.query_one('#search', Input)
        search.clear()
        search.focus()

    @on(Input.Submitted, '#search')
    def _submitted(self, event: Input.Submitted) -> None:
        self.search(event.value)

    @on(DataTable.RowHighlighted, '#hits')
    def _highlighted(self, event: DataTable.RowHighlighted) -> None:
        if event.row_key is not None and event.row_key.value:
            self.show_horse(event.row_key.value)

    def search(self, term: str) -> None:
        """Fill the hit list, and load the top hit."""
        hits = self._query(horse_stats.search_horses, term)
        table = self.query_one('#hits', DataTable)
        table.clear(columns=True)
        if hits is None:
            return
        names, rows = hits
        if not rows:
            self._message(f'no horse matching {term!r}')
            return
        # The last column is the canonicalKey: it is the row's identity, not
        # something to read, so it becomes the row key rather than a column.
        table.add_columns(*names[:-1])
        for row in rows:
            table.add_row(*(_cell(v) for v in row[:-1]), key=row[-1])
        # RowHighlighted does not necessarily fire for a first row added under a
        # cursor already sitting at 0, so the top hit is loaded outright.
        self.show_horse(rows[0][-1])
        table.focus()

    def show_horse(self, canonical_key: str) -> None:
        """Refresh the four breakdown tables for one horse.

        Each breakdown runs on its own inside one connection, so a column an
        older archive does not have costs that one panel rather than the whole
        screen — a read-only reader cannot migrate the archive, by design, and
        three quarters of an answer beats none.
        """
        try:
            with db_read(self.db) as conn:
                heading, failed = canonical_key, []
                for i, (title, sql) in enumerate(horse_stats.BREAKDOWNS):
                    table = self.query_one(f'#bd{i}', DataTable)
                    table.clear(columns=True)
                    try:
                        names, rows = horse_stats.fetch(conn, sql, [canonical_key])
                    except duckdb.Error as exc:
                        failed.append(f'{title}: {_first_line(exc)}')
                        continue
                    table.add_columns(*_spaced(names, GAP))
                    for row in rows:
                        table.add_row(*_spaced([_cell(v) for v in row]))
                    if not i:
                        heading = _heading(canonical_key, rows)
        except duckdb.Error as exc:
            self._message(f'query failed: {exc}')
            return
        self.query_one('#horse', Static).update(
            heading + (f'   (query failed — {"; ".join(failed)})' if failed else ''))

    def _query(self, fn, *args):
        """The search's read: a browser must not die on a query error.

        One `except` covers the ways a real archive refuses to be read — another
        process holding the write lock, a WAL a reader cannot replay, a schema
        older than the query. `show_horse` handles its own, per panel.
        """
        try:
            with db_read(self.db) as conn:
                return fn(conn, *args)
        except duckdb.Error as exc:
            self._message(f'query failed: {exc}')
            return None

    def _message(self, text: str) -> None:
        """Every message and empty state goes in one place."""
        self.query_one('#horse', Static).update(text)
        for i in range(len(horse_stats.BREAKDOWNS)):
            self.query_one(f'#bd{i}', DataTable).clear(columns=True)


def _heading(canonical_key: str, overall) -> str:
    """`key — N starts`, or the honest 'no starts' for a horse that has none.

    Not an edge case worth hiding: 5,419 of the archive's 17,099 horses have no
    `heppa_start` row at all, so "found it, it has nothing here" has to read as
    an answer rather than as a blank screen.
    """
    starts = overall[0][0] if overall else 0
    if not starts:
        return f'{canonical_key} — no starts in archive.heppa_start'
    return f'{canonical_key} — {starts} starts'


def _spaced(values, filler: str = '') -> list:
    """The row, with a blank column in front of its last cell.

    Every breakdown ends in the gallop count, and that column overlaps the
    placings rather than adding to them — a horse can gallop and still win. A
    blank column says so at a glance, which beats a caption explaining that
    `1st` and `gallop` can count the same start.
    """
    return [*values[:-1], filler, values[-1]]


def _first_line(exc) -> str:
    """DuckDB's binder errors carry a multi-line list of candidate columns."""
    return str(exc).splitlines()[0]


def _cell(value) -> str:
    """None renders as empty, as crosscheck._cell does — never as 'None'."""
    return '' if value is None else str(value)


def horse_tui(args):
    """CLI handler: browse a horse's registry starts in a terminal UI."""
    HorseStatsApp(args.db, args.name).run()


if __name__ == '__main__':
    from argparse import Namespace
    horse_tui(Namespace(db=DEFAULT_DB, name=None))
