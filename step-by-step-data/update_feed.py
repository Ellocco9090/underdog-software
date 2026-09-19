from __future__ import annotations

import json
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.support.ui import WebDriverWait

ROME = ZoneInfo('Europe/Rome')
OUT_DIR = Path(__file__).resolve().parent
MONTHS = {1:'gen',2:'feb',3:'mar',4:'apr',5:'mag',6:'giu',7:'lug',8:'ago',9:'set',10:'ott',11:'nov',12:'dic'}

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
    # La cella evento nel browser spesso mantiene:
    # "Paese - Campionato\nCasa - Trasferta"
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

    # Heuristica di emergenza: cerca l'inizio tipico del nome squadra.
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

def chrome_rows(day: str):
    stamp = int(time.time())
    url = (
        'https://odd24.io/odds'
        f'?dates={day}&days=1&markets=PRINCIPALI&minAgioPct=0'
        f'&pageSize=1000&providers=ALL&_cb={stamp}'
    )

    opts = Options()
    opts.add_argument('--headless=new')
    opts.add_argument('--no-sandbox')
    opts.add_argument('--disable-dev-shm-usage')
    opts.add_argument('--disable-gpu')
    opts.add_argument('--window-size=1600,1200')
    opts.add_argument('--lang=it-IT')
    opts.add_argument('--disable-blink-features=AutomationControlled')
    opts.set_capability('pageLoadStrategy','eager')

    driver = webdriver.Chrome(options=opts)
    try:
        driver.set_page_load_timeout(45)
        driver.get(url)

        def enough_rows(d):
            try:
                return d.execute_script("""
                    return [...document.querySelectorAll('tr')].filter(tr=>{
                      const cells=[...tr.querySelectorAll('td')];
                      return cells.length>=10 && /^\\s*\\d{1,2}:\\d{2}/.test(cells[0]?.innerText||'');
                    }).length;
                """) >= 5
            except Exception:
                return False

        WebDriverWait(driver,45,poll_frequency=1).until(enough_rows)

        rows = driver.execute_script("""
            return [...document.querySelectorAll('tr')]
              .map(tr=>[...tr.querySelectorAll('td')].map(td=>td.innerText||''))
              .filter(cells=>cells.length>=10 && /^\\s*\\d{1,2}:\\d{2}/.test(cells[0]||''));
        """)

        return rows
    finally:
        driver.quit()

def parse_rows(rows, day: str):
    out=[]
    debug=[]

    for cells in rows:
        if len(cells)<18:
            if len(debug)<3:
                debug.append(cells)
            continue

        hhmm=parse_time_text(cells[0],day)
        if not hhmm:
            continue

        event=parse_event_cell(cells[1])
        if not event:
            if len(debug)<3:
                debug.append(cells[:3])
            continue

        country,league,home,away=event
        base={
            'home':home,
            'away':away,
            'country':country,
            'league':league,
            'kickoff':kickoff_iso(day,hhmm),
            'id':f'{day}|{hhmm}|{home}|{away}',
        }

        # Layout attuale:
        # 0 Ora, 1 Evento,
        # 2/3/4 = 1X2, 5 payout,
        # 6/7/8 = DC, 9 payout,
        # 10/11 = OU2.5, 12 payout,
        # 13/14 = GG/NG, 15 payout,
        # 16/17 = OU1.5, 18 payout.
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

    if debug:
        print('DEBUG ROWS:',json.dumps(debug,ensure_ascii=False)[:4000],file=sys.stderr)
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

def main():
    now=datetime.now(ROME)
    day=now.strftime('%Y-%m-%d')

    rows=chrome_rows(day)
    print(f'DOM rows: {len(rows)}')

    records=unique(parse_rows(rows,day))
    c=counts(records)

    if len(records)<20 or not all(c[k]>0 for k in c):
        raise RuntimeError(f'feed incompleto: {len(records)} record, {c}')

    payload={
        'date':day,
        'updated_at':now.isoformat(),
        'timezone':'Europe/Rome',
        'source_ok':True,
        'counts':c,
        'records':records,
    }

    OUT_DIR.mkdir(parents=True,exist_ok=True)
    data=json.dumps(payload,ensure_ascii=False,separators=(',',':'))
    (OUT_DIR/'latest.json').write_text(data,encoding='utf-8')
    (OUT_DIR/f'{day}.json').write_text(data,encoding='utf-8')
    print(f'OK {day}: {len(records)} records {c}')

if __name__=='__main__':
    try:
        main()
    except Exception as exc:
        print(f'ERROR: {exc}',file=sys.stderr)
        raise
