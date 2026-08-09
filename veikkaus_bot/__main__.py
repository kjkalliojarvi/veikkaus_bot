import argparse
import datetime
import signal
import sys

from .archive_db import DEFAULT_DB
from .crawler import backfill, status
from .fetcher import DEFAULT_DELAY
from .parse import parse

DEFAULT_RAW = 'data/raw'

PACKAGE_NAME = 'veikkaus_bot'
PVM = datetime.datetime.now().strftime("%d%m%Y")



def register_exit_handler(func):
    signal.signal(signal.SIGTERM, func)


def sigterm_exit(_sig_func=None):
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
    parser_backfill.set_defaults(func=backfill)

    parser_parse = subparser.add_parser(
        'parse', help='Parse the raw archive into the archive.* tables')
    parser_parse.add_argument('--raw', default=DEFAULT_RAW,
                              help=f'Raw archive directory (default: {DEFAULT_RAW})')
    parser_parse.add_argument('--db', default=DEFAULT_DB,
                              help=f'DuckDB database file (default: {DEFAULT_DB})')
    parser_parse.add_argument('--country', default='FI', help='Card country filter (default: FI)')
    parser_parse.set_defaults(func=parse)

    parser_status = subparser.add_parser('status', help='Show crawl manifest progress')
    parser_status.add_argument('--db', default=DEFAULT_DB,
                               help=f'DuckDB database file (default: {DEFAULT_DB})')
    parser_status.set_defaults(func=status)

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

    try:
        args.func(args)
    except (KeyboardInterrupt, SystemExit):
        sigterm_exit(None)


if __name__ == '__main__':
    veikkaus()
