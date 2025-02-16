import argparse
import datetime
import signal
import sys

from .get_data import fi_se

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

    parser_cards = subparser.add_parser('fi_se')
    parser_cards.set_defaults(func=fi_se)

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
