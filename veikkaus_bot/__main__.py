import argparse
import datetime
import os
import signal
import sys

from .archive_db import DEFAULT_DB
from .crawler import backfill, status
from .crosscheck import crosscheck
from .fetcher import DEFAULT_DELAY
from .heppa import backfill as heppa_backfill
from .heppa import SEED_FILE
from .heppa import backfill_foreign as heppa_foreign
from .heppa import backfill_horse_stats as heppa_stats
from .heppa import backfill_horses as heppa_horses
from .horse_tui import stats_tui
from .parse import parse

DEFAULT_RAW = 'data/raw'

PACKAGE_NAME = 'veikkaus_bot'
PVM = datetime.datetime.now().strftime("%d%m%Y")



def require_db(args):
    """Refuse a --db path that is not already there.

    DuckDB creates a database for whatever path it is handed, so a mistyped
    --db does not fail — it mints an empty archive, and the command then
    reports zero of everything as though the work had never been done. One
    such file turned up in data/ as `veikkaus_data`, the default path minus
    its suffix, and looked exactly like a 297 MB archive gone missing.

    The crawls and the parse do have to create the database the first time, so
    they take --create-db and say so. `status` and `crosscheck` only ever read
    one, so for them there is nothing to opt into.
    """
    if os.path.exists(args.db) or getattr(args, 'create_db', False):
        return
    hint = ' Pass --create-db to start a new one.' if hasattr(args, 'create_db') else ''
    sys.exit(f'{PACKAGE_NAME}: no database at {args.db}.{hint}')


def register_exit_handler(func):
    signal.signal(signal.SIGTERM, func)


def sigterm_exit(_signum=None, _frame=None):
    """Leave quietly, whether SIGTERM sent us here or the dispatch tail did.

    Both parameters exist because `signal.signal` calls a handler with (signum,
    frame) and this is also called directly with neither. With only one, a real
    SIGTERM raised TypeError *inside the handler* — so instead of exiting, the
    crawl blew up in `time.sleep` and took `db_ops`'s `conn.close()` down with
    it. Seen on a `heppa-foreign` run killed at 1,500 fetches; nothing was lost,
    because DuckDB had committed each statement as it went, but the traceback
    read like a crash rather than a stop.
    """
    sys.exit(0)


def veikkaus():
    register_exit_handler(sigterm_exit)

    sys.argv[0] = PACKAGE_NAME
    parser = argparse.ArgumentParser(description='Veikkaus bot')

    subparser = parser.add_subparsers(title='Commands', dest='command')

    parser_backfill = subparser.add_parser(
        'backfill', help='Crawl the race calendar into the raw archive (resumable)')
    parser_backfill.add_argument('--from', dest='start', required=True,
                                 help='First meet date to crawl (yyyy-mm-dd)')
    parser_backfill.add_argument('--to', dest='end', default=None,
                                 help='Last meet date to crawl (yyyy-mm-dd, default: today)')
    parser_backfill.add_argument('--country', default='FI', help='Card country filter (default: FI)')
    parser_backfill.add_argument('--raw', default=DEFAULT_RAW,
                                 help=f'Raw archive directory (default: {DEFAULT_RAW})')
    parser_backfill.add_argument('--db', default=DEFAULT_DB,
                                 help=f'DuckDB database holding the manifest (default: {DEFAULT_DB})')
    parser_backfill.add_argument('--delay', type=float, default=DEFAULT_DELAY,
                                 help=f'Base seconds between requests (default: {DEFAULT_DELAY})')
    parser_backfill.add_argument('--odds', action='store_true',
                                 help='Also crawl win-pool odds (roughly doubles the request count)')
    parser_backfill.add_argument('--limit', type=int, default=None,
                                 help='Stop after N fetches (for a trial run)')
    parser_backfill.add_argument('--retry-failed', action='store_true',
                                 help='Reset failed manifest rows to pending before crawling')
    parser_backfill.add_argument('--refetch-from', dest='refetch_start', default=None,
                                 help='Re-fetch this date even if already crawled '
                                      '(recovers a card crawled before its racing was final)')
    parser_backfill.add_argument('--refetch-to', dest='refetch_end', default=None,
                                 help='Last date of the re-fetch window '
                                      '(default: --refetch-from)')
    parser_backfill.add_argument('--create-db', action='store_true',
                                 help='Create the database if it is not there yet '
                                      '(otherwise a missing --db is an error)')
    parser_backfill.set_defaults(func=backfill)

    parser_heppa = subparser.add_parser(
        'heppa', help="Crawl Hippos's Heppa registry for the full finishing order (resumable)")
    parser_heppa.add_argument('--from', dest='start', required=True,
                              help='First meet date to crawl (yyyy-mm-dd)')
    parser_heppa.add_argument('--to', dest='end', default=None,
                              help='Last meet date to crawl (yyyy-mm-dd, default: today)')
    parser_heppa.add_argument('--raw', default=DEFAULT_RAW,
                              help=f'Raw archive directory (default: {DEFAULT_RAW})')
    parser_heppa.add_argument('--db', default=DEFAULT_DB,
                              help=f'DuckDB database holding the manifest (default: {DEFAULT_DB})')
    parser_heppa.add_argument('--delay', type=float, default=DEFAULT_DELAY,
                              help=f'Base seconds between requests (default: {DEFAULT_DELAY})')
    parser_heppa.add_argument('--limit', type=int, default=None,
                              help='Stop after N fetches (for a trial run)')
    parser_heppa.add_argument('--retry-failed', action='store_true',
                              help='Reset failed manifest rows to pending before crawling')
    parser_heppa.add_argument('--refetch-from', dest='refetch_start', default=None,
                              help='Re-fetch this date even if already crawled '
                                   '(recovers a meeting crawled before its results '
                                   'were published)')
    parser_heppa.add_argument('--refetch-to', dest='refetch_end', default=None,
                              help='Last date of the re-fetch window '
                                   '(default: --refetch-from)')
    parser_heppa.add_argument('--create-db', action='store_true',
                              help='Create the database if it is not there yet '
                                   '(otherwise a missing --db is an error)')
    parser_heppa.set_defaults(func=heppa_backfill)

    parser_hhorses = subparser.add_parser(
        'heppa-horses',
        help="Crawl the Heppa registry record of every horse the meetings turned up "
             "(run after `heppa` and `parse`)")
    parser_hhorses.add_argument('--raw', default=DEFAULT_RAW,
                                help=f'Raw archive directory (default: {DEFAULT_RAW})')
    parser_hhorses.add_argument('--db', default=DEFAULT_DB,
                                help=f'DuckDB database holding the manifest (default: {DEFAULT_DB})')
    parser_hhorses.add_argument('--delay', type=float, default=DEFAULT_DELAY,
                                help=f'Base seconds between requests (default: {DEFAULT_DELAY})')
    parser_hhorses.add_argument('--limit', type=int, default=None,
                                help='Stop after N fetches (for a trial run)')
    parser_hhorses.add_argument('--retry-failed', action='store_true',
                                help='Reset failed manifest rows to pending before crawling')
    # No --create-db: this crawl is driven by the horses already in the archive
    # (SELECT DISTINCT horseId FROM archive.heppa_start), so it has nothing to
    # do against a database it just made.
    parser_hhorses.set_defaults(func=heppa_horses)

    parser_hforeign = subparser.add_parser(
        'heppa-foreign',
        help="Crawl the meetings abroad the archive knows about, for the starts "
             "Finnish horses made there (run after `heppa` and `parse`)")
    parser_hforeign.add_argument('--raw', default=DEFAULT_RAW,
                                 help=f'Raw archive directory (default: {DEFAULT_RAW})')
    parser_hforeign.add_argument('--db', default=DEFAULT_DB,
                                 help=f'DuckDB database holding the manifest (default: {DEFAULT_DB})')
    parser_hforeign.add_argument('--delay', type=float, default=DEFAULT_DELAY,
                                 help=f'Base seconds between requests (default: {DEFAULT_DELAY})')
    parser_hforeign.add_argument('--limit', type=int, default=None,
                                 help='Stop after N fetches (for a trial run)')
    parser_hforeign.add_argument('--retry-failed', action='store_true',
                                 help='Reset failed manifest rows to pending before crawling')
    parser_hforeign.add_argument('--seeds', default=SEED_FILE,
                                 help='File of `date,trackCode` meetings to add by hand, '
                                      f'for the ones prev_start cannot name (default: {SEED_FILE})')
    parser_hforeign.add_argument('--refetch-from', dest='refetch_start', default=None,
                                 help='Reset already-fetched meetings from this date (yyyy-mm-dd) '
                                      'to pending — a seeded date whose results were not out yet '
                                      'is recorded as an empty meeting, not as a failure')
    parser_hforeign.add_argument('--refetch-to', dest='refetch_end', default=None,
                                 help='Last date of the refetch window (default: --refetch-from)')
    # No --create-db, for the same reason as heppa-horses: the meetings come from
    # archive.prev_start (heppa.FOREIGN_MEETINGS), because Heppa lists Finnish
    # events only and there is nothing else to discover them from.
    parser_hforeign.set_defaults(func=heppa_foreign)

    parser_hstats = subparser.add_parser(
        'heppa-stats',
        help="Crawl the registry's own career statistics per horse, which is how "
             "much of each career the archive is missing (run after `heppa` and "
             "`parse`)")
    parser_hstats.add_argument('--raw', default=DEFAULT_RAW,
                               help=f'Raw archive directory (default: {DEFAULT_RAW})')
    parser_hstats.add_argument('--db', default=DEFAULT_DB,
                               help=f'DuckDB database holding the manifest (default: {DEFAULT_DB})')
    parser_hstats.add_argument('--delay', type=float, default=DEFAULT_DELAY,
                               help=f'Base seconds between requests (default: {DEFAULT_DELAY})')
    parser_hstats.add_argument('--limit', type=int, default=None,
                               help='Stop after N fetches (for a trial run)')
    parser_hstats.add_argument('--retry-failed', action='store_true',
                               help='Reset failed manifest rows to pending before crawling')
    # No --create-db: driven by the horses already in the archive, as above.
    parser_hstats.set_defaults(func=heppa_stats)

    parser_parse = subparser.add_parser(
        'parse', help='Parse the raw archive into the archive.* tables')
    parser_parse.add_argument('--raw', default=DEFAULT_RAW,
                              help=f'Raw archive directory (default: {DEFAULT_RAW})')
    parser_parse.add_argument('--db', default=DEFAULT_DB,
                              help=f'DuckDB database file (default: {DEFAULT_DB})')
    parser_parse.add_argument('--country', default='FI', help='Card country filter (default: FI)')
    parser_parse.add_argument('--full', action='store_true',
                              help='Reload every archived payload, not just the ones fetched '
                                   'since the last parse. Needed after changing a parser')
    parser_parse.add_argument('--create-db', action='store_true',
                              help='Create the database if it is not there yet '
                                   '(otherwise a missing --db is an error)')
    parser_parse.set_defaults(func=parse)

    parser_status = subparser.add_parser('status', help='Show crawl manifest progress')
    parser_status.add_argument('--db', default=DEFAULT_DB,
                               help=f'DuckDB database file (default: {DEFAULT_DB})')
    parser_status.set_defaults(func=status)

    parser_crosscheck = subparser.add_parser(
        'crosscheck', help='Cross-validate the Veikkaus and Heppa halves of the archive')
    parser_crosscheck.add_argument('--db', default=DEFAULT_DB,
                                   help=f'DuckDB database file (default: {DEFAULT_DB})')
    parser_crosscheck.set_defaults(func=crosscheck)

    # `horse` is kept as an alias: it is what this subcommand was called when it
    # only counted horses, and it is in muscle memory and in the docs.
    parser_stats = subparser.add_parser(
        'stats', aliases=['horse'],
        help="Browse a horse's, trainer's or driver's registry starts in a terminal UI")
    parser_stats.add_argument('name', nargs='?', default=None,
                              help='Prefill the search box with this name')
    parser_stats.add_argument('--db', default=DEFAULT_DB,
                              help=f'DuckDB database file (default: {DEFAULT_DB})')
    # No --create-db: this only ever reads, on a read-only connection, so there
    # is nothing it could do with a database it just made.
    parser_stats.set_defaults(func=stats_tui)

    #parser_card = subparser.add_parser('card', help='Ravit')
    #parser_card.add_argument('-n', '--name', help='Radan nimi')
    #parser_card.add_argument('-a', '--abbreviation', help='Lyhenne')
    #parser_card.set_defaults(func=show_card)

    #parser_race = subparser.add_parser('race', help='Lähtö')
    #parser_race.add_argument('raceNumber', help='Lähtö')
    #parser_race.set_defaults(func=analysoi)

    args, _ = parser.parse_known_args()
    if not args.command:
        parser.print_help()
        sigterm_exit(None)

    require_db(args)

    try:
        args.func(args)
    except (KeyboardInterrupt, SystemExit):
        sigterm_exit(None)


if __name__ == '__main__':
    veikkaus()
