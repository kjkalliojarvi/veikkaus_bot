from collections import namedtuple
from datetime import datetime
import requests


headers = {'Content-type':'application/json', 'Accept':'application/json', 'X-ESA-API-Key':'ROBOT'}
metadata = namedtuple('metadata', ['vaihto', 'jako', 'lyhenne', 'pvm', 'peli'])
PVM = datetime.now().strftime("%y%m%d")
URL = "https://www.veikkaus.fi/api/toto-info/v1"
CARDS_TODAY_URL = f"{URL}/cards/today"
CARD_RACES_URL = f"{URL}/card/{{card_id}}/races"
CARD_POOLS_URL = f"{URL}/card/{{card_id}}/pools"
RACE_RUNNERS_URL = f"{URL}/race/{{race_id}}/runners"
POOL_ODDS_URL = f"{URL}/pool/{{pool_id}}/odds"


def fetch_cards_today() -> list[dict]:
    """Fetch today's toto race cards from the Veikkaus REST API."""
    response = requests.get(CARDS_TODAY_URL, headers=headers, timeout=10)
    response.raise_for_status()
    return response.json()["collection"]


def fetch_card_races(card_id: int) -> list[dict]:
    """Fetch all races for a card from the Veikkaus REST API."""
    response = requests.get(CARD_RACES_URL.format(card_id=card_id), headers=headers, timeout=10)
    response.raise_for_status()
    return response.json()["collection"]


def fetch_card_pools(card_id: int) -> list[dict]:
    """Fetch all pools for a card from the Veikkaus REST API."""
    response = requests.get(CARD_POOLS_URL.format(card_id=card_id), headers=headers, timeout=10)
    response.raise_for_status()
    return response.json()["collection"]


def fetch_race_odds(pool_id: int) -> dict:
    """Fetch odds for a pool from the Veikkaus REST API."""
    response = requests.get(POOL_ODDS_URL.format(pool_id=pool_id), headers=headers, timeout=10)
    response.raise_for_status()
    return response.json()


def get_odds(track_number: int, race_number: int, pool_type: str) -> tuple[metadata, list[dict]]:
    """Get odds for a specific race and pool type."""
    cards = fetch_cards_today()
    card = next((c for c in cards if c["trackNumber"] == track_number), None)
    if not card:
        raise ValueError(f"No card found for track number {track_number}")
    races = fetch_card_races(card["CardId"])
    race = next((r for r in races if r["raceNumber"] == race_number), None)
    if not race:
        raise ValueError(f"No race found for race number {race_number} on card {card['CardId']}")
    pools = fetch_card_pools(card["CardId"])
    pool = next((p for p in pools if p["poolType"] == pool_type and p["firstRaceId"] == race["raceId"]), None)
    if not pool:
        raise ValueError(f"No pool found for pool type {pool_type} on card {card['CardId']}")
    odds = fetch_race_odds(pool["poolId"])
    meta = metadata(
        vaihto=odds.get("netSales"),
        jako=odds.get("netPool"),
        lyhenne=odds.get(card["trackAbbreviation"]),
        pvm=PVM,
        peli=pool_type
    )
    return meta, odds.get("collection", [])