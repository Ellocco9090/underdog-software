from __future__ import annotations

import json
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

ROME = ZoneInfo('Europe/Rome')
OUT_DIR = Path(__file__).resolve().parent
UA = 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/126 Safari/537.36'
MONTHS = {1:'gen',2:'feb',3:'mar',4:'apr',5:'mag',6:'giu',7:'lug',8:'ago',9:'set',10:'ott',11:'nov',12:'dic'}

def get_text(url: str, timeout: int = 35) -> str:
    r = requests.get(url, headers={
        'User-Agent': UA,
        'Accept-Language': 'it-IT,it;q=0.9,en;q=0.6',
        'Cache-Control': 'no-cache',
    }, timeout=timeout)
    r.raise_for_status()
    return r.text

def fetch_source(day: str) -> tuple[str, str]:
    stamp = int(time.time())
    # Prima la pagina diretta: è la più veloce e contiene il palinsesto corrente.
    # Jina resta solo come fallback.
    urls = [
        f'https://odd24.io/odds?_cb={stamp}',
        f'https://www.odd24.io/odds?_cb={stamp}',
        f'https://odd24.io/odds?dates={day}&days=1&markets=PRINCIPALI&minAgioPct=0&pageSize=1000&providers=ALL&_cb={stamp}',
        f'https://r.jina.ai/http://odd24.io/odds?_cb={stamp}',
    ]
    last = None
    for url in urls:
        try:
            text = get_text(url, timeout=18)
            if 'Doppia Chance' in text and ('GG / NG' in text or 'GG/NG' in text) and '1X2' in text:
                return text, url
            last = RuntimeError('pagina senza mercati principali')
        except Exception as exc:
            last = exc
    raise last or RuntimeError('nessuna sorgente disponibile')

def clean(s: str) -> str:
    return re.sub(r'\s+', ' ', s.replace('\xa0',' ')).strip()

def day_ok(day_num: str | None, mon: str | None, day: str) -> bool:
    if not day_num or not mon:
        return True
    _, mo, d = map(int, day.split('-'))
    return int(day_num) == d and mon.lower()[:3] == MONTHS[mo]

def parse_time(line: str, day: str) -> str | None:
    value = clean(line.replace('**','')).lstrip('#').strip()
    m = re.match(r'^(\d{1,2}:\d{2})(?:\s+(\d{1,2})\s+([A-Za-zÀ-ÿ]{3}))?\b', value, re.I)
    if not m:
        return None
    if not day_ok(m.group(2), m.group(3), day):
        return None
    return m.group(1)

def kickoff_iso(day: str, hhmm: str) -> str:
    y, m, d = map(int, day.split('-'))
    hh, mm = map(int, hhmm.split(':'))
    return datetime(y,m,d,hh,mm,tzinfo=ROME).isoformat()

def split_region(line: str) -> tuple[str,str]:
    value = clean(line).lstrip('#').strip()
    if ' - ' in value:
        a,b = value.split(' - ',1)
        return a.strip() or '—', b.strip() or '—'
    return '—', value or '—'

def split_teams(line: str) -> tuple[str,str] | None:
    value = clean(line.replace('**','')).lstrip('#').strip()
    if ' - ' not in value:
        return None
    a,b = value.split(' - ',1)
    a,b = a.strip(), b.strip()
    return (a,b) if a and b else None

def odd(value):
    value = clean(str(value)).replace('▲','').replace('▼','').replace(',','.')
    value = re.sub(r'[^0-9.]','', value)
    try:
        n = float(value)
    except Exception:
        return None
    return n if 1.0 < n < 20 else None

def add_record(out, base, market, outcome, value, confidence):
    n = odd(value)
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

def parse_detail_blocks(text: str, day: str) -> list[dict]:
    raw_lines = text.replace('\r','').split('\n')
    out = []
    i = 0

    while i < len(raw_lines):
        raw = raw_lines[i]
        # IMPORTANT: ignora le righe della tabella. I blocchi dettaglio hanno il tempo da solo.
        if '|' in raw:
            i += 1
            continue

        hhmm = parse_time(raw, day)
        if not hhmm:
            i += 1
            continue

        j = i + 1
        while j < len(raw_lines) and not clean(raw_lines[j]):
            j += 1
        if j >= len(raw_lines):
            break
        region_line = raw_lines[j]

        k = j + 1
        while k < len(raw_lines) and not clean(raw_lines[k]):
            k += 1
        if k >= len(raw_lines):
            break

        # la riga squadre nel mirror Jina inizia con ##
        if not clean(raw_lines[k]).startswith('##'):
            i += 1
            continue

        teams = split_teams(raw_lines[k])
        if not teams:
            i += 1
            continue

        country, league = split_region(region_line)
        home, away = teams

        z = k + 1
        while z < len(raw_lines):
            candidate = raw_lines[z]
            if '|' not in candidate and parse_time(candidate, day):
                break
            z += 1

        block = '\n'.join(clean(x.replace('**','')) for x in raw_lines[k+1:z] if clean(x))
        base = {
            'home': home,
            'away': away,
            'country': country,
            'league': league,
            'kickoff': kickoff_iso(day, hhmm),
            'id': f'{day}|{hhmm}|{home}|{away}',
        }

        m = re.search(r'1X2\s+1\s+([0-9]+(?:[.,][0-9]+)?)\s+.*?\sX\s+([0-9]+(?:[.,][0-9]+)?)\s+.*?\s2\s+([0-9]+(?:[.,][0-9]+)?)\s+', block, re.I|re.S)
        if m:
            add_record(out,base,'1X2','1',m.group(1),.68)
            add_record(out,base,'1X2','2',m.group(3),.68)

        m = re.search(r'Doppia\s+Chance\s+1X\s+([0-9]+(?:[.,][0-9]+)?)\s+.*?\s12\s+([0-9]+(?:[.,][0-9]+)?)\s+.*?\sX2\s+([0-9]+(?:[.,][0-9]+)?)\s+', block, re.I|re.S)
        if m:
            add_record(out,base,'Doppia chance','1X',m.group(1),.78)
            add_record(out,base,'Doppia chance','12',m.group(2),.74)
            add_record(out,base,'Doppia chance','X2',m.group(3),.78)

        m = re.search(r'Over/Under\s+2[.,]5\s+Over\s*\(2[.,]5\)\s+([0-9]+(?:[.,][0-9]+)?)\s+.*?Under\s*\(2[.,]5\)\s+([0-9]+(?:[.,][0-9]+)?)\s+', block, re.I|re.S)
        if m:
            add_record(out,base,'Over/Under','Over 2.5',m.group(1),.72)
            add_record(out,base,'Over/Under','Under 2.5',m.group(2),.72)

        m = re.search(r'GG\s*/\s*NG\s+GG\s+([0-9]+(?:[.,][0-9]+)?)\s+.*?\sNG\s+([0-9]+(?:[.,][0-9]+)?)\s+', block, re.I|re.S)
        if m:
            add_record(out,base,'Gol/No Gol','GG',m.group(1),.70)
            add_record(out,base,'Gol/No Gol','NG',m.group(2),.70)

        m = re.search(r'Over/Under\s+1[.,]5\s+Over\s*\(1[.,]5\)\s+([0-9]+(?:[.,][0-9]+)?)\s+.*?Under\s*\(1[.,]5\)\s+([0-9]+(?:[.,][0-9]+)?)\s+', block, re.I|re.S)
        if m:
            add_record(out,base,'Over/Under','Over 1.5',m.group(1),.78)
            add_record(out,base,'Over/Under','Under 1.5',m.group(2),.66)

        i = max(z, i+1)

    return out

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

def split_event_cell(cell: str) -> tuple[str,str,str,str] | None:
    value = clean(cell)
    if ' - ' not in value:
        return None
    country, rest = value.split(' - ',1)
    if ' - ' not in rest:
        return None
    left, away = rest.rsplit(' - ',1)
    country, left, away = country.strip(), left.strip(), away.strip()

    for prefix in LEAGUE_PREFIXES:
        if left.lower().startswith(prefix.lower() + ' '):
            return country or '—', prefix, left[len(prefix):].strip(), away

    markers = [' Fc ',' Fk ',' Acs ',' Cs ',' Csm ',' Afc ',' Nk ',' Sc ',' Real ',' Dinamo ',' Dynamo ',' Al ',' El ']
    cut = None
    for marker in markers:
        idx = (' '+left+' ').lower().find(marker.lower())
        if idx > 0:
            real_idx = max(0, idx-1)
            if cut is None or real_idx < cut:
                cut = real_idx
    if cut is not None and cut > 0:
        return country or '—', left[:cut].strip() or '—', left[cut:].strip(), away

    # Se non sappiamo separare bene campionato e squadra, non inventiamo il campionato.
    return country or '—', '—', left, away

def parse_table(text: str, day: str) -> list[dict]:
    out = []
    for raw in text.replace('\r','').split('\n'):
        line = clean(raw.replace('**',''))
        if '|' not in line:
            continue
        hhmm = parse_time(line.split('|',1)[0], day)
        if not hhmm:
            continue

        cells = [clean(x) for x in line.split('|')]
        while cells and not cells[0]:
            cells.pop(0)
        while cells and not cells[-1]:
            cells.pop()
        if len(cells) < 18:
            continue

        event = split_event_cell(cells[1])
        if not event:
            continue
        country, league, home, away = event

        base = {
            'home':home,'away':away,'country':country,'league':league,
            'kickoff':kickoff_iso(day,hhmm),
            'id':f'{day}|{hhmm}|{home}|{away}'
        }

        # Layout ODD24 attuale:
        # Ora|Evento|1|X|2|%|1X|12|X2|%|O2.5|U2.5|%|GG|NG|%|O1.5|U1.5|%...
        add_record(out,base,'1X2','1',cells[2],.66)
        add_record(out,base,'1X2','2',cells[4],.66)
        add_record(out,base,'Doppia chance','1X',cells[6],.78)
        add_record(out,base,'Doppia chance','12',cells[7],.74)
        add_record(out,base,'Doppia chance','X2',cells[8],.78)
        add_record(out,base,'Over/Under','Over 2.5',cells[10],.72)
        add_record(out,base,'Over/Under','Under 2.5',cells[11],.72)
        add_record(out,base,'Gol/No Gol','GG',cells[13],.70)
        add_record(out,base,'Gol/No Gol','NG',cells[14],.70)
        add_record(out,base,'Over/Under','Over 1.5',cells[16],.78)
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
            result[r['market_name']] += 1
    return result

def main():
    now=datetime.now(ROME)
    day=now.strftime('%Y-%m-%d')
    source_text, source_url=fetch_source(day)

    # Prima prova i blocchi dettaglio (nomi campionato/squadre più precisi),
    # poi completa con la tabella principale.
    detail=parse_detail_blocks(source_text,day)
    table=parse_table(source_text,day)
    records=unique(detail+table)

    c=counts(records)
    complete=all(c[k]>0 for k in c)
    if not complete or len(records)<20:
        # salva un piccolo debug leggibile nel log
        print(source_text[:7000], file=sys.stderr)
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
