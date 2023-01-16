from .get_data import get_racecards, get_card


def show_cards(args):
    cards = get_racecards()
    for card in cards:
        if not args.country or args.country == card.country:
            print(card)


def show_card(args):
    card = get_card(args.name, args.abbreviation)
    print(card)