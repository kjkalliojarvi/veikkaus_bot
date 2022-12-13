
from collections import namedtuple
import requests
import json

from .classes import Card, Race, Runner, Pool


Ids = namedtuple('Ids', 'card_id race_id')
RaceCards = namedtuple('RaceCards', 'trackname abbreviation country card_id')
headers = {'Content-type':'application/json', 'Accept':'application/json', 'X-ESA-API-Key':'ROBOT'}

URL = 'https://www.veikkaus.fi/api/toto-info/v1'


def get_cards() -> list:
    resp = requests.get(f'{URL}/cards/today', headers=headers)
    return json.loads(resp.text)['collection']


def get_racecards() -> list:
    cards = get_cards()
    racecards = []
    for card in cards:
        racecards.append(RaceCards(trackname=card['trackName'], abbreviation=card['trackAbbreviation'],
                                   country=card['country'], card_id=card['cardId']))
    return racecards


def get_card(trackname: str='', abbreviation:str='') -> Card:
    cards = get_cards()
    for card in cards:
        if trackname:
            if card['trackName'] == trackname:
                return Card(**card)
        elif abbreviation:
            if card['trackAbbreviation'] == abbreviation:
                return Card(**card)


def get_card_id(trackname: str) -> int:
    card = get_card(trackname)
    try:
        return card.cardId
    except AttributeError:
        print(f'Unknown trackname: {trackname}')


def get_races(trackname: str) -> list:
    card_id = get_card_id(trackname)
    if card_id:
        resp = requests.get(f'{URL}/card/{card_id}/races', headers=headers)
        return json.loads(resp.text)['collection']
    else:
        return []


def get_race(trackname: str, racenumber: int) -> Race:
    races = get_races(trackname)
    for race in races:
        if race['number'] == racenumber:
            return Race(**race)


def get_ids(trackname: str, racenumber: int) -> Ids:
    race = get_race(trackname, racenumber)
    if race:
        return Ids(card_id=race.cardId, race_id=race.raceId)


def get_card_pools(trackname: str) -> list:
    card_id = get_card_id(trackname)
    if card_id:
        resp = requests.get(f'{URL}/card/{card_id}/pools', headers=headers)
        return json.loads(resp.text)['collection']


def get_race_pools(trackname: str, racenumber: int) -> list:
    ids = get_ids(trackname, racenumber)
    if ids:
        resp = requests.get(f'{URL}/race/{ids.race_id}/pools', headers=headers)
        return json.loads(resp.text)['collection']


def get_runners(trackname: str, racenumber: int) -> list:
    ids = get_ids(trackname, racenumber)
    if ids:
        resp = requests.get(f'{URL}/race/{ids.race_id}/runners', headers=headers)
        return json.loads(resp.text)['collection']


