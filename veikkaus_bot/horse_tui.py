"""`veikkaus stats` — browse one horse's or one trainer's registry starts.

A reader over `archive.heppa_start`: search on the left, and the right pane
counts the starts and the first three placings seven ways — overall, by shoe
combination, by cart, by distance class, by start type, by the gap since the
previous start, and by driver. All the SQL, and the reasoning behind it, is in
`horse_stats`; this module is the widgets and nothing else.

**Which subject is being counted is app state, not a second app.** `t` switches
between horses and trainers; the widget tree does not change, because both
subjects answer the same seven axes and `horse_stats.Subject` is the only thing
that differs. See `horse_stats.Subject`.

Read-only, on a connection opened per query rather than per session, so a
browsing session cannot hold the archive against a concurrent `parse`. See
`archive_db.db_read`.

Clicking a bucket row — or pressing Enter on it — opens the individual starts
behind it, filtered by the same expression that labelled the bucket.

    uv run veikkaus stats
    uv run veikkaus stats 'com milton'
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

HINT = 'Type part of a {} name and press Enter.'

# A space, not '', so the spacer column keeps a width of its own: DataTable
# sizes a column from its widest cell, and nothing is not a width.
GAP = ' '

# The shoe and cart codes are Heppa's, kept verbatim through the pipeline, so
# the vocabulary is glossed once here rather than translated in two tables.
#
# The layoff line is the one caveat that changes meaning with the subject: the
# gap is always the horse's own, so on a trainer it answers 'how do this
# trainer's horses go off a break' and its unknown bucket is one row per horse
# rather than one row. See `horse_stats._LAYOFF`.
LEGEND = ('shoes / cart: K = shod / american sulky · E = barefoot / normal · X = not reported\n'
          'distance: ly < ke < kp < pi, short to long — the start type is the `a` prefix of the '
          'same code\n'
          'days since previous start is the horse\'s own gap, whoever trained it\n'
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
        """The bucket rides in the row key, as the hit list's identity does.

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
        # add_rows, not a row at a time: Overall on the busiest trainer is 3,701
        # starts over eleven columns. Nothing here needs row keys — a start list
        # is the end of the drill-down, not something to click through.
        table.add_rows([[_cell(v) for v in row] for row in self.rows])


class StatsApp(App):
    """Search a horse or a trainer, count its Heppa starts."""

    CSS = """
    #body { height: 1fr; }
    #hits-pane { width: 46; }
    #hits { height: 1fr; }
    #stats-pane { padding: 0 1; }
    #subject { padding: 0 0 1 0; text-style: bold; }
    .bd-title { color: $text-muted; }
    #legend { padding: 1 0 0 0; color: $text-muted; }
    """

    TITLE = 'Heppa starts'

    BINDINGS = [
        ('/', 'search', 'Search'),
        ('escape', 'search', 'Search'),
        ('t', 'toggle_subject', 'Horse/trainer'),
        ('q', 'quit', 'Quit'),
    ]

    def __init__(self, db: str = DEFAULT_DB, name: str | None = None):
        super().__init__()
        self.db = db
        self.subject = horse_stats.HORSE
        # Which subject this is counting, and which archive it is looking at:
        # both are worth having on screen, and a mistyped --db has already cost
        # this project a day.
        self.sub_title = f'{self.subject.name} · {db}'
        self.prefill = name or ''
        self.key = None         # the identity the breakdowns are showing
        self.label = None       # what to call it, since a trainerId is 19 digits
        self.labels = {}        # identity -> display name, filled by the search

    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal(id='body'):
            with Vertical(id='hits-pane'):
                yield Input(value=self.prefill,
                            placeholder=f'{self.subject.name} name…', id='search')
                yield DataTable(id='hits', cursor_type='row')
            with VerticalScroll(id='stats-pane'):
                yield Static(HINT.format(self.subject.name), id='subject')
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

    def action_toggle_subject(self) -> None:
        """Swap horses for trainers, and re-run the term in the new subject.

        Re-running rather than clearing is what makes `veikkaus stats
        'koivunen'` work: it reports no horse, and one keypress turns that into
        the three trainers. A term that matches nothing in the new subject says
        so, which is an answer too.
        """
        self.subject = (horse_stats.TRAINER if self.subject is horse_stats.HORSE
                        else horse_stats.HORSE)
        self.key = self.label = None
        self.labels = {}
        self.sub_title = f'{self.subject.name} · {self.db}'
        search = self.query_one('#search', Input)
        search.placeholder = f'{self.subject.name} name…'
        if search.value:
            self.search(search.value)
        else:
            self.query_one('#hits', DataTable).clear(columns=True)
            self._message(HINT.format(self.subject.name))
            search.focus()

    @on(Input.Submitted, '#search')
    def _submitted(self, event: Input.Submitted) -> None:
        self.search(event.value)

    @on(DataTable.RowHighlighted, '#hits')
    def _highlighted(self, event: DataTable.RowHighlighted) -> None:
        if event.row_key is not None and event.row_key.value:
            key = event.row_key.value
            self.show_subject(key, self.labels.get(key, key))

    def search(self, term: str) -> None:
        """Fill the hit list, and load the top hit."""
        hits = self._query(horse_stats.search, self.subject, term)
        table = self.query_one('#hits', DataTable)
        table.clear(columns=True)
        if hits is None:
            return
        names, rows = hits
        if not rows:
            self._message(f'no {self.subject.name} matching {term!r}')
            return
        # The last column is the identity — a canonicalKey or a trainerId. It is
        # the row's identity, not something to read, so it becomes the row key
        # rather than a column, and the first column is what to display for it.
        # A trainerId is 19 digits, so that pairing is not optional here.
        self.labels = {row[-1]: _cell(row[0]) for row in rows}
        table.add_columns(*names[:-1])
        for row in rows:
            table.add_row(*(_cell(v) for v in row[:-1]), key=row[-1])
        # RowHighlighted does not necessarily fire for a first row added under a
        # cursor already sitting at 0, so the top hit is loaded outright.
        self.show_subject(rows[0][-1], _cell(rows[0][0]))
        table.focus()

    def show_subject(self, key: str, label: str) -> None:
        """Refresh the seven breakdown tables for one horse or trainer.

        Each breakdown runs on its own inside one connection, so a column an
        older archive does not have costs that one panel rather than the whole
        screen — a read-only reader cannot migrate the archive, by design, and
        six sevenths of an answer beats none.
        """
        self.key, self.label = key, label
        try:
            with db_read(self.db) as conn:
                heading, failed = key, []
                for i, axis in enumerate(horse_stats.BREAKDOWNS):
                    table = self.query_one(f'#bd{i}', DataTable)
                    table.clear(columns=True)
                    try:
                        names, rows = horse_stats.fetch(
                            conn, axis.breakdown(self.subject), [key])
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
                        heading = _heading(label, key, rows)
        except duckdb.Error as exc:
            self._message(f'query failed: {exc}')
            return
        self.query_one('#subject', Static).update(
            heading + (f'   (query failed — {"; ".join(failed)})' if failed else ''))

    @on(ClickableTable.BucketSelected)
    def _drill(self, event: ClickableTable.BucketSelected) -> None:
        """Show the starts behind the chosen bucket.

        The query goes through `_query` like every other read, so a failure is
        a message on the main screen rather than an empty panel.
        """
        if self.key is None:
            return
        axis, label = event.axis, event.bucket
        result = self._query(horse_stats.bucket_starts, self.subject, axis, self.key, label)
        if result is None:
            return
        columns, rows = result
        bucket = f'{axis.title}: {label}' if label else axis.title
        count = f'{len(rows)} start' + ('' if len(rows) == 1 else 's')
        self.push_screen(StartsScreen(f'{self.label or self.key} · {bucket} · {count}',
                                      columns, rows))

    def _query(self, fn, *args):
        """The search's read: a browser must not die on a query error.

        One `except` covers the ways a real archive refuses to be read — another
        process holding the write lock, a WAL a reader cannot replay, a schema
        older than the query. `show_subject` handles its own, per panel.
        """
        try:
            with db_read(self.db) as conn:
                return fn(conn, *args)
        except duckdb.Error as exc:
            self._message(f'query failed: {exc}')
            return None

    def _message(self, text: str) -> None:
        """Every message and empty state goes in one place."""
        self.query_one('#subject', Static).update(text)
        for i in range(len(horse_stats.BREAKDOWNS)):
            self.query_one(f'#bd{i}', DataTable).clear(columns=True)


def _heading(label: str, key: str, overall) -> str:
    """`name · identity — N starts`, or the honest 'no starts'.

    Both halves earn their place. The name is what you searched for, and a
    trainer's identity is 19 digits and unreadable; the identity is what every
    other tool in this repo speaks — a canonicalKey is all over crosscheck
    output — so dropping it would mean retyping what is already on screen.

    'No starts' is not an edge case worth hiding: 5,419 of the archive's 17,099
    horses have no `heppa_start` row at all, so "found it, it has nothing here"
    has to read as an answer rather than as a blank screen. A trainer cannot be
    in that state, existing only as a start row, but the branch costs nothing.
    """
    starts = overall[0][0] if overall else 0
    who = f'{label} · {key}' if label and label != key else key
    if not starts:
        return f'{who} — no starts in archive.heppa_start'
    return f'{who} — {starts} starts'


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


def stats_tui(args):
    """CLI handler: browse a horse's or a trainer's registry starts in a TUI.

    Always opens on horses; `t` switches. There is no --trainer flag because
    the toggle re-runs the term, so one keypress does the same job.
    """
    StatsApp(args.db, args.name).run()


if __name__ == '__main__':
    from argparse import Namespace
    stats_tui(Namespace(db=DEFAULT_DB, name=None))
