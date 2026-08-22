"""`veikkaus horse` — browse one horse's registry starts in the terminal.

A reader over `archive.heppa_start`: search for a horse on the left, and the
right pane counts its starts and its first three placings four ways — overall,
by shoe combination, by cart, and by the gap since its previous start. All the
SQL, and the reasoning behind it, is in `horse_stats`; this module is the
widgets and nothing else.

Read-only, on a connection opened per query rather than per session, so a
browsing session cannot hold the archive against a concurrent `parse`. See
`archive_db.db_read`.

Clicking a bucket row — or pressing Enter on it — opens the individual starts
behind it, filtered by the same expression that labelled the bucket.

    uv run veikkaus horse
    uv run veikkaus horse 'com milton'
"""
import duckdb
from textual import events, on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.coordinate import Coordinate
from textual.message import Message
from textual.screen import ModalScreen
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


class ClickableTable(DataTable):
    """A breakdown table that opens the starts behind the row you click.

    A subclass, and both halves of that are forced. `DataTable._on_click` calls
    `event.stop()`, so an app-level `@on(events.Click)` never sees the click;
    and a single click posts only `RowHighlighted`, because `RowSelected` wants
    a second click on the same cell. Defining `on_click` here fires on the
    first one, and `event.style.meta['row']` is the row that was clicked — the
    header is -1, a click below the last row has no `row` at all, and
    `self.cursor_row` is still the *old* row at this point, which is why the
    metadata is what we read rather than the cursor.

    It posts its own message rather than reposting `RowSelected`, and that is
    the load-bearing part. `_on_click` posts `RowSelected` itself whenever the
    clicked cell already holds the cursor — which a freshly filled table's
    (0, 0) always does — so a `RowSelected` handler would open two panels for
    one click on the first row, and only on that row. Enter is bound here for
    the same reason: this binding replaces DataTable's own `enter`, since
    bindings merge with the most-derived class winning per key, leaving exactly
    one path per input method.
    """

    BINDINGS = [Binding('enter', 'open_bucket', 'Show starts')]

    class BucketSelected(Message):
        """A bucket row was chosen. `bucket` is None on Overall, which has none."""

        def __init__(self, axis, bucket: str | None):
            super().__init__()
            self.axis = axis
            self.bucket = bucket

    def __init__(self, axis, id: str):
        super().__init__(id=id, cursor_type='row')
        self.axis = axis

    def on_click(self, event: events.Click) -> None:
        self._open(event.style.meta.get('row', -1))

    def action_open_bucket(self) -> None:
        self._open(self.cursor_row)

    def _open(self, row_index: int) -> None:
        """The bucket rides in the row key, as the hit list's canonicalKey does.

        One guard covers the header (-1), the empty space below the last row
        (no `row` in the metadata, so -1 again) and Enter on an empty table.
        """
        if 0 <= row_index < self.row_count:
            key = self.coordinate_to_cell_key(Coordinate(row_index, 0)).row_key
            self.post_message(self.BucketSelected(self.axis, key.value))


class StartsScreen(ModalScreen[None]):
    """The starts behind one bucket.

    DEFAULT_CSS rather than CSS, which would apply to the whole app: this is the
    one place in the file where the distinction bites.
    """

    DEFAULT_CSS = """
    StartsScreen { align: center middle; }
    StartsScreen > Vertical { width: 90%; height: 80%; border: round $accent;
                              background: $surface; padding: 0 1; }
    StartsScreen DataTable { height: 1fr; }
    """

    BINDINGS = [('escape', 'dismiss', 'Close'), ('q', 'dismiss', 'Close')]

    def __init__(self, heading: str, columns, rows):
        super().__init__()
        self.heading = heading
        self.columns = columns
        self.rows = rows

    def compose(self) -> ComposeResult:
        with Vertical():
            yield DataTable(id='starts', cursor_type='row')

    def on_mount(self) -> None:
        box = self.query_one(Vertical)
        box.border_title = self.heading
        box.border_subtitle = 'escape: close'
        table = self.query_one('#starts', DataTable)
        table.add_columns(*self.columns)
        for row in self.rows:
            table.add_row(*(_cell(v) for v in row))


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
        self.horse = None       # the canonicalKey the breakdowns are showing

    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal(id='body'):
            with Vertical(id='hits-pane'):
                yield Input(value=self.prefill, placeholder='horse name…', id='search')
                yield DataTable(id='hits', cursor_type='row')
            with VerticalScroll(id='stats-pane'):
                yield Static(HINT, id='horse')
                for i, axis in enumerate(horse_stats.BREAKDOWNS):
                    yield Label(axis.title, classes='bd-title')
                    yield ClickableTable(axis, id=f'bd{i}')
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
        self.horse = canonical_key
        try:
            with db_read(self.db) as conn:
                heading, failed = canonical_key, []
                for i, axis in enumerate(horse_stats.BREAKDOWNS):
                    table = self.query_one(f'#bd{i}', DataTable)
                    table.clear(columns=True)
                    try:
                        names, rows = horse_stats.fetch(conn, axis.breakdown, [canonical_key])
                    except duckdb.Error as exc:
                        failed.append(f'{axis.title}: {_first_line(exc)}')
                        continue
                    table.add_columns(*_spaced(names, GAP))
                    for row in rows:
                        # The bucket rides on the row key, which is what a click
                        # or an Enter hands back. Overall has no bucket.
                        table.add_row(*_spaced([_cell(v) for v in row]),
                                      key=row[0] if axis.label else None)
                    if not i:
                        heading = _heading(canonical_key, rows)
        except duckdb.Error as exc:
            self._message(f'query failed: {exc}')
            return
        self.query_one('#horse', Static).update(
            heading + (f'   (query failed — {"; ".join(failed)})' if failed else ''))

    @on(ClickableTable.BucketSelected)
    def _drill(self, event: ClickableTable.BucketSelected) -> None:
        """Show the starts behind the chosen bucket.

        The query goes through `_query` like every other read, so a failure is
        a message on the main screen rather than an empty panel.
        """
        if self.horse is None:
            return
        axis, label = event.axis, event.bucket
        result = self._query(horse_stats.bucket_starts, axis, self.horse, label)
        if result is None:
            return
        columns, rows = result
        bucket = f'{axis.title}: {label}' if label else axis.title
        count = f'{len(rows)} start' + ('' if len(rows) == 1 else 's')
        self.push_screen(StartsScreen(f'{self.horse} · {bucket} · {count}', columns, rows))

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
