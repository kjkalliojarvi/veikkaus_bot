import pytest

from veikkaus_bot.models import HeppaHorse, HeppaStart, Runner
from veikkaus_bot.parse import (_captured_at, base_horse_key, betpercentage_records,
                                blank_to_none, heppa_horse_record,
                                heppa_start_record, horse_key, is_placeholder,
                                normalize_name, parse_heppa_auto_start,
                                parse_heppa_int, parse_heppa_km_time,
                                parse_heppa_odds, parse_km_time, parse_meet_date,
                                parse_placing, parse_result, parse_tote_result,
                                parse_win_odd, prevstart_records, stat_records,
                                strip_import_markers)


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


@pytest.mark.parametrize('driver_name', ['Poissa', '- -', 'Poissa Poissa', ''])
def test_is_placeholder_spots_a_vacated_start_number(driver_name):
    """`driverName` varies across them, so the test cannot lean on it."""
    runner = Runner(runnerId=1054821980, raceId=2, horseName='Poissa',
                    startNumber=11, scratched=True, prize=0, horseAge=16,
                    gender='UNKNOWN', coachNameInitials='', ownerName='',
                    specialCart='UNKNOWN', driverName=driver_name)
    assert is_placeholder(runner)


def test_is_placeholder_leaves_real_runners_alone():
    """A real runner always names a trainer, however thin the payload."""
    assert not is_placeholder(make_runner())
    assert not is_placeholder(make_runner(scratched=True))


def test_captured_at_prefers_the_payloads_own_timestamp():
    assert _captured_at({'updated': 1623770218000}, 1623769620000) == 1623770218000


def test_captured_at_falls_back_to_the_race_start_time():
    """Old payloads drop `updated` and `updatedString` together, but capturedAt
    is in the primary key and cannot be NULL."""
    assert _captured_at({'poolId': 153443, 'odds': []}, 1118826000000) == 1118826000000


def test_captured_at_uses_an_explicit_sentinel_when_nothing_is_known():
    assert _captured_at({'poolId': 153443}, None) == 0


def test_captured_at_treats_an_explicit_null_like_a_missing_field():
    assert _captured_at({'updated': None}, 1118826000000) == 1118826000000


# --- Heppa ------------------------------------------------------------------

@pytest.mark.parametrize('text, expected', [
    ('2080', 2080), ('0', 0),
    ('-5', -5),                        # temperature goes negative in winter
    ('-', None), ('', None), (None, None), ('1,5', None), ('4.44', None),
])
def test_parse_heppa_int(text, expected):
    assert parse_heppa_int(text) == expected


@pytest.mark.parametrize('code, placement', [
    ('1', 1), ('7', 7), ('13', 13),   # the whole field, not just the paid places
    ('0', None),                      # absent, or disqualified under hpl/hll/hrp/k
    # 100 + the position a disqualified horse crossed the line in. It holds no
    # placing — the same treatment archive.prev_start gives 'hlo4'.
    ('105', None), ('108', None), ('110', None),
    ('', None), (None, None), ('-', None),
])
def test_parse_placing(code, placement):
    assert parse_placing(code) == placement


@pytest.mark.parametrize('text, ms', [
    ('1.18.8', 78800), ('1.29.0', 89000), ('1.41.1', 101100),
    ('2.05.0', 125000),
    (None, None), ('', None), ('-', None), ('18,8', None), ('1.18', None),
])
def test_parse_heppa_km_time(text, ms):
    assert parse_heppa_km_time(text) == ms


@pytest.mark.parametrize('long_form, short_form', [
    ('1.18.8', '18,8'), ('1.19.8', '19,8'), ('1.29.0', '29,0'), ('2.05.0', '2.05,0'),
])
def test_the_two_km_time_forms_agree(long_form, short_form):
    """Heppa sends both. The short one is byte-identical to the notation
    archive.start already uses, so the record builder prefers it and falls back
    to the long form — which only works if they mean the same thing."""
    assert parse_heppa_km_time(long_form) == parse_km_time(short_form)[0]


@pytest.mark.parametrize('text, hundredths', [
    ('4.44', 444), ('19.71', 1971), ('56.51', 5651), ('3.58', 358), ('69.07', 6907),
    ('100', 10000),
    (None, None), ('', None), ('-', None),
])
def test_parse_heppa_odds(text, hundredths):
    """Heppa sends a decimal; the archive stores hundredths everywhere else."""
    assert parse_heppa_odds(text) == hundredths


@pytest.mark.parametrize('code, auto', [
    ('ake', True), ('aly', True), ('akp', True),
    ('ke', False), ('ly', False), ('kp', False),
    (None, None), ('', None),
])
def test_parse_heppa_auto_start(code, auto):
    """The 'a' prefix on distanceCode is the auto-start signal — not the race's
    startForm, which is handicap-versus-group and a different axis entirely."""
    assert parse_heppa_auto_start(code) == auto


# The Savonlinna 2026-08-08 race 1 winner, verbatim from the API. Its Veikkaus
# counterpart carries placement 1, kmTime '18,8' and winOddsFinal 444, and
# careerWinnings 142000 (cents, pre-race) against horsePriceSum 2120 (euros,
# post-race) — 1420 + the 700 won that day.
BOOMER = {'date': '2026-08-08', 'trackCode': 'SN', 'startNumber': '1',
          'programNumber': '9', 'lane': '1', 'distance': '2080', 'placing': '1',
          'totalTime': '2.43.8', 'kilometerTime': '1.18.8',
          'shortKilometerTime': '18,8', 'price': '700', 'winOdds': '4.44',
          'horseId': '7913507947789197818', 'horseName': "Boomer's Revenge",
          'trainerId': '828320150867768153', 'trainerName': 'Pasi Vaittinen',
          'gallop': False, 'absent': False, 'shoesFront': 'K', 'shoesBack': 'K',
          'driverId': '828320150867768153', 'driverName': 'P Vaittinen',
          'horsePriceSum': '2120', 'distanceCode': 'ke'}


def heppa_field(record):
    """Read a heppa_start record back by column name."""
    names = ('meetDate trackCode raceNumber programNumber horseKey horseId horseName '
             'horseBreed horseRegistrationCountry startTrack distance distanceCode '
             'placingRaw placement disqualifiedCode gallop absent kmTime kmTimeMs '
             'autoStart totalTime prizeWon winOdd horsePriceSum').split()
    return dict(zip(names, record))


def test_heppa_start_record_reads_the_race_and_horse_numbers_the_right_way_round():
    """Heppa's `startNumber` is the race number and `programNumber` is the
    horse's start number — the exact inverse of the Veikkaus vocabulary."""
    row = heppa_field(heppa_start_record(HeppaStart(**BOOMER)))
    assert row['raceNumber'] == 1
    assert row['programNumber'] == 9


def test_heppa_start_record_converts_the_scalars():
    row = heppa_field(heppa_start_record(HeppaStart(**BOOMER)))
    assert row['placement'] == 1
    assert row['kmTime'] == '18,8'
    assert row['kmTimeMs'] == 78800
    assert row['winOdd'] == 444          # '4.44' -> hundredths
    assert row['prizeWon'] == 700        # this race's purse, not career earnings
    assert row['horsePriceSum'] == 2120
    assert row['autoStart'] is False     # 'ke' has no 'a' prefix
    assert row['horseId'] == '7913507947789197818'   # stays a string


def test_heppa_start_record_falls_back_to_the_long_km_time():
    row = heppa_field(heppa_start_record(
        HeppaStart(**(BOOMER | {'shortKilometerTime': None}))))
    assert row['kmTimeMs'] == 78800
    assert row['kmTime'] is None


def test_heppa_start_record_keeps_a_disqualification_out_of_placement():
    """'108' is 8th across the line, disqualified. Putting 8 in `placement`
    would give the race two 8th places once the field is renumbered."""
    row = heppa_field(heppa_start_record(HeppaStart(
        **(BOOMER | {'placing': '108', 'disqualifiedCode': 'hlo', 'gallop': True}))))
    assert row['placement'] is None
    assert row['placingRaw'] == '108'    # the position survives verbatim
    assert row['disqualifiedCode'] == 'hlo'
    assert row['gallop'] is True


def test_heppa_start_record_leaves_a_scratched_horse_unplaced():
    row = heppa_field(heppa_start_record(HeppaStart(
        **(BOOMER | {'placing': '0', 'absent': True, 'price': '0',
                     'kilometerTime': None, 'shortKilometerTime': None}))))
    assert row['placement'] is None
    assert row['absent'] is True
    assert row['kmTimeMs'] is None


def test_heppa_start_record_leaves_horse_key_to_the_recompute():
    """A Heppa start carries no birth year, so horse_key() cannot be built from
    one — identity comes back through the registry id instead."""
    assert heppa_field(heppa_start_record(HeppaStart(**BOOMER)))['horseKey'] is None


def test_heppa_start_record_survives_a_payload_with_only_the_identifying_fields():
    """Every optional field is optional on purpose: a validation error would
    cost the whole start, which is the row that fills a hole in archive.start."""
    row = heppa_field(heppa_start_record(HeppaStart(
        date='2005-06-15', trackCode='S', startNumber='3', programNumber='4')))
    assert (row['meetDate'], row['trackCode'], row['raceNumber'],
            row['programNumber']) == ('2005-06-15', 'S', 3, 4)
    assert row['placement'] is None


# --- import markers and the fallback identity --------------------------------
#
# All of these are real names from the archive. The trailing token is a country
# marker only when it agrees with the tag; otherwise it is a stable suffix and
# part of the name.

@pytest.mark.parametrize('name, expected', [
    # Country letter agreeing with the tag — an import marker, dropped.
    ('Morell S (SE)', 'Morell'),
    ('Frozen Elsa S (SE)', 'Frozen Elsa'),
    ('Touch the Clouds S (SE)', 'Touch the Clouds'),
    ('Black Swan N (NO)', 'Black Swan'),
    ('Harley D (DE)', 'Harley'),
    ('Hurriganes DK (DK)', 'Hurriganes'),
    ('Twentyfourseven DE* (DE)', 'Twentyfourseven'),
    ('The Next One US (US)', 'The Next One'),
    ('Humble Stance FR* (FR)', 'Humble Stance'),
    # Tag alone, nothing to drop beyond it.
    ('Humble Stance* (FR)', 'Humble Stance'),
    ('Kapplans Orlando (SE)', 'Kapplans Orlando'),
    # Stable suffixes: they disagree with the tag, so they stay.
    ('Birbone OK (IT)', 'Birbone OK'),
    ('Vulcano OP (IT)', 'Vulcano OP'),
    ('Giovy LJ (IT)', 'Giovy LJ'),
    ('Tako ÖK* (NO)', 'Tako ÖK'),
    ('A RM (RU)', 'A RM'),
    ('Let it be VP* (NL)', 'Let it be VP'),
    ("Aurelia's Pearl KS* (FI)", "Aurelia's Pearl KS"),
    ('Isola L C (DK)', 'Isola L C'),
    # 'S' against a US tag is not a country marker for that tag.
    ('Sweet Game S (US)', 'Sweet Game S'),
    # No tag to agree with, so nothing is stripped.
    ('Remington XO', 'Remington XO'),
    ('Pompom S*', 'Pompom S*'),
    ('Gripen S', 'Gripen S'),
    ('Lumi-Urho', 'Lumi-Urho'),
])
def test_strip_import_markers(name, expected):
    assert strip_import_markers(name) == expected


def test_base_horse_key_folds_the_ways_veikkaus_writes_one_import():
    """'Humble Stance' arrives three ways across cards; they are one horse."""
    keys = {base_horse_key(n, 2017) for n in
            ('Humble Stance*', 'Humble Stance* (FR)', 'Humble Stance FR* (FR)')}
    assert len(keys) == 1


def test_base_horse_key_still_separates_birth_years():
    assert base_horse_key('Consta', 2019) != base_horse_key('Consta', 2021)


def test_base_horse_key_does_not_merge_on_a_stable_suffix():
    """'Birbone OK' and a hypothetical 'Birbone' are different horses."""
    assert base_horse_key('Birbone OK (IT)', 2017) != base_horse_key('Birbone (IT)', 2017)


def test_base_horse_key_loses_the_origin_that_horse_key_keeps():
    """The documented cost of the fallback: 'Elliot' and 'Elliot (DK)', both
    foaled 2016, are two real horses and this cannot tell them apart. Only the
    registry id can, which is why RECOMPUTE_HORSE_IDENTITY prefers it."""
    assert horse_key('Elliot', 2016) != horse_key('Elliot (DK)', 2016)
    assert base_horse_key('Elliot', 2016) == base_horse_key('Elliot (DK)', 2016)


# --- the registry record ------------------------------------------------------

BOOMER_HORSE = {'id': '7913507947789197818', 'name': "Boomer's Revenge",
                'birthDate': '2021-06-14', 'birthDateAccurate': True,
                'registerNo': '246001L00211779', 'ueln': '246001L00211779',
                'chipNo': '246000026217495', 'chipNo2': '-', 'dead': False,
                'species': 'L', 'breedFinName': 'Amerikkalainen', 'breedCode': 'A',
                'gender': 'O', 'color': 'punaruunikko', 'registrationSuspended': False,
                'birthCountry': 'FI', 'birthCountryName': 'Suomi', 'birthPlace': 'Joensuu',
                'origin': 'FI', 'breedingUnion': 'Pohjois-Karjalan Hjl',
                'registrationCountry': 'FI', 'ownerName': 'Cathill Stable',
                'breederName': 'Cathill Stable', 'groomName': 'Erika Vaittinen',
                'racingRenterName': '-', 'trainerId': '828320150867768153',
                'trainerName': 'Pasi Vaittinen', 'homeTrackName': 'Linnunlahti',
                'homeTrackCity': 'Joensuu', 'bestRecord': '18,0ke', 'age': '5',
                'sire': {'id': '6600639144951126845', 'name': 'Zola Boko*',
                         'registerNo': 'S-06-3122', 'laboratoryNumber': '-'},
                'dam': {'id': '4125784394378719727', 'name': 'Beautifly',
                        'registerNo': '246001L00141755'}}

HORSE_FIELDS = ('horseId horseName birthDate birthDateAccurate registerNo ueln chipNo dead '
                'registrationSuspended species breedCode breedFinName gender color '
                'birthCountry birthCountryName birthPlace origin registrationCountry '
                'breedingUnion breederName ownerName trainerId trainerName homeTrackName '
                'homeTrackCity bestRecord sireId sireName sireRegisterNo damId damName '
                'damRegisterNo').split()


def horse_field(record):
    return dict(zip(HORSE_FIELDS, record))


@pytest.mark.parametrize('text, expected', [
    ('-', None),          # Heppa's placeholder for an absent value
    ('', None), (None, None), ('  ', None),
    ('Cathill Stable', 'Cathill Stable'),
    ('A-1', 'A-1'),       # a hyphen inside a real value is not a placeholder
])
def test_blank_to_none(text, expected):
    assert blank_to_none(text) == expected


def test_heppa_horse_record_carries_the_identifiers_veikkaus_lacks():
    row = horse_field(heppa_horse_record(HeppaHorse(**BOOMER_HORSE)))
    assert row['horseId'] == '7913507947789197818'
    assert row['registerNo'] == '246001L00211779'
    assert row['ueln'] == '246001L00211779'      # international; the cross-registry key
    assert row['birthDate'] == '2021-06-14'      # exact, where archive.horse has a year


def test_heppa_horse_record_separates_origin_from_where_it_races():
    """birthCountry is what the country tag in a Veikkaus name gestures at;
    registrationCountry reads FI for any import."""
    row = horse_field(heppa_horse_record(HeppaHorse(**BOOMER_HORSE)))
    assert (row['birthCountry'], row['registrationCountry']) == ('FI', 'FI')
    imported = horse_field(heppa_horse_record(HeppaHorse(
        **(BOOMER_HORSE | {'birthCountry': 'SE', 'registrationCountry': 'FI'}))))
    assert (imported['birthCountry'], imported['registrationCountry']) == ('SE', 'FI')


def test_heppa_horse_record_flattens_the_parents():
    row = horse_field(heppa_horse_record(HeppaHorse(**BOOMER_HORSE)))
    assert (row['sireId'], row['sireName']) == ('6600639144951126845', 'Zola Boko*')
    assert (row['damId'], row['damRegisterNo']) == ('4125784394378719727', '246001L00141755')


def test_heppa_horse_record_drops_the_dash_placeholders():
    """A horse with no known breeder is not a horse bred by '-'."""
    row = horse_field(heppa_horse_record(HeppaHorse(
        **(BOOMER_HORSE | {'breederName': '-', 'birthPlace': '-', 'bestRecord': '-'}))))
    assert row['breederName'] is None
    assert row['birthPlace'] is None
    assert row['bestRecord'] is None


def test_heppa_horse_record_survives_a_payload_with_only_an_id():
    """Only `id` is required — a validation error would cost the whole horse."""
    row = horse_field(heppa_horse_record(HeppaHorse(id='123')))
    assert row['horseId'] == '123'
    assert row['sireId'] is None and row['ueln'] is None
