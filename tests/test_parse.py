import pytest

from veikkaus_bot.models import Runner
from veikkaus_bot.parse import (betpercentage_records, horse_key, normalize_name,
                                parse_km_time, parse_meet_date, parse_result,
                                parse_tote_result, parse_win_odd,
                                prevstart_records, stat_records)


def make_runner(**overrides):
    """A runner with only the fields the API always sends."""
    fields = dict(runnerId=1, raceId=2, horseName='Lumi-Urho', startNumber=1,
                  startTrack=1, distance=2100, scratched=False, prize=0,
                  frontShoes='HAS_SHOES', rearShoes='NO_SHOES',
                  frontShoesChanged=False, rearShoesChanged=False, horseAge=7,
                  gender='RUUNA', coachName='T. Korpela', coachNameInitials='T',
                  ownerName='Korpela', specialCart='NO')
    return Runner(**(fields | overrides))


@pytest.mark.parametrize('text, ms, auto', [
    # The leading minute is dropped by convention: 24,9 is 1.24,9.
    ('24,9a', 84900, True),
    ('24,9', 84900, False),
    ('33,4', 93400, False),
    ('13,4a', 73400, True),
    # Equipment/track markers are not auto-start markers.
    ('40,2ke', 100200, False),
    # Slower times spell the minute out.
    ('2.05,0', 125000, False),
    ('1.13,4a', 73400, True),
    # Monté times take a leading 'm'.
    ('m19,6', 79600, False),
    ('m21,0', 81000, False),
])
def test_parse_km_time(text, ms, auto):
    assert parse_km_time(text) == (ms, auto)


@pytest.mark.parametrize('text', [None, '', 'hyl', 'kesk', '-', '24.9', 'x,y'])
def test_parse_km_time_rejects_non_times(text):
    assert parse_km_time(text) == (None, False)


def test_parsed_km_times_are_plausible():
    """Strategy §8: km times should land between 1:08 and 1:50."""
    for text in ('08,0', '24,9a', '50,0'):
        ms, _ = parse_km_time(text)
        assert 68000 <= ms <= 110000


@pytest.mark.parametrize('text, expected', [
    ('6-3-8', [6, 3, 8]),
    ('6-5-2', [6, 5, 2]),
    ('5-11-13', [5, 11, 13]),
    ('1-2', [1, 2]),          # fewer than three finishers
    (None, []),
    ('', []),
])
def test_parse_tote_result(text, expected):
    assert parse_tote_result(text) == expected


def test_normalize_name_folds_case_and_spacing():
    assert normalize_name('  Lumi-Urho  ') == 'lumi-urho'
    assert normalize_name('Let\'s Take  a Selfie') == "let's take a selfie"


def test_normalize_name_drops_the_asterisk_but_keeps_the_country_tag():
    """The tag is what separates an import from a same-named domestic horse."""
    assert normalize_name('Lerin* (SE)') == 'lerin (se)'
    assert normalize_name('Lerin') != normalize_name('Lerin* (SE)')


def test_horse_key_separates_same_name_different_year():
    assert horse_key('Vieskerin Vinha', 2014) != horse_key('Vieskerin Vinha', 2015)
    assert horse_key('Vieskerin Vinha', 2014) == horse_key('vieskerin  vinha', 2014)


STATS = {'currentYear': {'year': '2026', 'record2': '33,4ke', 'starts': 4,
                         'position1': 2, 'position2': 0, 'position3': 0,
                         'places': 0, 'winMoney': 180000, 'winningPercent': 50},
         'total': {'year': '', 'starts': 12, 'position1': 3, 'position2': 1,
                   'position3': 0, 'places': 1, 'winMoney': 200000}}


def test_stat_records_one_row_per_period_present():
    records = stat_records(make_runner(stats=STATS))
    assert [r[1] for r in records] == ['currentYear', 'total']
    assert records[0][:6] == (1, 'currentYear', '2026', None, '33,4ke', 4)


def test_stat_records_empty_for_historical_runners():
    """Historical runners payloads carry no stats block at all."""
    assert stat_records(make_runner()) == []


def test_betpercentage_records_one_row_per_pool_type():
    runner = make_runner(betPercentages={'KAK': {'percentage': 939}, 'T5': {'percentage': 12}})
    assert sorted(betpercentage_records(runner)) == [(1, 'KAK', 939), (1, 'T5', 12)]


def test_betpercentage_records_empty_when_absent():
    assert betpercentage_records(make_runner()) == []


@pytest.mark.parametrize('code, placement', [
    ('1', 1), ('3', 3), ('14', 14),   # placings run well past the paid places
    ('kl', None),                     # koelähtö — a qualifying start
    ('k', None),                      # keskeytti — did not finish
    ('hpl', None), ('hll', None), ('hlo4', None),   # hylätty — disqualified
    ('', None), (None, None),
])
def test_parse_result(code, placement):
    assert parse_result(code) == placement


def test_parse_meet_date_uses_the_local_meet_day():
    """The sibling `meetDate` is midnight Finnish time in UTC, so its date
    component is a day early — shortMeetDate is the one that matches a card."""
    assert parse_meet_date('22.12.24') == '2024-12-22'
    assert parse_meet_date('31.05.25') == '2025-05-31'


@pytest.mark.parametrize('text', [None, '', 'x', '12.5'])
def test_parse_meet_date_rejects_junk(text):
    assert parse_meet_date(text) is None


@pytest.mark.parametrize('text, expected', [
    ('1002', 1002), ('100', 100), (None, None), ('', None), ('-', None), ('1,5', None),
])
def test_parse_win_odd(text, expected):
    assert parse_win_odd(text) == expected


PREV_START = {'priorStartId': 2582032773, 'distance': 2140, 'driver': 'P Eskelinen',
              'meetDate': '2024-12-21T22:01:00.000+00:00', 'raceNumber': 21,
              'shortMeetDate': '22.12.24', 'startTrack': 4, 'result': 'kl',
              'trackCode': 'Ku', 'kmTime': '-', 'firstPrize': 0}


def test_prevstart_records_carry_the_horse_not_the_reporting_runner():
    """Keyed on the start itself, so re-reports across later races collapse."""
    runner = make_runner(birthDate='2019-01-01', prevStarts=[PREV_START])
    (record,) = prevstart_records(runner)
    assert record[0] == 2582032773
    assert record[1] == horse_key('Lumi-Urho', 2019)
    assert record[2] == '2024-12-22'                  # local meet date
    assert record[3] == '2024-12-21T22:01:00.000+00:00'   # raw timestamp kept


def test_prevstart_records_keep_the_raw_code_and_drop_the_placement():
    runner = make_runner(prevStarts=[PREV_START])
    (record,) = prevstart_records(runner)
    assert record[12] == 'kl'      # result, verbatim
    assert record[13] is None      # placement — a qualifying start has none
    assert record[15] is None      # kmTimeMs — '-' is not a time


def test_prevstart_records_parse_a_finished_start():
    runner = make_runner(prevStarts=[PREV_START | {'result': '4', 'kmTime': '24,9a',
                                                   'winOdd': '1002'}])
    (record,) = prevstart_records(runner)
    assert (record[12], record[13]) == ('4', 4)
    assert (record[14], record[15], record[16]) == ('24,9a', 84900, True)
    assert record[17] == 1002


def test_prevstart_records_empty_for_historical_runners():
    assert prevstart_records(make_runner()) == []
