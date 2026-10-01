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

import requests

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
    pm=re.search(r'__PROVIDER__=([^\\n]+)',raw_value)
    mm=re.search(r'__MODAL__=([^\\n]+)',raw_value)
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

def probe_odd24_betflag_modal(records):
    candidate=next(
        (
            r for r in records
            if r.get('_modalUrl') and
            1.05 <= float(r.get('odd') or 0) <= 1.60 and
            r.get('selection_column') not in ('X','Under 1.5')
        ),
        None
    )
    if not candidate:
        return
    try:
        url='https://odd24.io'+str(candidate['_modalUrl'])
        response=requests.get(
            url,
            timeout=15,
            headers={'User-Agent':'Mozilla/5.0 StepByStep/1.0','Accept':'text/html,*/*'}
        )
        print(
            'ODD24 MODAL PROBE status='+str(response.status_code)+
            ' url='+url+
            ' body='+clean(response.text[:5000]),
            file=sys.stderr
        )
    except Exception as exc:
        print(f'WARN modal probe: {exc}',file=sys.stderr)

def load_existing_board(day: str):
    path = OUT_DIR / f'{day}.json'
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding='utf-8'))
        board = payload.get('board')
        roads = board.get('roads') if isinstance(board,dict) else None
        if (
            isinstance(roads,list) and
            len(roads) == 5 and
            all(len(r.get('selections',[])) == 2 for r in roads) and
            int(board.get('version') or 0) >= 4 and
            board.get('playability_source') in {
                'betflag-pregame-v1',
                'odd24-betflag-best-v1',
                'odd24-modal-probe-fallback'
            } and
            all(
                all(bool(sel.get('_betflagPlayable')) for sel in road.get('selections',[]))
                for road in roads
            )
        ):
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
    odd = float(record.get('odd') or 0)
    conf = float(record.get('_marketConfidence') or .5)
    if odd <= 1:
        return -999
    return conf*55 + (1/odd)*35 + board_market_bonus(record) - max(0,odd-1.35)*12

def build_canonical_board(records, day: str, now: datetime, betflag_fixtures):
    # La board è costruita UNA SOLA VOLTA e poi conservata nei run successivi.
    # Tutti i dispositivi scaricano quindi esattamente le stesse 5 strade.
    # Prima condizione: la partita + il mercato + il segno devono
    # essere realmente presenti nel palinsesto Betflag.
    if betflag_fixtures:
        verified_records=filter_records_playable_on_betflag(
            records,
            betflag_fixtures
        )
        playability_source='betflag-pregame-v1'
    else:
        betflag_cells=[
            r for r in records
            if bool(r.get('_betflagCell'))
        ]
        if betflag_cells:
            verified_records=betflag_cells
            playability_source='odd24-betflag-best-v1'
        else:
            verified_records=list(records)
            playability_source='odd24-modal-probe-fallback'

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
        future.append(r)

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
            if total < 1.50 or total > 1.60:
                continue
            same_league=(
                a['record'].get('country') == b['record'].get('country') and
                a['record'].get('league_name') == b['record'].get('league_name')
            )
            score=a['score']+b['score']-abs(total-1.50)*45-(1.2 if same_league else 0)
            pairs.append({'a':a,'b':b,'total':total,'score':score})

    pairs.sort(key=lambda p:(
        -p['score'],
        abs(p['total']-1.50),
        p['a']['gid'],p['b']['gid']
    ))

    chosen=[]
    used=set()
    for p in pairs:
        if p['a']['gid'] in used or p['b']['gid'] in used:
            continue
        chosen.append(p)
        used.add(p['a']['gid'])
        used.add(p['b']['gid'])
        if len(chosen) == 5:
            break

    if len(chosen) < 5:
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
                '_betflagPlayable':bool(
                    r.get('_betflagPlayable') or
                    r.get('_betflagCell')
                ),
                '_bookmaker':'Betflag',
                '_betflagEventId':r.get('_betflagEventId'),
                '_betflagTournamentId':r.get('_betflagTournamentId'),
                '_betflagCheckedAt':r.get('_betflagCheckedAt'),
            })
        roads.append({
            'road':road,
            'total_odds':round(p['total'],2),
            'selections':selections,
        })

    return {
        'version':4,
        'date':day,
        'generated_at':now.isoformat(),
        'locked':True,
        'playability_source':playability_source,
        'playability_checked_at':now.isoformat(),
        'roads':roads,
    }

def build_day(day: str, make_latest: bool):
    rows=chrome_rows(day)
    print(f'DOM rows {day}: {len(rows)}')

    records=unique(parse_rows(rows,day))
    probe_odd24_betflag_modal(records)
    records,stat_fixtures,stat_records=enrich_records_with_stats(records,day)
    c=counts(records)

    if len(records)<20 or not all(c[k]>0 for k in c):
        raise RuntimeError(f'{day}: feed incompleto: {len(records)} record, {c}')

    now=datetime.now(ROME)

    # Board centrale anche per DOMANI, ma SOLO con eventi/mercati
    # effettivamente giocabili su Betflag.
    board=load_existing_board(day)

    if board is None:
        try:
            betflag_fixtures=fetch_betflag_playable(day)
            board=build_canonical_board(
                records,
                day,
                now,
                betflag_fixtures
            )
            if board is None:
                print(
                    f'WARN {day}: Betflag verificato ma non ci sono '
                    '10 selezioni compatibili per 5 strade',
                    file=sys.stderr
                )
        except Exception as exc:
            print(
                f'WARN Betflag board {day}: {exc}',
                file=sys.stderr
            )
            board=build_canonical_board(
                records,
                day,
                now,
                None
            )
            if board is None:
                print(
                    f'WARN {day}: meno di 10 selezioni Betflag '
                    'giocabili per costruire 5 strade',
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

    print(f'OK {day}: {len(records)} records {c} | stats fixtures={stat_fixtures} records={stat_records}')

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
