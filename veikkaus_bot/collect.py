
from datetime import datetime
import json
from . import get_data, database


def collect_data(country: str):
    cards = get_data.get_cards()
    all_races = []
    all_runners = []
    all_starts = []
    for card in cards:
        if card.country == country:
            races = card.get_races()
            for race in races:
                all_races.append(race.race_record(card))
                runners = race.get_runners()
                for runner in runners:
                    all_runners.append(runner.runner_record())
                    all_starts +=  runner.prevstarts_record()
    return all_races, all_runners, all_starts


def save_data(races: list, runners: list, starts: list):
    data = {
        'races': races,
        'runners': runners,
        'starts': starts
    }
    timestamp = datetime.now()
    filename = f'{timestamp.year}-{timestamp.month}-{timestamp.day}-SE.json'
    with open(filename, 'w') as outfile:
        json.dump(data, outfile)


def store_data(jsonfile: str):
    with open(jsonfile, 'r') as openfile:
        json_object = json.load(openfile)
    database.store_races(json_object['races'])
    database.store_runners(json_object['runners'])
    database.store_starts(json_object['starts'])
