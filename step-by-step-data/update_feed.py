from __future__ import annotations

import csv
import io
import json
import re
import sys
import time
import unicodedata
from difflib import SequenceMatcher
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.support.ui import WebDriverWait

ROME = ZoneInfo('Europe/Rome')
OUT_DIR = Path(__file__).resolve().parent
MONTHS = {1:'gen',2:'feb',3:'mar',4:'apr',5:'mag',6:'giu',7:'lug',8:'ago',9:'set',10:'ott',11:'nov',12:'dic'}

BETFLAG_API = 'https://sportservice.betflag.it'
BETFLAG_HEADERS = {
    'x-api-version':'1.0',
    'X-Auth-Token':'',
    'X-Brand':'3',
    'X-IdCanale':'1',
    'User-Agent':'Mozilla/5.0 StepByStep/1.0',
    'Accept':'application/json,text/plain,*/*',
}


MULTI_SOURCE_BOARD_V6 = [
    {
        'id':'predictz',
        'weight':1.00,
        'kind':'predictz',
        'urls':[
            'https://www.predictz.com/predictions/',
            'https://r.jina.ai/http://www.predictz.com/predictions/',
        ],
    },
    {
        'id':'windrawwin',
        'weight':0.96,
        'kind':'windrawwin',
        'urls':[
            'https://www.windrawwin.com/',
            'https://r.jina.ai/http://www.windrawwin.com/',
        ],
    },
    {
        'id':'forebet',
        'weight':1.02,
        'kind':'forebet',
        'urls':[
            'https://www.forebet.com/en',
            'https://r.jina.ai/http://www.forebet.com/en',
        ],
    },
    {
        'id':'forebetdc',
        'weight':1.04,
        'kind':'forebetdc',
        'urls':[
            'https://www.forebet.com/en/football-tips-and-predictions-for-today/double-chance-predictions',
            'https://r.jina.ai/http://www.forebet.com/en/football-tips-and-predictions-for-today/double-chance-predictions',
        ],
    },
    {
        'id':'vitibet',
        'weight':0.98,
        'kind':'vitibet',
        'urls':[
            'https://www.vitibet.com/index.php?clanek=quicktips&lang=en&sekce=fotbal',
            'https://r.jina.ai/http://www.vitibet.com/index.php?clanek=quicktips&lang=en&sekce=fotbal',
        ],
    },
]

def clean(s: str) -> str:
    return re.sub(r'\s+', ' ', str(s or '').replace('\xa0',' ')).strip()

def parse_time_text(value: str, day: str) -> str | None:
    m = re.search(r'\b(\d{1,2}:\d{2})(?:\s+(\d{1,2})\s+([A-Za-zÀ-ÿ]{3}))?', clean(value), re.I)
    if not m:
        return None
    if m.group(2) and m.group(3):
        _, mo, d = map(int, day.split('-'))
        if int(m.group(2)) != d or m.group(3).lower()[:3] != MONTHS[mo]:
            return None
    return m.group(1)

def kickoff_iso(day: str, hhmm: str) -> str:
    y,m,d = map(int, day.split('-'))
    hh,mm = map(int, hhmm.split(':'))
    return datetime(y,m,d,hh,mm,tzinfo=ROME).isoformat()

def first_odd(value):
    txt = str(value or '').replace('▲',' ').replace('▼',' ')
    if '%' in txt or '—' in txt:
        return None
    m = re.search(r'(?<!\d)(\d{1,3}(?:[.,]\d{1,3})?)(?!\d)', txt)
    if not m:
        return None
    try:
        n = float(m.group(1).replace(',','.'))
    except Exception:
        return None
    return n if 1.0 < n < 100 else None

def add_record(out, base, market, outcome, value, confidence):
    n = first_odd(value)
    if n is None:
        return
    raw_value=str(value or '')
    pm=re.search(r'__PROVIDER__=([^\n]+)',raw_value)
    mm=re.search(r'__MODAL__=([^\n]+)',raw_value)
    provider=clean(pm.group(1)) if pm else ''
    modal_url=clean(mm.group(1)) if mm else ''
    is_betflag=provider.lower() == 'betflag'
    out.append({
        'home_team': base['home'],
        'away_team': base['away'],
        'event_time': base['kickoff'],
        'event_id': base['id'],
        'league_name': base['league'],
        'country': base['country'],
        'market_name': market,
        'selection_column': outcome,
        'odd': round(n,3),
        '_marketConfidence': confidence,
        '_serverFeed': True,
        '_bookmaker': provider or None,
        '_modalUrl': modal_url or None,
        '_betflagCell': is_betflag,
    })

def split_region(line: str):
    value = clean(line)
    if ' - ' in value:
        country, league = value.split(' - ',1)
        return country.strip() or '—', league.strip() or '—'
    return '—', value or '—'

def split_teams(line: str):
    value = clean(line)
    if ' - ' not in value:
        return None
    home, away = value.split(' - ',1)
    home,away = home.strip(),away.strip()
    return (home,away) if home and away else None

LEAGUE_PREFIXES = sorted([
    'National First Division','National Football League','Mizoram Premier League',
    'Kazakhstan Premier League','Korea Republic K4 League','China League 2 Nord',
    'China League 2','NPL Western Australia','NPL Queensland','Regional Football Leagues',
    'Hkg Senior Shield','Senior Shield','Premier League','Premier Division','Super League',
    'Primera Division','Segunda Division','Vysshaya Liga','Pervaya Liga','Srpska Liga',
    'Prva Liga','Liga Alef','Liga 3','Liga 2','Liga 1','1st Division','2nd Division',
    '2Nd Division','First Division','Second Division','Rou Liga II','Terza Liga',
    '3 Liga','IV Liga','K League 2','1 Mfl','1 Lyga','1 Lig','Serie A','Serie B',
    'Bundesliga','2 Bundesliga','Ligue 1','Ligue 2','Eredivisie','Primeira Liga',
    'Championship','League One','League Two','Premiership','Superliga','Allsvenskan',
    'Eliteserien','Veikkausliiga','Ekstraklasa','COSAFA Cup U20','Cup'
], key=len, reverse=True)

def parse_event_cell(raw: str):
    raw = str(raw or '').replace('\r','')
    lines = [clean(x) for x in raw.split('\n') if clean(x)]
    if len(lines) >= 2 and ' - ' in lines[0] and ' - ' in lines[-1]:
        country, league = split_region(lines[0])
        teams = split_teams(lines[-1])
        if teams:
            return country, league, teams[0], teams[1]

    value = clean(raw)
    if ' - ' not in value:
        return None
    country, rest = value.split(' - ',1)
    if ' - ' not in rest:
        return None

    left, away = rest.rsplit(' - ',1)
    country,left,away = country.strip(),left.strip(),away.strip()

    for prefix in LEAGUE_PREFIXES:
        if left.lower().startswith(prefix.lower() + ' '):
            home = left[len(prefix):].strip()
            if home and away:
                return country or '—', prefix, home, away

    markers = [' Fc ',' Fk ',' Acs ',' Cs ',' Csm ',' Afc ',' Nk ',' Sc ',' Real ',
               ' Dinamo ',' Dynamo ',' Al ',' El ',' Atletico ',' Athletic ',' Union ']
    padded = ' ' + left + ' '
    best = None
    for marker in markers:
        idx = padded.lower().find(marker.lower())
        if idx > 1 and (best is None or idx < best):
            best = idx
    if best is not None:
        cut = max(0,best-1)
        league = left[:cut].strip() or '—'
        home = left[cut:].strip()
        if home and away:
            return country or '—', league, home, away

    return country or '—', '—', left, away


def ascii_norm(value: str) -> str:
    value = unicodedata.normalize('NFKD', clean(value)).encode('ascii','ignore').decode('ascii').lower()
    value = re.sub(r'\b(fc|cf|sc|ac|afc|calcio|football club|club de futbol|futbol club)\b', ' ', value)
    return re.sub(r'[^a-z0-9]+', ' ', value).strip()

COUNTRY_GROUPS = {
    'italy': {'italy','italia'},
    'england': {'england','inghilterra'},
    'scotland': {'scotland','scozia'},
    'germany': {'germany','germania'},
    'spain': {'spain','spagna'},
    'france': {'france','francia'},
    'netherlands': {'netherlands','olanda','paesi bassi'},
    'belgium': {'belgium','belgio'},
    'portugal': {'portugal','portogallo'},
    'turkey': {'turkey','turkiye','turchia'},
    'greece': {'greece','grecia'},
}

REGION_SUFFIXES = {
    'rj','sp','mg','rs','sc','pr','ba','pe','ce','go','df','pb','rn','al','se','ma','pa','pi','mt','ms','es'
}

def canonical_team_key(value: str) -> str:
    n = ascii_norm(value)
    parts = n.split()
    if parts and parts[-1] in REGION_SUFFIXES:
        parts = parts[:-1]
    return ' '.join(parts)

def team_similarity(a: str, b: str) -> float:
    x = canonical_team_key(a)
    y = canonical_team_key(b)
    if not x or not y:
        return 0.0
    if x == y:
        return 1.0
    if x in y or y in x:
        return min(len(x),len(y))/max(len(x),len(y)) + 0.15
    return SequenceMatcher(None,x,y).ratio()

def same_fixture_record(a: dict, b: dict) -> bool:
    try:
        ta = datetime.fromisoformat(str(a.get('event_time','')))
        tb = datetime.fromisoformat(str(b.get('event_time','')))
        if abs((ta-tb).total_seconds()) > 20*60:
            return False
    except Exception:
        return False

    direct = (
        team_similarity(a.get('home_team',''),b.get('home_team','')) >= .68 and
        team_similarity(a.get('away_team',''),b.get('away_team','')) >= .68
    )
    swapped = (
        team_similarity(a.get('home_team',''),b.get('away_team','')) >= .82 and
        team_similarity(a.get('away_team',''),b.get('home_team','')) >= .82
    )
    return direct or swapped

def country_key(value: str) -> str:
    n = ascii_norm(value)
    for key, aliases in COUNTRY_GROUPS.items():
        if n in aliases:
            return key
    return n

def football_data_code(country: str, league: str) -> str | None:
    c = country_key(country)
    l = ascii_norm(league)

    # Esclude coppe, giovanili, riserve e femminile: i CSV usati qui sono campionati senior.
    if re.search(r'\b(cup|coppa|u17|u18|u19|u20|u21|u23|youth|reserve|women|femminile|friendly)\b', l):
        return None

    if c == 'italy':
        if 'serie a' in l: return 'I1'
        if 'serie b' in l: return 'I2'

    if c == 'england':
        if 'premier league' in l: return 'E0'
        if 'championship' in l: return 'E1'
        if 'league one' in l: return 'E2'
        if 'league two' in l: return 'E3'
        if 'national league' in l or 'conference' in l: return 'EC'

    if c == 'scotland':
        if 'premiership' in l: return 'SC0'
        if 'championship' in l: return 'SC1'
        if 'league one' in l: return 'SC2'
        if 'league two' in l: return 'SC3'

    if c == 'germany':
        if '2 bundesliga' in l or '2nd bundesliga' in l: return 'D2'
        if 'bundesliga' in l: return 'D1'

    if c == 'spain':
        if 'segunda' in l: return 'SP2'
        if 'la liga' in l or 'primera division' in l: return 'SP1'

    if c == 'france':
        if 'ligue 2' in l: return 'F2'
        if 'ligue 1' in l: return 'F1'

    if c == 'netherlands' and 'eredivisie' in l:
        return 'N1'

    if c == 'belgium' and ('jupiler' in l or 'first division a' in l or 'pro league' in l):
        return 'B1'

    if c == 'portugal' and ('primeira liga' in l or 'liga portugal' in l):
        return 'P1'

    if c == 'turkey' and ('super lig' in l or 'super league' in l):
        return 'T1'

    if c == 'greece' and ('super league' in l or 'ethniki' in l):
        return 'G1'

    return None

def season_codes_for(day: str):
    y,m,_ = map(int, day.split('-'))
    if m >= 7:
        current_start = y
    else:
        current_start = y - 1
    previous_start = current_start - 1

    def code(start):
        return f'{start%100:02d}{(start+1)%100:02d}'

    return code(current_start), code(previous_start)

def parse_csv_date(value: str):
    txt = clean(value)
    for fmt in ('%d/%m/%Y','%d/%m/%y','%d/%m/%Y %H:%M','%d/%m/%y %H:%M'):
        try:
            return datetime.strptime(txt, fmt).replace(tzinfo=ROME)
        except Exception:
            pass
    return None

def fetch_football_data_csv(season_code: str, division_code: str):
    url = f'https://www.football-data.co.uk/mmz4281/{season_code}/{division_code}.csv'
    response = requests.get(
        url,
        timeout=14,
        headers={'User-Agent':'Mozilla/5.0 StepByStep/1.0'}
    )
    response.raise_for_status()

    # Alcuni file storici possono usare BOM.
    text = response.content.decode('utf-8-sig', errors='replace')
    rows = list(csv.DictReader(io.StringIO(text)))
    return rows

def load_league_history(division_code: str, day: str):
    current_code, previous_code = season_codes_for(day)
    target_day = datetime.fromisoformat(day).replace(tzinfo=ROME)
    rows = []
    current_rows = []

    for season_code, is_current in ((previous_code,False),(current_code,True)):
        try:
            season_rows = fetch_football_data_csv(season_code, division_code)
        except Exception as exc:
            print(f'WARN stats {division_code} {season_code}: {exc}', file=sys.stderr)
            continue

        for raw in season_rows:
            dt = parse_csv_date(raw.get('Date',''))
            if not dt or dt.date() >= target_day.date():
                continue

            try:
                hg = int(float(raw.get('FTHG','')))
                ag = int(float(raw.get('FTAG','')))
            except Exception:
                continue

            item = {
                'date': dt,
                'home': clean(raw.get('HomeTeam','')),
                'away': clean(raw.get('AwayTeam','')),
                'hg': hg,
                'ag': ag,
                'hs': raw.get('HS',''),
                'as': raw.get('AS',''),
                'hst': raw.get('HST',''),
                'ast': raw.get('AST',''),
                'current': is_current,
            }

            if item['home'] and item['away']:
                rows.append(item)
                if is_current:
                    current_rows.append(item)

    rows.sort(key=lambda x: x['date'])
    current_rows.sort(key=lambda x: x['date'])

    teams = sorted({r['home'] for r in rows} | {r['away'] for r in rows})
    return {'rows':rows,'current_rows':current_rows,'teams':teams}

def match_team_name(feed_name: str, candidates):
    target = ascii_norm(feed_name)
    if not target:
        return None

    exact = {ascii_norm(name):name for name in candidates}
    if target in exact:
        return exact[target]

    best_name = None
    best_score = 0.0

    for name in candidates:
        n = ascii_norm(name)
        if not n:
            continue

        compact_t = target.replace(' ','')
        compact_n = n.replace(' ','')

        if len(compact_t) >= 5 and (compact_t in compact_n or compact_n in compact_t):
            score = 0.90
        else:
            score = SequenceMatcher(None, compact_t, compact_n).ratio()

        if score > best_score:
            best_score = score
            best_name = name

    return best_name if best_score >= 0.72 else None

def num_or_none(value):
    try:
        return float(value)
    except Exception:
        return None

def team_matches(history_rows, team):
    return [r for r in history_rows if r['home'] == team or r['away'] == team]

def team_metrics(matches, team, limit=10, venue=None):
    selected = []

    for r in matches:
        is_home = r['home'] == team
        if venue == 'home' and not is_home:
            continue
        if venue == 'away' and is_home:
            continue
        selected.append(r)

    selected = selected[-limit:]
    n = len(selected)

    if not n:
        return {'matches':0}

    wins=draws=losses=gf=ga=over15=over25=btts=scored=clean_sheets=points=0
    shots=shots_on_target=0.0
    shots_n=sot_n=0

    for r in selected:
        is_home = r['home'] == team
        tgf = r['hg'] if is_home else r['ag']
        tga = r['ag'] if is_home else r['hg']

        gf += tgf
        ga += tga

        if tgf > tga:
            wins += 1
            points += 3
        elif tgf == tga:
            draws += 1
            points += 1
        else:
            losses += 1

        total = tgf + tga
        over15 += int(total >= 2)
        over25 += int(total >= 3)
        btts += int(tgf > 0 and tga > 0)
        scored += int(tgf > 0)
        clean_sheets += int(tga == 0)

        sh = num_or_none(r['hs'] if is_home else r['as'])
        st = num_or_none(r['hst'] if is_home else r['ast'])

        if sh is not None:
            shots += sh
            shots_n += 1
        if st is not None:
            shots_on_target += st
            sot_n += 1

    return {
        'matches': n,
        'wins': wins,
        'draws': draws,
        'losses': losses,
        'ppg': round(points/n,3),
        'gf_pg': round(gf/n,3),
        'ga_pg': round(ga/n,3),
        'goal_total_pg': round((gf+ga)/n,3),
        'over15_rate': round(over15/n,3),
        'over25_rate': round(over25/n,3),
        'btts_rate': round(btts/n,3),
        'scored_rate': round(scored/n,3),
        'clean_sheet_rate': round(clean_sheets/n,3),
        'shots_pg': round(shots/shots_n,2) if shots_n else None,
        'sot_pg': round(shots_on_target/sot_n,2) if sot_n else None,
    }

def table_snapshot(current_rows):
    table = {}

    for r in current_rows:
        for team in (r['home'],r['away']):
            table.setdefault(team,{'played':0,'points':0,'gf':0,'ga':0})

        h=table[r['home']]
        a=table[r['away']]
        h['played'] += 1
        a['played'] += 1
        h['gf'] += r['hg']; h['ga'] += r['ag']
        a['gf'] += r['ag']; a['ga'] += r['hg']

        if r['hg'] > r['ag']:
            h['points'] += 3
        elif r['hg'] < r['ag']:
            a['points'] += 3
        else:
            h['points'] += 1
            a['points'] += 1

    ordered = sorted(
        table.items(),
        key=lambda kv:(kv[1]['points'],kv[1]['gf']-kv[1]['ga'],kv[1]['gf']),
        reverse=True
    )

    out={}
    total=len(ordered)

    for pos,(team,row) in enumerate(ordered,1):
        played=row['played']
        out[team]={
            'position':pos,
            'teams':total,
            'played':played,
            'points':row['points'],
            'ppg':round(row['points']/played,3) if played else 0,
            'goal_diff':row['gf']-row['ga'],
        }

    return out

def workload_metrics(rows, team, day):
    target = datetime.fromisoformat(day).replace(tzinfo=ROME)
    dates=[r['date'] for r in rows if r['home']==team or r['away']==team]
    dates=[d for d in dates if d < target]

    if not dates:
        return {'rest_days':None,'matches_14d':0}

    last=max(dates)
    rest=(target.date()-last.date()).days
    matches_14=sum(1 for d in dates if 0 < (target.date()-d.date()).days <= 14)

    return {'rest_days':rest,'matches_14d':matches_14}

def fixture_stat_profile(history, home_name, away_name, day):
    rows=history['rows']
    current_rows=history['current_rows']
    table=table_snapshot(current_rows)

    home_all=team_metrics(rows,home_name,10,None)
    away_all=team_metrics(rows,away_name,10,None)
    home_venue=team_metrics(rows,home_name,5,'home')
    away_venue=team_metrics(rows,away_name,5,'away')

    return {
        'coverage':'historical-results',
        'home':home_all,
        'away':away_all,
        'home_venue':home_venue,
        'away_venue':away_venue,
        'home_table':table.get(home_name),
        'away_table':table.get(away_name),
        'home_workload':workload_metrics(rows,home_name,day),
        'away_workload':workload_metrics(rows,away_name,day),
    }

def enrich_records_with_stats(records, day: str):
    by_event={}
    for r in records:
        by_event.setdefault(r['event_id'],r)

    needed_codes={}
    for event_id,r in by_event.items():
        code=football_data_code(r.get('country',''),r.get('league_name',''))
        if code:
            needed_codes.setdefault(code,[]).append(event_id)

    histories={}
    for code in needed_codes:
        try:
            histories[code]=load_league_history(code,day)
        except Exception as exc:
            print(f'WARN stats league {code}: {exc}',file=sys.stderr)

    profiles={}
    for event_id,r in by_event.items():
        code=football_data_code(r.get('country',''),r.get('league_name',''))
        history=histories.get(code)
        if not history or not history['teams']:
            continue

        home=match_team_name(r.get('home_team',''),history['teams'])
        away=match_team_name(r.get('away_team',''),history['teams'])

        if not home or not away or home==away:
            continue

        profile=fixture_stat_profile(history,home,away,day)

        # Richiediamo almeno un minimo di storico reale per considerare
        # la conferma statistica utilizzabile.
        if profile['home'].get('matches',0) < 3 or profile['away'].get('matches',0) < 3:
            continue

        profiles[event_id]=profile

    enriched=0
    for r in records:
        profile=profiles.get(r['event_id'])
        if profile:
            r['_stats']=profile
            enriched+=1

    return records, len(profiles), enriched


def make_options():
    opts = Options()
    opts.add_argument('--headless=new')
    opts.add_argument('--no-sandbox')
    opts.add_argument('--disable-dev-shm-usage')
    opts.add_argument('--disable-gpu')
    opts.add_argument('--window-size=1600,1200')
    opts.add_argument('--lang=it-IT')
    opts.add_argument('--disable-blink-features=AutomationControlled')
    opts.set_capability('pageLoadStrategy','eager')
    return opts

def chrome_rows(day: str, attempts: int = 3):
    last_error = None

    for attempt in range(1, attempts+1):
        stamp = int(time.time())
        url = (
            'https://odd24.io/odds'
            f'?dates={day}&days=1&markets=PRINCIPALI&minAgioPct=0'
            f'&pageSize=1000&providers=ALL&_cb={stamp}-{attempt}'
        )

        driver = webdriver.Chrome(options=make_options())
        try:
            driver.set_page_load_timeout(35)
            driver.get(url)

            def count_rows(d):
                try:
                    return d.execute_script("""
                        return [...document.querySelectorAll('tr')].filter(tr=>{
                          const cells=[...tr.querySelectorAll('td')];
                          return cells.length>=10 && /^\s*\d{1,2}:\d{2}/.test(cells[0]?.innerText||'');
                        }).length;
                    """)
                except Exception:
                    return 0

            try:
                WebDriverWait(driver,30,poll_frequency=1).until(lambda d: count_rows(d) >= 5)
            except Exception:
                # Anche se il wait scade, prova a leggere ciò che la pagina ha già renderizzato.
                pass

            rows = driver.execute_script("""
                return [...document.querySelectorAll('tr')]
                  .map(tr=>[...tr.querySelectorAll('td')].map(td=>{
                    const txt=td.innerText||'';
                    const quote=td.querySelector('.quote-open');
                    const provider=quote?.getAttribute('title')||'';
                    const modal=quote?.getAttribute('data-modal-url')||'';
                    return txt+
                      (provider?'\\n__PROVIDER__='+provider:'')+
                      (modal?'\\n__MODAL__='+modal:'');
                  }))
                  .filter(cells=>cells.length>=10 && /^\s*\d{1,2}:\d{2}/.test(cells[0]||''));
            """)


            if len(rows) >= 5:
                return rows

            last_error = RuntimeError(f'{day}: solo {len(rows)} righe al tentativo {attempt}')
        except Exception as exc:
            last_error = exc
        finally:
            driver.quit()

        time.sleep(3)

    raise last_error or RuntimeError(f'{day}: nessuna riga disponibile')

def parse_rows(rows, day: str):
    out=[]
    for cells in rows:
        if len(cells)<18:
            continue

        hhmm=parse_time_text(cells[0],day)
        if not hhmm:
            continue

        event=parse_event_cell(cells[1])
        if not event:
            continue

        country,league,home,away=event

        # Scarta eventi malformati: una squadra non può giocare contro se stessa.
        # Questo evita che un errore di parsing venga usato dal motore di selezione.
        norm_home=re.sub(r'[^a-z0-9]+','',home.lower())
        norm_away=re.sub(r'[^a-z0-9]+','',away.lower())
        if norm_home and norm_home==norm_away:
            continue

        base={
            'home':home,
            'away':away,
            'country':country,
            'league':league,
            'kickoff':kickoff_iso(day,hhmm),
            'id':f'{day}|{hhmm}|{home}|{away}',
        }

        add_record(out,base,'1X2','1',cells[2],.68)
        add_record(out,base,'1X2','2',cells[4],.68)

        add_record(out,base,'Doppia chance','1X',cells[6],.80)
        add_record(out,base,'Doppia chance','12',cells[7],.76)
        add_record(out,base,'Doppia chance','X2',cells[8],.80)

        add_record(out,base,'Over/Under','Over 2.5',cells[10],.72)
        add_record(out,base,'Over/Under','Under 2.5',cells[11],.72)

        add_record(out,base,'Gol/No Gol','GG',cells[13],.70)
        add_record(out,base,'Gol/No Gol','NG',cells[14],.70)

        add_record(out,base,'Over/Under','Over 1.5',cells[16],.80)
        add_record(out,base,'Over/Under','Under 1.5',cells[17],.66)

    return out

def unique(records):
    seen=set()
    out=[]
    for r in records:
        key=(r['event_id'],r['market_name'],r['selection_column'])
        if key in seen:
            continue
        seen.add(key)
        out.append(r)
    return out

def counts(records):
    result={'1X2':0,'Doppia chance':0,'Over/Under':0,'Gol/No Gol':0}
    for r in records:
        if r['market_name'] in result:
            result[r['market_name']]+=1
    return result

def betflag_market_kind(name: str) -> str | None:
    n = ascii_norm(name).upper()
    if n == 'DC' or 'DOPPIA CHANCE' in n:
        return 'Doppia chance'
    if n == '1X2' or 'ESITO FINALE' in n:
        return '1X2'
    if n == 'U O' or 'UNDER OVER' in n or ('UNDER' in n and 'OVER' in n):
        return 'Over/Under'
    if n == 'GG NG' or ('GOL' in n and 'NO GOL' in n) or ('GOAL' in n and 'NO GOAL' in n):
        return 'Gol/No Gol'
    return None

def betflag_norm_sign(value: str) -> str:
    n = ascii_norm(value).upper().replace(' ', '')
    n = n.replace('DOPPIACHANCE','')
    if n in {'GOAL','GOL','BTS','BTTS','GG'}:
        return 'GG'
    if n in {'NOGOAL','NOGOL','NG'}:
        return 'NG'
    if n in {'HOME','CASA'}:
        return '1'
    if n in {'AWAY','OSPITE'}:
        return '2'
    if n in {'DRAW','PAREGGIO'}:
        return 'X'
    return n

def parse_betflag_line(value) -> float | None:
    txt = str(value or '').replace(',','.')
    nums = re.findall(r'\d+(?:\.\d+)?', txt)
    if not nums:
        return None
    try:
        n=float(nums[-1])
    except Exception:
        return None
    if n > 10 and n in (15,25,35,45,55,65):
        n=n/10
    return n if 0 <= n <= 20 else None

def betflag_selection_map(event: dict):
    out={}
    for market in (event.get('mmkW') or {}).values():
        if not isinstance(market,dict):
            continue
        market_name=clean(market.get('mn',''))
        kind=betflag_market_kind(market_name)
        if not kind:
            continue
        for line,data in (market.get('spd') or {}).items():
            if not isinstance(data,dict):
                continue
            line_num=parse_betflag_line(line)
            for sign in data.get('asl') or []:
                if not isinstance(sign,dict):
                    continue
                raw_sign=clean(sign.get('sn',''))
                try:
                    odd=float(sign.get('ov'))
                except Exception:
                    continue
                if not raw_sign or not (1.01 < odd <= 30):
                    continue
                ns=betflag_norm_sign(raw_sign)

                key=None
                if kind == 'Doppia chance' and ns in {'1X','12','X2'}:
                    key=(kind,ns)
                elif kind == '1X2' and ns in {'1','X','2'}:
                    key=(kind,ns)
                elif kind == 'Gol/No Gol' and ns in {'GG','NG'}:
                    key=(kind,ns)
                elif kind == 'Over/Under':
                    direction=None
                    if 'OVER' in ns or ns in {'O','OV'}:
                        direction='Over'
                    elif 'UNDER' in ns or ns in {'U','UN'}:
                        direction='Under'
                    sign_line=parse_betflag_line(raw_sign)
                    use_line=sign_line if sign_line is not None else line_num
                    if direction and use_line is not None:
                        key=(kind,f'{direction} {use_line:.1f}')

                if key:
                    prev=out.get(key)
                    if prev is None or odd < prev:
                        out[key]=round(odd,3)
    return out

def betflag_parse_event(event: dict, day: str):
    name=clean(event.get('en',''))
    cut=name.find(' - ')
    if cut < 1:
        teams=event.get('teams') or []
        home=clean((teams[0] or {}).get('nm','')) if len(teams)>0 and isinstance(teams[0],dict) else ''
        away=clean((teams[1] or {}).get('nm','')) if len(teams)>1 and isinstance(teams[1],dict) else ''
    else:
        home=name[:cut].strip()
        away=name[cut+3:].strip()
    if not home or not away:
        return None

    raw=clean(event.get('ed',''))
    mt=re.search(r'(\d{2})-(\d{2})-(\d{4})\s+(\d{2}:\d{2})',raw)
    if not mt:
        return None
    d=f'{mt.group(3)}-{mt.group(2)}-{mt.group(1)}'
    if d != day:
        return None
    kickoff=kickoff_iso(day,mt.group(4))
    selections=betflag_selection_map(event)
    if not selections:
        return None
    return {
        'home_team':home,
        'away_team':away,
        'event_time':kickoff,
        'event_id':str(event.get('ei') or f'{day}|{home}|{away}'),
        'league_name':clean(event.get('td','')) or '—',
        'country':'—',
        'betflag_event_id':int(event.get('ei') or 0),
        'betflag_tournament_id':int(event.get('ti') or 0),
        'selections':selections,
    }

def fetch_betflag_playable(day: str):
    session=requests.Session()
    session.headers.update(BETFLAG_HEADERS)

    program=session.get(
        BETFLAG_API+'/api/sport/pregame/getProgram?channelId=1',
        timeout=22
    )
    program.raise_for_status()
    data=program.json()
    football=next((x for x in data if int(x.get('id') or 0)==1),None)
    if not football:
        raise RuntimeError('Betflag calcio non trovato')

    tournament_ids=[]
    for country in football.get('lc') or []:
        for tournament in country.get('lts') or []:
            try:
                tid=int(tournament.get('id') or 0)
            except Exception:
                continue
            counts=tournament.get('nEfT') or []
            total=int(tournament.get('ne') or 0)
            if isinstance(counts,list) and counts:
                total=max(total,*[int(x or 0) for x in counts if str(x or '').isdigit()])
            if 0 < tid < 1_000_000_000 and total > 0:
                tournament_ids.append(tid)

    tournament_ids=sorted(set(tournament_ids))
    fixtures=[]

    def one(tid):
        url=(
            BETFLAG_API+
            f'/api/sport/pregame/getOverviewEventsAams/0/1/79/{tid}/0/0/0?channelId=1'
        )
        response=requests.get(url,headers=BETFLAG_HEADERS,timeout=18)
        response.raise_for_status()
        body=response.json()
        return [
            item for item in (
                betflag_parse_event(e,day)
                for e in (body.get('leo') or [])
            )
            if item
        ]

    with ThreadPoolExecutor(max_workers=8) as pool:
        jobs={pool.submit(one,tid):tid for tid in tournament_ids}
        for future in as_completed(jobs):
            try:
                fixtures.extend(future.result())
            except Exception as exc:
                print(f'WARN Betflag torneo {jobs[future]}: {exc}',file=sys.stderr)

    print(f'BETFLAG {day}: {len(fixtures)} fixture con mercati giocabili')
    return fixtures

def betflag_match_for_record(record: dict, fixtures):
    target={
        'home_team':record.get('home_team',''),
        'away_team':record.get('away_team',''),
        'event_time':record.get('event_time',''),
    }
    market=record.get('market_name','')
    outcome=record.get('selection_column','')
    key=(market,outcome)

    best=None
    best_score=-1.0
    for fixture in fixtures:
        if key not in (fixture.get('selections') or {}):
            continue
        try:
            ta=datetime.fromisoformat(str(target['event_time']))
            tb=datetime.fromisoformat(str(fixture['event_time']))
            if abs((ta-tb).total_seconds()) > 75*60:
                continue
        except Exception:
            continue

        direct=(
            team_similarity(target['home_team'],fixture['home_team'])+
            team_similarity(target['away_team'],fixture['away_team'])
        )/2
        swapped=(
            team_similarity(target['home_team'],fixture['away_team'])+
            team_similarity(target['away_team'],fixture['home_team'])
        )/2
        score=max(direct,swapped-.08)
        if score >= .70 and score > best_score:
            best_score=score
            best=fixture

    return best

def filter_records_playable_on_betflag(records, fixtures):
    playable=[]
    matched_fixtures=set()
    for record in records:
        fixture=betflag_match_for_record(record,fixtures)
        if not fixture:
            continue
        key=(record.get('market_name',''),record.get('selection_column',''))
        odd=(fixture.get('selections') or {}).get(key)
        if odd is None:
            continue
        item=dict(record)
        item['odd']=round(float(odd),3)
        item['_betflagPlayable']=True
        item['_betflagEventId']=fixture.get('betflag_event_id')
        item['_betflagTournamentId']=fixture.get('betflag_tournament_id')
        item['_betflagCheckedAt']=datetime.now(ROME).isoformat()
        playable.append(item)
        matched_fixtures.add(fixture.get('betflag_event_id'))
    print(
        f'BETFLAG MATCH {datetime.now(ROME).isoformat()}: '
        f'{len(playable)} selezioni su {len(matched_fixtures)} fixture'
    )
    return playable

def modal_header_match(header: str, outcome: str) -> bool:
    h=ascii_norm(header).upper().replace(' ','')
    o=ascii_norm(outcome).upper().replace(' ','')

    if o in {'1','X','2','1X','12','X2','GG','NG'}:
        return h == o

    if o.startswith('OVER'):
        return (
            'OVER' in h or
            h in {'O','OV'}
        )

    if o.startswith('UNDER'):
        return (
            'UNDER' in h or
            h in {'U','UN'}
        )

    return False

def median_value(values):
    values=sorted(float(x) for x in values if x is not None)
    if not values:
        return None
    n=len(values)
    mid=n//2
    if n%2:
        return values[mid]
    return (values[mid-1]+values[mid])/2

def market_quotes_from_modal(html_text: str):
    soup=BeautifulSoup(html_text,'html.parser')
    out={}

    for table in soup.select('table.odm-table'):
        headers=[
            clean(th.get_text(' ',strip=True))
            for th in table.select('thead th')
        ]
        if not headers:
            continue

        for tr in table.select('tbody tr'):
            if 'summary-row' in (tr.get('class') or []):
                continue

            tds=tr.find_all('td',recursive=False)
            if not tds:
                continue

            provider_cell=tds[0]
            provider=clean(
                provider_cell.get('title') or
                provider_cell.get_text(' ',strip=True)
            ).upper()

            if not provider or provider in {'OPERATORI','VAL. MAX','VAL MAX'}:
                continue

            for idx in range(1,min(len(tds)-1,len(headers)-1)):
                label=headers[idx]
                raw=clean(tds[idx].get_text(' ',strip=True))
                m=re.search(r'(?<!\d)(\d+(?:[.,]\d+)?)(?!\d)',raw)
                if not m:
                    continue
                try:
                    odd=float(m.group(1).replace(',','.'))
                except Exception:
                    continue
                if not (1.01 < odd <= 30):
                    continue

                bucket=out.setdefault(label,{'all':[],'providers':{},'betflag':None})
                bucket['all'].append(odd)
                bucket['providers'][provider]=odd
                if provider=='BETFLAG':
                    bucket['betflag']=odd

    return out

def stat_rate(block, key):
    try:
        matches=int((block or {}).get('matches') or 0)
        if matches<3:
            return None
        if key.endswith('_rate'):
            return float((block or {}).get(key))
        count=float((block or {}).get(key) or 0)
        return count/matches
    except Exception:
        return None

def stats_support_for_record(record: dict) -> float:
    profile=record.get('_stats')
    if not isinstance(profile,dict):
        return 0.0

    home=profile.get('home_venue') or profile.get('home') or {}
    away=profile.get('away_venue') or profile.get('away') or {}

    market=str(record.get('market_name') or '')
    outcome=str(record.get('selection_column') or '')

    hw=stat_rate(home,'wins')
    hd=stat_rate(home,'draws')
    hl=stat_rate(home,'losses')
    aw=stat_rate(away,'wins')
    ad=stat_rate(away,'draws')
    al=stat_rate(away,'losses')

    ho15=stat_rate(home,'over15_rate')
    ao15=stat_rate(away,'over15_rate')
    ho25=stat_rate(home,'over25_rate')
    ao25=stat_rate(away,'over25_rate')
    hbtts=stat_rate(home,'btts_rate')
    abtts=stat_rate(away,'btts_rate')

    def avg(a,b):
        vals=[x for x in (a,b) if x is not None]
        return sum(vals)/len(vals) if vals else None

    score=0.0

    if market=='1X2':
        if outcome=='1' and hw is not None and al is not None:
            score=(hw+al)/2
        elif outcome=='2' and aw is not None and hl is not None:
            score=(aw+hl)/2

    elif market=='Doppia chance':
        if outcome=='1X' and hl is not None and aw is not None:
            score=1-((hl+aw)/2)
        elif outcome=='X2' and hw is not None and al is not None:
            score=1-((hw+al)/2)
        elif outcome=='12' and hd is not None and ad is not None:
            score=1-((hd+ad)/2)

    elif market=='Over/Under':
        if outcome=='Over 1.5':
            score=avg(ho15,ao15) or 0.0
        elif outcome=='Over 2.5':
            score=avg(ho25,ao25) or 0.0
        elif outcome=='Under 2.5':
            over=avg(ho25,ao25)
            score=(1-over) if over is not None else 0.0

    elif market=='Gol/No Gol':
        if outcome=='GG':
            score=avg(hbtts,abtts) or 0.0
        elif outcome=='NG':
            btts=avg(hbtts,abtts)
            score=(1-btts) if btts is not None else 0.0

    return round(max(0.0,min(1.0,score)),3)

def quality_tier_for_record(record: dict) -> int:
    support=int(record.get('_externalSupportCount') or 0)
    oppose=int(record.get('_externalOpposeCount') or 0)
    validated90=int(record.get('_validated90SupportCount') or 0)
    stats=float(record.get('_statsSupport') or 0)
    providers=int(record.get('_providerCount') or 0)
    spread=float(record.get('_marketSpreadPct') or 9)
    deviation=float(record.get('_betflagDeviationPct') or 9)
    median_odd=float(record.get('_marketMedianOdd') or 99)

    # Se il mercato è poco coperto o Betflag è un forte outlier,
    # la selezione non è abbastanza stabile per la board.
    if providers < 5:
        return 0
    if spread > .24 or deviation > .12:
        return 0
    if oppose > support and oppose > 0:
        return 0

    # V9 PRIMARY: almeno una fonte con track record pubblico >=90%
    # sullo STESSO tipo di mercato + mercato bookmaker stabile.
    if validated90>=1 and providers>=5 and spread<=.18 and deviation<=.10:
        return 4

    # A: consenso esterno forte.
    if support >= 2 and oppose == 0:
        return 4

    # B: una conferma esterna + statistiche reali coerenti.
    if support >= 1 and oppose == 0 and stats >= .58:
        return 4

    # C: statistiche molto forti + mercato largo e stabile.
    if stats >= .72 and providers >= 6 and spread <= .18:
        return 3

    # D: una conferma esterna senza opposizioni, con mercato stabile.
    if support >= 1 and oppose == 0 and providers >= 6 and spread <= .18:
        return 3

    # E: solo mercato, ma deve essere molto ampio/stabile e la probabilità
    # implicita deve essere elevata. È il fallback meno preferito.
    if (
        support==0 and oppose==0 and
        providers>=8 and
        spread<=.12 and
        deviation<=.07 and
        median_odd<=1.42
    ):
        return 2

    return 0

def verify_records_on_odd24_betflag(records, day: str, now: datetime):
    cutoff=now+timedelta(minutes=45)

    groups={}
    for record in records:
        modal=record.get('_modalUrl')
        if not modal:
            continue

        try:
            kickoff=datetime.fromisoformat(str(record.get('event_time','')))
        except Exception:
            continue

        if kickoff < cutoff:
            continue

        if record.get('selection_column') in ('X','Under 1.5'):
            continue

        try:
            overview_odd=float(record.get('odd') or 0)
        except Exception:
            continue

        if not (1.03 <= overview_odd <= 2.30):
            continue

        groups.setdefault(str(modal),[]).append(record)

    if not groups:
        return []

    headers={
        'User-Agent':'Mozilla/5.0 StepByStep/1.0',
        'Accept':'text/html,*/*',
        'Referer':'https://odd24.io/odds',
    }

    def fetch_modal(modal):
        url=modal if str(modal).startswith('http') else 'https://odd24.io'+str(modal)
        response=requests.get(url,timeout=16,headers=headers)
        response.raise_for_status()
        return modal,market_quotes_from_modal(response.text)

    modal_quotes={}
    with ThreadPoolExecutor(max_workers=12) as pool:
        jobs={pool.submit(fetch_modal,modal):modal for modal in groups}
        for future in as_completed(jobs):
            modal=jobs[future]
            try:
                key,quotes=future.result()
                modal_quotes[key]=quotes
            except Exception as exc:
                print(f'WARN Odd24 modal {modal}: {exc}',file=sys.stderr)

    verified=[]
    checked_at=datetime.now(ROME).isoformat()

    for modal,rows in groups.items():
        quotes=modal_quotes.get(modal) or {}
        if not quotes:
            continue

        for record in rows:
            outcome=str(record.get('selection_column') or '')
            matched=None

            for header,bucket in quotes.items():
                if modal_header_match(header,outcome):
                    matched=bucket
                    break

            if not matched:
                continue

            betflag=matched.get('betflag')
            all_odds=[float(x) for x in matched.get('all') or [] if float(x)>1]
            if betflag is None or len(all_odds)<3:
                continue

            median=median_value(all_odds)
            if median is None or median<=1:
                continue

            minimum=min(all_odds)
            maximum=max(all_odds)
            spread=(maximum-minimum)/median
            deviation=abs(float(betflag)-median)/median

            item=dict(record)
            item['odd']=round(float(betflag),3)
            item['_betflagPlayable']=True
            item['_bookmaker']='Betflag'
            item['_betflagOdd']=round(float(betflag),3)
            item['_betflagCheckedAt']=checked_at
            item['_playabilitySource']='odd24-modal-betflag-v2'
            item['_providerCount']=len(matched.get('providers') or {})
            item['_marketMedianOdd']=round(median,3)
            item['_marketSpreadPct']=round(spread,4)
            item['_betflagDeviationPct']=round(deviation,4)
            verified.append(item)

    print(
        f'BETFLAG MODAL V2 {day}: {len(verified)} selezioni verificate '
        f'su {len(groups)} mercati controllati'
    )

    return verified




VALIDATED_90_SOURCES_V9 = {
    'matris_dc_85': {
        'market':'Doppia chance',
        'threshold':85.0,
        'historical_win_rate':90.7,
        'sample':7108,
    },
    'footballprediction_ai_over15': {
        'market':'Over 1.5',
        'historical_win_rate':90.6,
        'sample':286,
    },
}

def absolute_url(base: str, href: str):
    try:
        return urljoin(base,href)
    except Exception:
        return href

def aliases_present_in_text(team: str, text: str) -> bool:
    t=source_plain(text)
    for alias in source_team_aliases(team):
        if alias and alias in t:
            return True
    return False

def matris_dc_values_from_html(html_text: str):
    soup=BeautifulSoup(html_text,'html.parser')
    text=clean(soup.get_text(' ',strip=True))
    values={}

    patterns=[
        re.compile(r'\b(1X|X1|12|X2|2X)\b.{0,180}?(\d{2,3}(?:[.,]\d+)?)\s*%',re.I),
        re.compile(r'(\d{2,3}(?:[.,]\d+)?)\s*%\s*\(\s*(1X|X1|12|X2|2X)\b',re.I),
    ]

    for pat in patterns:
        for m in pat.finditer(text):
            if pat.pattern.startswith(r'\b('):
                sign=m.group(1).upper()
                pct=m.group(2)
            else:
                pct=m.group(1)
                sign=m.group(2).upper()

            if sign=='X1': sign='1X'
            if sign=='2X': sign='X2'

            try:
                value=float(pct.replace(',','.'))
            except Exception:
                continue

            if 0 < value <= 100:
                values[sign]=max(value,values.get(sign,0))

    return values

def load_matris_high_confidence_dc_v9(records):
    candidates=[
        r for r in records
        if r.get('market_name')=='Doppia chance' and
        r.get('selection_column') in {'1X','12','X2'}
    ]
    if not candidates:
        return {}

    headers={
        'User-Agent':'Mozilla/5.0 StepByStep/1.0',
        'Accept':'text/html,*/*',
    }
    session=requests.Session()
    session.headers.update(headers)

    seeds=[
        'https://matrisx.com/en',
        'https://matrisx.com/en/track-record',
    ]

    league_urls=set()
    match_index=[]

    for seed in seeds:
        try:
            response=session.get(seed,timeout=16)
            response.raise_for_status()
            soup=BeautifulSoup(response.text,'html.parser')
            for a in soup.select('a[href]'):
                href=str(a.get('href') or '')
                if '/en/leagues/' in href:
                    league_urls.add(absolute_url(seed,href))
                if '/en/match/' in href:
                    label=clean(
                        a.get_text(' ',strip=True) or
                        (a.parent.get_text(' ',strip=True) if a.parent else '')
                    )
                    match_index.append((absolute_url(seed,href),label))
        except Exception as exc:
            print(f'WARN Matris seed {seed}: {exc}',file=sys.stderr)

    def fetch_league(url):
        response=session.get(url,timeout=16)
        response.raise_for_status()
        soup=BeautifulSoup(response.text,'html.parser')
        rows=[]
        for a in soup.select('a[href]'):
            href=str(a.get('href') or '')
            if '/en/match/' not in href:
                continue
            parent_text=(
                a.parent.get_text(' ',strip=True)
                if a.parent else ''
            )
            label=clean(a.get_text(' ',strip=True)+' '+parent_text)
            rows.append((absolute_url(url,href),label))
        return rows

    league_urls=list(league_urls)[:40]
    with ThreadPoolExecutor(max_workers=8) as pool:
        jobs={pool.submit(fetch_league,url):url for url in league_urls}
        for future in as_completed(jobs):
            try:
                match_index.extend(future.result())
            except Exception as exc:
                print(
                    f'WARN Matris league {jobs[future]}: {exc}',
                    file=sys.stderr
                )

    # URL unici mantenendo il testo più ricco.
    unique_links={}
    for url,label in match_index:
        if not url:
            continue
        if len(label)>len(unique_links.get(url,'')):
            unique_links[url]=label

    event_to_url={}
    for r in candidates:
        for url,label in unique_links.items():
            if (
                aliases_present_in_text(r.get('home_team',''),label) and
                aliases_present_in_text(r.get('away_team',''),label)
            ):
                event_to_url[r['event_id']]=url
                break

    urls=sorted(set(event_to_url.values()))
    page_values={}

    def fetch_match(url):
        response=session.get(url,timeout=16)
        response.raise_for_status()
        return url,matris_dc_values_from_html(response.text)

    with ThreadPoolExecutor(max_workers=8) as pool:
        jobs={pool.submit(fetch_match,url):url for url in urls}
        for future in as_completed(jobs):
            url=jobs[future]
            try:
                key,values=future.result()
                page_values[key]=values
            except Exception as exc:
                print(f'WARN Matris match {url}: {exc}',file=sys.stderr)

    support={}
    for r in candidates:
        url=event_to_url.get(r['event_id'])
        if not url:
            continue
        sign=str(r.get('selection_column') or '')
        pct=float((page_values.get(url) or {}).get(sign) or 0)
        if pct>=85.0:
            support[(r['event_id'],r['market_name'],sign)]={
                'source':'matris_dc_85',
                'probability':round(pct,1),
                'historical_win_rate':90.7,
                'sample':7108,
            }

    print(
        f'VALIDATED90 Matris: {len(support)} selezioni DC>=85%'
    )
    return support

def load_footballprediction_ai_over15_v9(records):
    candidates=[
        r for r in records
        if r.get('market_name')=='Over/Under' and
        r.get('selection_column')=='Over 1.5'
    ]
    if not candidates:
        return {}

    url='https://www.footballprediction.ai/'
    try:
        response=requests.get(
            url,
            timeout=18,
            headers={
                'User-Agent':'Mozilla/5.0 StepByStep/1.0',
                'Accept':'text/html,*/*',
            }
        )
        response.raise_for_status()
        soup=BeautifulSoup(response.text,'html.parser')
        text=source_plain(soup.get_text(' ',strip=True))
    except Exception as exc:
        print(f'WARN FootballPredictionAI: {exc}',file=sys.stderr)
        return {}

    support={}
    for r in candidates:
        chunk=source_find_nearest_pair(
            text,
            r.get('home_team',''),
            r.get('away_team','')
        )
        if not chunk:
            continue

        # Il track record 90.6% riguarda le pick pubblicate di tipo Over 1.5.
        # Non basta che la card contenga il mercato: deve essere la scelta AI.
        m=re.search(
            r'ai s choice\s+(.{0,55})',
            chunk,
            re.I
        )
        if not m:
            continue

        choice=m.group(1)
        if not re.search(r'\bover\s*1[.,]5\b|\bover\s*1\.5\b',choice,re.I):
            continue

        support[(r['event_id'],r['market_name'],r['selection_column'])]={
            'source':'footballprediction_ai_over15',
            'historical_win_rate':90.6,
            'sample':286,
        }

    print(
        f'VALIDATED90 FootballPredictionAI: {len(support)} selezioni Over1.5'
    )
    return support


def _find_pair_after_percent(text: str, home: str, away: str):
    text=source_plain(text)
    h_aliases=source_team_aliases(home)
    a_aliases=source_team_aliases(away)
    best=None

    for h in h_aliases:
        hp=text.find(h)
        while hp>=0:
            for a in a_aliases:
                ap=text.find(a,max(0,hp-80),min(len(text),hp+500))
                if ap<0:
                    continue

                end=max(hp+len(h),ap+len(a))
                after=text[end:end+260]
                m=re.search(r'\b(\d{2,3}(?:\.\d+)?)\s*%',after)
                if not m:
                    continue

                try:
                    pct=float(m.group(1))
                except Exception:
                    continue

                distance=abs(ap-hp)
                candidate=(distance,pct)
                if best is None or candidate[0]<best[0]:
                    best=candidate
            hp=text.find(h,hp+max(1,len(h)))

    return best[1] if best else None

def load_ganhar_dc90_v11(records, day: str):
    candidates=[
        r for r in records
        if r.get('market_name')=='Doppia chance' and
        r.get('selection_column') in {'1X','X2'}
    ]
    if not candidates:
        return {}

    pages={}
    headers={
        'User-Agent':'Mozilla/5.0 StepByStep/1.0',
        'Accept':'text/html,*/*',
    }

    market_map={
        '1X':'double_chance_1x',
        'X2':'double_chance_x2',
    }

    for sign,market in market_map.items():
        direct=f'https://www.ganhar.pt/en/predictions?date={day}&market={market}'
        bare=f'https://ganhar.pt/en/predictions?date={day}&market={market}'
        pt=f'https://ganhar.pt/pt/previsoes?date={day}&market={market}'
        urls=[
            direct,
            bare,
            pt,
            'https://r.jina.ai/http://ganhar.pt/en/predictions?date='+day+'&market='+market,
            'https://r.jina.ai/https://ganhar.pt/en/predictions?date='+day+'&market='+market,
        ]
        errors=[]
        for url in urls:
            try:
                response=requests.get(url,headers=headers,timeout=20)
                response.raise_for_status()
                raw=response.text
                if 'r.jina.ai/' in url:
                    page=source_plain(raw)
                else:
                    page=source_plain(
                        BeautifulSoup(raw,'html.parser').get_text(' ',strip=True)
                    )
                if len(page)<500:
                    raise RuntimeError('pagina troppo corta')
                pages[sign]=page
                break
            except Exception as exc:
                errors.append(str(exc))
        if sign not in pages:
            print(
                f'WARN Ganhar {sign}: '+' | '.join(errors),
                file=sys.stderr
            )

    support={}
    for r in candidates:
        sign=str(r.get('selection_column') or '')
        page=pages.get(sign)
        if not page:
            continue

        pct=_find_pair_after_percent(
            page,
            r.get('home_team',''),
            r.get('away_team','')
        )
        if pct is None or pct < 90.0:
            continue

        # Track record Ganhar.pt nella fascia di confidenza 90-100%.
        # Per DC 1X: 95% su 391; per DC X2: 90% su 167
        # (pagina Performance pubblica, controllata 02/10/2026).
        if sign=='1X':
            historical_rate=95.0
            sample=391
        else:
            historical_rate=90.0
            sample=167

        support[(r['event_id'],r['market_name'],r['selection_column'])]={
            'source':'ganhar_dc_'+sign.lower(),
            'prediction_confidence':round(float(pct),1),
            'historical_win_rate':historical_rate,
            'sample':sample,
        }

    print(
        f'VALIDATED90 Ganhar: {len(support)} selezioni DC>=90%'
    )
    return support

def enrich_records_with_validated90_v9(records, day: str):
    # Fonti con track record pubblico >=90% nella fascia/mercato usato.
    # Le altre fonti restano controlli secondari/tie-breaker.
    matris=load_matris_high_confidence_dc_v9(records)
    fpai=load_footballprediction_ai_over15_v9(records)
    ganhar=load_ganhar_dc90_v11(records,day)

    for r in records:
        key=(r['event_id'],r['market_name'],r['selection_column'])
        hits=[]
        if key in matris:
            hits.append(matris[key])
        if key in fpai:
            hits.append(fpai[key])
        if key in ganhar:
            hits.append(ganhar[key])

        r['_validated90SupportCount']=len(hits)
        r['_validated90Sources']=[h['source'] for h in hits]
        r['_validated90Details']=hits
        r['_validated90BestHistoricalRate']=round(
            max([h['historical_win_rate'] for h in hits],default=0),
            1
        )

    total=sum(1 for r in records if int(r.get('_validated90SupportCount') or 0)>0)
    print(f'VALIDATED90 TOTAL {day}: {total} selezioni abilitate')
    return records

def source_plain(value: str) -> str:
    value=unicodedata.normalize('NFKD',str(value or '')).encode('ascii','ignore').decode('ascii').lower()
    value=value.replace('&',' and ')
    value=re.sub(r'[^a-z0-9.+/-]+',' ',value)
    return re.sub(r'\s+',' ',value).strip()

def source_team_aliases(value: str):
    original=source_plain(value)
    original=re.sub(
        r'\b(football club|futbol club|soccer club|club de futbol|fc|cf|sc|ac|afc)\b',
        ' ',
        original
    )
    original=re.sub(r'\s+',' ',original).strip()

    aliases=set()
    if original:
        aliases.add(original)

    replacements=[
        ('manchester united','man utd'),
        ('manchester city','man city'),
        ('tottenham hotspur','tottenham'),
        ('wolverhampton wanderers','wolves'),
        ('nottingham forest','nott m forest'),
        ('newcastle united','newcastle'),
        ('west ham united','west ham'),
        ('brighton and hove albion','brighton'),
    ]

    for old,new in replacements:
        changed=original.replace(old,new).strip()
        if changed:
            aliases.add(changed)

    tokens=[x for x in original.split() if x]
    if len(tokens)>=2:
        aliases.add(' '.join(tokens[:2]))
        aliases.add(' '.join(tokens[-2:]))

    return [x for x in aliases if len(x)>=5]

def source_find_nearest_pair(text: str, home: str, away: str):
    h_aliases=source_team_aliases(home)
    a_aliases=source_team_aliases(away)
    best=None

    for h in h_aliases:
        hp=text.find(h)
        while hp>=0:
            for a in a_aliases:
                start=max(0,hp-900)
                end=min(len(text),hp+1600)
                ap=text.find(a,start,end)
                if ap>=0:
                    distance=abs(ap-hp)
                    if best is None or distance<best[0]:
                        best=(distance,hp,ap)
            hp=text.find(h,hp+len(h))

    if best is None:
        return None

    _,hp,ap=best
    left=max(0,min(hp,ap)-700)
    right=min(len(text),max(hp,ap)+1100)
    return text[left:right]

def source_signals(kind: str, chunk: str):
    s=source_plain(chunk)
    out=set()

    if re.search(r'\b(home win|home victory|prediction home|pronostico vittoria casa)\b',s):
        out.add('H')
    if re.search(r'\b(away win|away victory|prediction away|pronostico vittoria trasferta)\b',s):
        out.add('A')
    if re.search(r'\b(prediction draw|draw prediction|pareggio)\b',s):
        out.add('D')

    if kind=='predictz':
        if re.search(r'\bhome\s+\d+[-:]\d+\b',s): out.add('H')
        if re.search(r'\baway\s+\d+[-:]\d+\b',s): out.add('A')
        if re.search(r'\b(draw|tie)\s+\d+[-:]\d+\b',s): out.add('D')

    if kind=='forebetdc':
        if re.search(r'\b(?:1x|x1)\b',s): out.add('DC1X')
        if re.search(r'\bx2\b',s): out.add('DCX2')
        if re.search(r'\b12\b',s): out.add('DC12')

    if re.search(r'\b(?:tip|tips|prediction|pick|best pick)\s*[:\-]?\s*(?:home|\(?1\)?)\b',s):
        out.add('H')
    if re.search(r'\b(?:tip|tips|prediction|pick|best pick)\s*[:\-]?\s*(?:away|\(?2\)?)\b',s):
        out.add('A')
    if re.search(r'\b(?:tip|tips|prediction|pick|best pick)\s*[:\-]?\s*(?:draw|x)\b',s):
        out.add('D')

    if re.search(r'\b(?:tip|tips|prediction|pick|best pick)\s*[:\-]?\s*1x\b',s):
        out.add('DC1X')
    if re.search(r'\b(?:tip|tips|prediction|pick|best pick)\s*[:\-]?\s*x2\b',s):
        out.add('DCX2')
    if re.search(r'\b(?:tip|tips|prediction|pick|best pick)\s*[:\-]?\s*12\b',s):
        out.add('DC12')

    if re.search(r'\bover\s*1[.,]5\b|\bover\s*1\.5\b',s): out.add('O15')
    if re.search(r'\bunder\s*1[.,]5\b|\bunder\s*1\.5\b',s): out.add('U15')
    if re.search(r'\bover\s*2[.,]5\b|\bover\s*2\.5\b',s): out.add('O25')
    if re.search(r'\bunder\s*2[.,]5\b|\bunder\s*2\.5\b',s): out.add('U25')

    if re.search(r'\b(btts|bts|both teams to score)\s*[-:]?\s*(yes|y)?\b|\bgoal goal\b|\bgg\b',s):
        out.add('GG')
    if re.search(r'\b(btts|both teams to score)\s*[-:]?\s*(no|n)\b|\bno goal\b|\bng\b',s):
        out.add('NG')

    return out

def source_vote_for_record(signals, record: dict) -> float:
    market=str(record.get('market_name') or '')
    outcome=str(record.get('selection_column') or '')

    if not signals:
        return 0.0

    if market=='1X2':
        if outcome=='1':
            if 'H' in signals: return 1.0
            if 'A' in signals or 'D' in signals: return -1.0
        if outcome=='2':
            if 'A' in signals: return 1.0
            if 'H' in signals or 'D' in signals: return -1.0
        if outcome=='X':
            if 'D' in signals: return 1.0
            if 'H' in signals or 'A' in signals: return -1.0

    if market=='Doppia chance':
        if outcome=='1X':
            if 'DC1X' in signals: return 1.15
            if 'H' in signals or 'D' in signals: return .75
            if 'A' in signals or 'DCX2' in signals: return -1.0
        if outcome=='X2':
            if 'DCX2' in signals: return 1.15
            if 'A' in signals or 'D' in signals: return .75
            if 'H' in signals or 'DC1X' in signals: return -1.0
        if outcome=='12':
            if 'DC12' in signals: return 1.10
            if 'H' in signals or 'A' in signals: return .55
            if 'D' in signals: return -1.0

    if market=='Over/Under':
        if outcome=='Over 1.5':
            if 'O15' in signals: return 1.0
            if 'U15' in signals: return -1.0
            if 'O25' in signals: return .60
        if outcome=='Over 2.5':
            if 'O25' in signals: return 1.0
            if 'U25' in signals: return -1.0
        if outcome=='Under 2.5':
            if 'U25' in signals: return 1.0
            if 'O25' in signals: return -1.0

    if market=='Gol/No Gol':
        if outcome=='GG':
            if 'GG' in signals: return 1.0
            if 'NG' in signals: return -1.0
        if outcome=='NG':
            if 'NG' in signals: return 1.0
            if 'GG' in signals: return -1.0

    return 0.0

def load_multi_source_pages_v6():
    headers={
        'User-Agent':'Mozilla/5.0 StepByStep/1.0',
        'Accept':'text/plain,text/html,*/*',
    }

    def one(source):
        errors=[]
        for url in source.get('urls') or []:
            try:
                response=requests.get(url,headers=headers,timeout=18)
                response.raise_for_status()
                page_text=source_plain(response.text)
                if len(page_text)<300:
                    raise RuntimeError('pagina troppo corta')
                return {
                    'id':source['id'],
                    'kind':source['kind'],
                    'weight':float(source['weight']),
                    'text':page_text,
                }
            except Exception as exc:
                errors.append(str(exc))
        raise RuntimeError(' | '.join(errors) or 'nessun URL')

    pages=[]
    with ThreadPoolExecutor(max_workers=5) as pool:
        jobs={pool.submit(one,src):src for src in MULTI_SOURCE_BOARD_V6}
        for future in as_completed(jobs):
            src=jobs[future]
            try:
                pages.append(future.result())
            except Exception as exc:
                print(
                    f'WARN fonte {src["id"]}: {exc}',
                    file=sys.stderr
                )

    print(
        'MULTI SOURCE V6: '+
        str(len(pages))+'/'+str(len(MULTI_SOURCE_BOARD_V6))+
        ' fonti disponibili'
    )
    return pages

def enrich_records_with_multi_source_v6(records, pages):
    if not pages:
        for r in records:
            r['_externalSupportCount']=0
            r['_externalOpposeCount']=0
            r['_externalCovered']=0
            r['_externalNet']=0.0
            r['_externalTier']=0
            r['_statsSupport']=stats_support_for_record(r)
            r['_qualityTier']=quality_tier_for_record(r)
        return records

    cache={}

    for r in records:
        fixture_key=(
            canonical_team_key(r.get('home_team','')),
            canonical_team_key(r.get('away_team',''))
        )

        if fixture_key not in cache:
            cache[fixture_key]={}
            for page in pages:
                chunk=source_find_nearest_pair(
                    page['text'],
                    r.get('home_team',''),
                    r.get('away_team','')
                )
                cache[fixture_key][page['id']]=(
                    source_signals(page['kind'],chunk)
                    if chunk else set()
                )

        support_count=0
        oppose_count=0
        covered=0
        weighted_support=0.0
        weighted_oppose=0.0
        support_sources=[]

        for page in pages:
            signals=cache[fixture_key].get(page['id']) or set()
            vote=source_vote_for_record(signals,r)
            if vote==0:
                continue

            covered+=1
            weight=float(page['weight'])

            if vote>0:
                support_count+=1
                weighted_support+=weight*vote
                support_sources.append(page['id'])
            else:
                oppose_count+=1
                weighted_oppose+=weight*abs(vote)

        net=weighted_support-weighted_oppose

        if support_count>=3 and oppose_count==0:
            tier=4
        elif support_count>=2 and oppose_count==0:
            tier=3
        elif support_count>=1 and oppose_count==0:
            tier=2
        elif support_count>oppose_count and net>0:
            tier=1
        else:
            tier=0

        r['_externalSupportCount']=support_count
        r['_externalOpposeCount']=oppose_count
        r['_externalCovered']=covered
        r['_externalWeightedSupport']=round(weighted_support,3)
        r['_externalWeightedOppose']=round(weighted_oppose,3)
        r['_externalNet']=round(net,3)
        r['_externalTier']=tier
        r['_externalSupportSources']=support_sources
        r['_statsSupport']=stats_support_for_record(r)
        r['_qualityTier']=quality_tier_for_record(r)

    return records

def load_existing_board(day: str):
    path=OUT_DIR / f'{day}.json'
    if not path.exists():
        return None

    try:
        payload=json.loads(path.read_text(encoding='utf-8'))
        board=payload.get('board')
        roads=board.get('roads') if isinstance(board,dict) else None
        version=int(board.get('version') or 0) if isinstance(board,dict) else 0

        if (
            isinstance(roads,list) and
            len(roads)==5 and
            all(len(r.get('selections',[]))==2 for r in roads) and
            (
                version>=11
                if day>='2026-10-02'
                else version>=5
            ) and
            board.get('playability_source') in {
                'odd24-modal-betflag-v1',
                'odd24-modal-betflag-v2'
            } and
            all(
                all(
                    bool(sel.get('_betflagPlayable')) and
                    sel.get('_bookmaker')=='Betflag'
                    for sel in road.get('selections',[])
                )
                for road in roads
            )
        ):
            # BOARD FREEZE: una volta pubblicata per quel giorno non viene
            # rigenerata né cambiata. Questo vale anche per le board V5 già attive.
            return board
    except Exception:
        return None

    return None


def board_market_bonus(record: dict) -> float:
    market = record.get('market_name','')
    outcome = record.get('selection_column','')
    if market == 'Doppia chance': return 11.0
    if market == 'Over/Under' and outcome == 'Over 1.5': return 10.0
    if market == '1X2': return 6.0
    if market == 'Over/Under': return 4.0
    if market == 'Gol/No Gol': return 3.0
    return 0.0

def board_record_score(record: dict) -> float:
    odd=float(record.get('odd') or 0)
    conf=float(record.get('_marketConfidence') or .5)

    if odd<=1:
        return -999

    support=int(record.get('_externalSupportCount') or 0)
    oppose=int(record.get('_externalOpposeCount') or 0)
    covered=int(record.get('_externalCovered') or 0)
    net=float(record.get('_externalNet') or 0)
    tier=int(record.get('_externalTier') or 0)
    quality=int(record.get('_qualityTier') or 0)
    validated90=int(record.get('_validated90SupportCount') or 0)
    validated_rate=float(record.get('_validated90BestHistoricalRate') or 0)
    stats_support=float(record.get('_statsSupport') or 0)
    providers=int(record.get('_providerCount') or 0)
    spread=float(record.get('_marketSpreadPct') or 9)

    base=(
        conf*42 +
        (1/odd)*26 +
        board_market_bonus(record) -
        max(0,odd-1.35)*11 +
        quality*16 +
        validated90*24 +
        max(0,validated_rate-90)*3 +
        stats_support*12 +
        min(providers,10)*.75 -
        max(0,spread-.08)*35
    )

    consensus=(
        support*7.5 -
        oppose*11.0 +
        min(covered,4)*1.1 +
        max(-2.5,min(3.5,net))*2.3
    )

    tier_bonus={
        4:18.0,
        3:12.0,
        2:6.5,
        1:2.5,
        0:0.0,
    }.get(tier,0.0)

    return base+consensus+tier_bonus

def build_canonical_board(records, day: str, now: datetime):
    # Qui arrivano SOLO selezioni confermate nel market-modal Odd24
    # con riga operatore BETFLAG e quota Betflag reale.
    verified_records=[
        r for r in records
        if bool(r.get('_betflagPlayable')) and
        r.get('_bookmaker')=='Betflag'
    ]
    playability_source='odd24-modal-betflag-v2'

    print(
        f'PLAYABLE {day}: {len(verified_records)} selezioni '
        f'fonte={playability_source}'
    )

    future=[]
    cutoff = now + timedelta(minutes=30)

    for r in verified_records:
        try:
            kickoff = datetime.fromisoformat(str(r.get('event_time','')))
            odd = float(r.get('odd') or 0)
        except Exception:
            continue
        if kickoff < cutoff:
            continue
        if odd < 1.05 or odd > 1.52:
            continue
        if r.get('selection_column') in ('X','Under 1.5'):
            continue

        support=int(r.get('_externalSupportCount') or 0)
        oppose=int(r.get('_externalOpposeCount') or 0)
        covered=int(r.get('_externalCovered') or 0)
        quality=int(r.get('_qualityTier') or 0)
        validated90=int(r.get('_validated90SupportCount') or 0)

        # V11 STRICT: ogni singola selezione deve essere coperta
        # da almeno una fonte con track record pubblico >=90% nello
        # stesso mercato/fascia. Nessun fallback basato solo sulla quota.
        if covered>0 and oppose>support:
            continue

        if validated90 < 1:
            continue

        # Anche con fonte >=90%, il mercato deve essere realmente
        # disponibile su Betflag e non presentare una forte anomalia
        # rispetto agli altri operatori.
        providers=int(r.get('_providerCount') or 0)
        spread=float(r.get('_marketSpreadPct') or 9)
        deviation=float(r.get('_betflagDeviationPct') or 9)

        if providers < 5 or spread > .18 or deviation > .10:
            continue

        # quality_tier_for_record assegna Tier 4 ai validated90
        # solo se il controllo bookmaker è coerente.
        if quality < 4:
            continue

        future.append(r)

    quality_counts={}
    approval_counts={}
    for _r in future:
        _q=int(_r.get('_qualityTier') or 0)
        quality_counts[_q]=quality_counts.get(_q,0)+1
        _ap=(
            'validated90'
            if int(_r.get('_validated90SupportCount') or 0)>0
            else str(_r.get('_approvalPathOverride') or 'strong-quality-gate')
        )
        approval_counts[_ap]=approval_counts.get(_ap,0)+1

    print(
        f'V11 STRICT CANDIDATES {day}: {len(future)} '
        f'quality={quality_counts} approval={approval_counts}'
    )

    future.sort(key=lambda r:(
        str(r.get('event_time','')),
        canonical_team_key(r.get('home_team','')),
        canonical_team_key(r.get('away_team','')),
        str(r.get('market_name','')),
        str(r.get('selection_column','')),
        float(r.get('odd') or 0)
    ))

    groups=[]
    for r in future:
        found=None
        for g in groups:
            if same_fixture_record(g['rep'],r):
                found=g
                break
        if found is None:
            found={'rep':r,'records':[],'gid':len(groups)}
            groups.append(found)
        found['records'].append(r)

    pool=[]
    for g in groups:
        ranked=sorted(
            g['records'],
            key=lambda r:(-board_record_score(r),float(r.get('odd') or 99),str(r.get('selection_column','')))
        )[:3]
        for r in ranked:
            pool.append({'record':r,'score':board_record_score(r),'gid':g['gid']})

    pool.sort(key=lambda x:(-x['score'],x['gid'],str(x['record'].get('market_name','')),str(x['record'].get('selection_column',''))))

    pairs=[]
    for i,a in enumerate(pool):
        for b in pool[i+1:]:
            if a['gid'] == b['gid']:
                continue
            total = float(a['record']['odd']) * float(b['record']['odd'])
            # Quota strada richiesta: 1.50 reale arrotondata a due decimali.
            # Non allarghiamo il range per riempire a forza le 5 strade.
            if round(total,2) != 1.50:
                continue
            same_league=(
                a['record'].get('country') == b['record'].get('country') and
                a['record'].get('league_name') == b['record'].get('league_name')
            )
            tier_a=int(a['record'].get('_externalTier') or 0)
            tier_b=int(b['record'].get('_externalTier') or 0)
            both_supported=(
                int(a['record'].get('_externalSupportCount') or 0)>0 and
                int(b['record'].get('_externalSupportCount') or 0)>0
            )

            quality_a=int(a['record'].get('_qualityTier') or 0)
            quality_b=int(b['record'].get('_qualityTier') or 0)
            support_a=int(a['record'].get('_externalSupportCount') or 0)
            support_b=int(b['record'].get('_externalSupportCount') or 0)

            score=(
                a['score']+
                b['score']-
                abs(total-1.50)*120-
                (2.0 if same_league else 0)+
                min(quality_a,quality_b)*8.0+
                min(tier_a,tier_b)*3.0+
                min(support_a,support_b)*4.0+
                (6.0 if both_supported else 0.0)
            )

            pairs.append({'a':a,'b':b,'total':total,'score':score})

    print(f'V11 EXACT150 PAIRS {day}: {len(pairs)}')

    pairs.sort(key=lambda p:(
        -p['score'],
        abs(p['total']-1.50),
        p['a']['gid'],p['b']['gid']
    ))

    chosen=[]
    used=set()
    league_usage={}

    # Primo passaggio: qualità + diversificazione.
    for p in pairs:
        if p['a']['gid'] in used or p['b']['gid'] in used:
            continue

        ra=p['a']['record']
        rb=p['b']['record']
        ka=(str(ra.get('country','')),str(ra.get('league_name','')))
        kb=(str(rb.get('country','')),str(rb.get('league_name','')))

        # Non più di due selezioni dello stesso campionato nella board,
        # quando il pool consente di mantenere 5 strade complete.
        if league_usage.get(ka,0)>=2 or league_usage.get(kb,0)>=2:
            continue

        chosen.append(p)
        used.add(p['a']['gid'])
        used.add(p['b']['gid'])
        league_usage[ka]=league_usage.get(ka,0)+1
        league_usage[kb]=league_usage.get(kb,0)+1

        if len(chosen)==5:
            break

    # Secondo passaggio: se la diversificazione impedisce di arrivare a 5,
    # rilassiamo SOLO il limite campionato, mai i filtri di qualità né quota 1.50.
    if len(chosen)<5:
        for p in pairs:
            if p in chosen:
                continue
            if p['a']['gid'] in used or p['b']['gid'] in used:
                continue

            chosen.append(p)
            used.add(p['a']['gid'])
            used.add(p['b']['gid'])

            if len(chosen)==5:
                break

    if len(chosen)<5:
        return None

    roads=[]
    for road,p in enumerate(chosen,1):
        selections=[]
        for item in (p['a'],p['b']):
            r=item['record']
            selections.append({
                'home_team':r.get('home_team'),
                'away_team':r.get('away_team'),
                'event_time':r.get('event_time'),
                'event_id':r.get('event_id'),
                'league_name':r.get('league_name'),
                'country':r.get('country'),
                'market_name':r.get('market_name'),
                'selection_column':r.get('selection_column'),
                'odd':r.get('odd'),
                '_marketConfidence':r.get('_marketConfidence'),
                '_serverFeed':True,
                '_betflagPlayable':bool(r.get('_betflagPlayable')),
                '_bookmaker':'Betflag',
                '_betflagOdd':r.get('_betflagOdd'),
                '_betflagCheckedAt':r.get('_betflagCheckedAt'),
                '_playabilitySource':'odd24-modal-betflag-v2',
                '_externalSupportCount':int(r.get('_externalSupportCount') or 0),
                '_externalOpposeCount':int(r.get('_externalOpposeCount') or 0),
                '_externalCovered':int(r.get('_externalCovered') or 0),
                '_externalNet':float(r.get('_externalNet') or 0),
                '_externalTier':int(r.get('_externalTier') or 0),
                '_statsSupport':float(r.get('_statsSupport') or 0),
                '_qualityTier':int(r.get('_qualityTier') or 0),
                '_providerCount':int(r.get('_providerCount') or 0),
                '_marketMedianOdd':r.get('_marketMedianOdd'),
                '_marketSpreadPct':r.get('_marketSpreadPct'),
                '_betflagDeviationPct':r.get('_betflagDeviationPct'),
                '_validated90SupportCount':int(r.get('_validated90SupportCount') or 0),
                '_validated90Sources':r.get('_validated90Sources') or [],
                '_validated90Details':r.get('_validated90Details') or [],
                '_validated90BestHistoricalRate':float(r.get('_validated90BestHistoricalRate') or 0),
                '_approvalPath':(
                    'validated90'
                    if int(r.get('_validated90SupportCount') or 0)>0
                    else (
                        str(r.get('_approvalPathOverride') or '')
                        or 'strong-quality-gate'
                    )
                ),
            })
        roads.append({
            'road':road,
            'total_odds':round(p['total'],2),
            'selections':selections,
        })

    return {
        'version':11,
        'date':day,
        'generated_at':now.isoformat(),
        'locked':True,
        'playability_source':playability_source,
        'playability_checked_at':now.isoformat(),
        'selection_engine':'strict-validated90-exact150-v11',
        'quality_policy':'every_leg_validated90_and_exact150',
        'roads':roads,
    }

def build_day(day: str, make_latest: bool):
    rows=chrome_rows(day)
    print(f'DOM rows {day}: {len(rows)}')

    records=unique(parse_rows(rows,day))
    records,stat_fixtures,stat_records=enrich_records_with_stats(records,day)
    c=counts(records)

    if len(records)<20 or not all(c[k]>0 for k in c):
        raise RuntimeError(f'{day}: feed incompleto: {len(records)} record, {c}')

    now=datetime.now(ROME)
    board=load_existing_board(day)

    # MIGRAZIONE UNA-TANTUM V11: dal 02/10/2026 in poi una board
    # costruita con fallback non-90% viene rigenerata una sola volta.
    # La prima board V11 completa viene poi congelata e resta identica
    # per tutti per l'intera giornata.
    if (
        board is not None and
        day >= '2026-10-02' and
        int(board.get('version') or 0) < 11
    ):
        print(
            f'REBUILD TO V11 {day}: v{board.get("version")} -> V10',
            file=sys.stderr
        )
        board=None

    if board is not None:
        print(
            f'BOARD FREEZE {day}: v{board.get("version")} '
            f'{board.get("selection_engine","legacy-v5")} · invariata'
        )
    else:
        pages=load_multi_source_pages_v6()
        verified=verify_records_on_odd24_betflag(records,day,now)
        verified=enrich_records_with_validated90_v9(verified,day)
        verified=enrich_records_with_multi_source_v6(verified,pages)
        board=build_canonical_board(verified,day,now)

        if board is None:
            print(
                f'WARN {day}: non ci sono 10 selezioni con fonte >=90% '
                'verificata + Betflag + combinazione esatta quota 1.50',
                file=sys.stderr
            )

    payload={
        'date':day,
        'updated_at':now.isoformat(),
        'timezone':'Europe/Rome',
        'source_ok':True,
        'counts':c,
        'stats_coverage':{'fixtures':stat_fixtures,'records':stat_records},
        'board':board,
        'records':records,
    }

    OUT_DIR.mkdir(parents=True,exist_ok=True)
    data=json.dumps(payload,ensure_ascii=False,separators=(',',':'))
    (OUT_DIR/f'{day}.json').write_text(data,encoding='utf-8')

    if make_latest:
        (OUT_DIR/'latest.json').write_text(data,encoding='utf-8')

    print(
        f'OK {day}: {len(records)} records {c} | '
        f'stats fixtures={stat_fixtures} records={stat_records}'
    )


def main():
    now=datetime.now(ROME)
    today=now.date()
    tomorrow=today+timedelta(days=1)

    errors=[]
    built=0

    # Prepara sempre sia oggi sia domani.
    # Così allo scoccare della mezzanotte il file del nuovo giorno esiste già.
    for d, make_latest in ((today, True), (tomorrow, False)):
        day=d.isoformat()
        try:
            build_day(day, make_latest)
            built+=1
        except Exception as exc:
            errors.append(f'{day}: {exc}')
            print(f'WARN {day}: {exc}',file=sys.stderr)

    if built==0:
        raise RuntimeError('nessun feed generato: ' + ' | '.join(errors))

if __name__=='__main__':
    try:
        main()
    except Exception as exc:
        print(f'ERROR: {exc}',file=sys.stderr)
        raise
