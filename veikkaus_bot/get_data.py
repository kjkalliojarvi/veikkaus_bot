
from collections import namedtuple
import requests
import json

from .classes import Card, Race, Runner, Pool, PrevStart, Stat, Stats, PoolTypes


Id = namedtuple('Id', 'card race')
RaceCard = namedtuple('RaceCard', 'trackname abbreviation country id')

headers = {'Content-type':'application/json', 'Accept':'application/json', 'X-ESA-API-Key':'ROBOT'}

URL = 'https://www.veikkaus.fi/api/toto-info/v1'


def _get_collection(url: str) -> dict:
    resp = requests.get(f'{URL}{url}', headers=headers)
    return json.loads(resp.text)['collection']


def _get_dict(url: str) -> dict:
    resp = requests.get(f'{URL}{url}', headers=headers)
    return json.loads(resp.text)

def get_cards() -> list:
    return _get_collection('/cards/today')

def get_racecards() -> list:
    cards = get_cards()
    racecards = []
    for card in cards:
        racecards.append(RaceCard(trackname=card['trackName'], abbreviation=card['trackAbbreviation'],
                                   country=card['country'], id=card['cardId']))
    return racecards


def get_card(abbreviation: str) -> Card:
    cards = get_cards()
    for card in cards:
        if card['trackAbbreviation'] == abbreviation:
            return Card(**card)


def get_races(abbreviation: str) -> list:
    card = get_card(abbreviation)
    if card:
        return _get_collection(f'/card/{card.cardId}/races')


def get_race(abbreviation: str, racenumber: int) -> Race:
    races = get_races(abbreviation)
    for race in races:
        if race['number'] == racenumber:
            return Race(**race)


def get_card_pools(abbreviation: str) -> list:
    card = get_card(abbreviation)
    if card:
        return _get_collection(f'/card/{card.cardId}/pools')


def get_race_pools(abbreviation: str, racenumber: int) -> list:
    race = get_race(abbreviation, racenumber)
    if race:
        return _get_collection(f'/race/{race.raceId}/pools')


def get_pool(abbreviation: str, racenumber: int, pooltype: PoolTypes) -> Pool:
    pools = get_race_pools(abbreviation, racenumber)
    for pool in pools:
        if pool['poolType'] == pooltype:
            return Pool(**pool)


def get_odds(abbreviation: str, racenumber: int, pooltype: PoolTypes) -> dict:
    pool = get_pool(abbreviation, racenumber, pooltype)
    return _get_dict(f'/pool/{pool.poolId}/odds')


def get_runners(abbreviation: str, racenumber: int) -> list:
    race = get_race(abbreviation, racenumber)
    if race:
        return _get_collection(f'/race/{race.raceId}/runners')


def get_runner(abbreviation: str, racenumber: int, startnumber: int) -> Runner:
    runners = get_runners(abbreviation, racenumber)
    for runner in runners:
        if runner['startNumber'] == startnumber:
            return Runner(**runner)


def get_previous_starts(runner: Runner) -> list:
    for start in runner.prevStarts:
        print(PrevStart(**start))
    print(Stat(**runner.stats['currentYear']))
    print(Stat(**runner.stats['previousYear']))
    print(Stat(**runner.stats['total']))
