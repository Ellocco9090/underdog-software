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
    r = requests.get(url, headers={'User-Agent': UA, 'Accept-Language': 'it-IT,it;q=0.9,en;q=0.6'}, timeout=timeout)
    r.raise_for_status()
    return r.text

def fetch_source(day: str) -> tuple[str, str]:
    stamp = int(time.time())
    urls = [
        f'https://r.jina.ai/http://odd24.io/odds?dates={day}&days=1&markets=PRINCIPALI&minAgioPct=0&pageSize=1000&providers=ALL&_cb={stamp}',
        f'https://r.jina.ai/http://www.odd24.io/odds?dates={day}&days=1&markets=PRINCIPALI&minAgioPct=0&pageSize=1000&providers=ALL&_cb={stamp}',
        f'https://r.jina.ai/http://odd24.io/odds?_cb={stamp}',
    ]
    last = None
    for url in urls:
        try:
            text = get_text(url)
            if 'Doppia Chance' in text and 'GG / NG' in text and '1X2' in text:
                return text, url
            last = RuntimeError('pagina senza mercati principali')
        except Exception as exc:
            last = exc
    raise last or RuntimeError('nessuna sorgente disponibile')

def clean(s: str) -> str:
    return re.sub(r'\s+', ' ', s.replace('\xa0',' ')).strip()

def parse_time_line(line: str, day: str) -> str | None:
    line = clean(line).replace('**','').lstrip('#').strip()
    m = re.match(r'^(\d{1,2}:\d{2})(?:\s+(\d{1,2})\s+([A-Za-zÀ-ÿ]{3}))?\b', line, re.I)
    if not m:
        return None
    if m.group(2) and m.group(3):
        y, mo, d = map(int, day.split('-'))
        mon = MONTHS.get(mo)
        if int(m.group(2)) != d or m.group(3).lower()[:3] != mon:
            return None
    return m.group(1)

def kickoff_iso(day: str, hhmm: str) -> str:
    y, m, d = map(int, day.split('-'))
    hh, mm = map(int, hhmm.split(':'))
    dt = datetime(y,m,d,hh,mm,tzinfo=ROME)
    return dt.isoformat()

def split_region(line: str) -> tuple[str, str]:
    line = clean(line).lstrip('#').strip()
    if ' - ' in line:
        country, league = line.split(' - ', 1)
        return country.strip() or '—', league.strip() or '—'
    return '—', line or '—'

def split_teams(line: str) -> tuple[str, str] | None:
    line = clean(line).replace('**','').lstrip('#').strip()
    if ' - ' not in line:
        return None
    home, away = line.split(' - ', 1)
    home, away = home.strip(), away.strip()
    return (home, away) if home and away else None

def first_match(pattern: str, block: str):
    return re.search(pattern, block, re.I | re.S)

def add_record(out, base, market, outcome, odd, confidence):
    try:
        odd = float(str(odd).replace(',','.'))
    except Exception:
        return
    if not (1.0 < odd < 20):
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
        'odd': round(odd, 3),
        '_marketConfidence': confidence,
        '_serverFeed': True,
    })

def parse_markdown(text: str, day: str) -> list[dict]:
    lines = [clean(x.replace('**','')) for x in text.replace('\r','').split('\n')]
    out = []
    i = 0
    while i < len(lines):
        hhmm = parse_time_line(lines[i], day)
        if not hhmm:
            i += 1
            continue

        region_idx = None
        teams_idx = None
        for j in range(i+1, min(len(lines), i+10)):
            if not lines[j]:
                continue
            if region_idx is None and not lines[j].startswith('#') and ' | ' not in lines[j] and not re.match(r'^(1X2|Doppia Chance|Over/Under|GG / NG)', lines[j], re.I):
                region_idx = j
                continue
            if split_teams(lines[j]) and (lines[j].startswith('#') or (region_idx is not None and j > region_idx)):
                teams_idx = j
                break

        if region_idx is None or teams_idx is None:
            i += 1
            continue

        teams = split_teams(lines[teams_idx])
        if not teams:
            i += 1
            continue
        home, away = teams
        country, league = split_region(lines[region_idx])

        k = teams_idx + 1
        while k < len(lines):
            if parse_time_line(lines[k], day):
                break
            k += 1
        block = '\n'.join(lines[teams_idx+1:k])

        base = {
            'home': home,
            'away': away,
            'country': country,
            'league': league,
            'kickoff': kickoff_iso(day, hhmm),
            'id': f'{day}|{hhmm}|{home}|{away}',
        }

        m = first_match(r'1X2\s*\n?\s*1\s+([0-9]+(?:[.,][0-9]+)?)\b.*?\sX\s+([0-9]+(?:[.,][0-9]+)?)\b.*?\s2\s+([0-9]+(?:[.,][0-9]+)?)\b', block)
        if m:
            add_record(out, base, '1X2', '1', m.group(1), .68)
            add_record(out, base, '1X2', '2', m.group(3), .68)

        m = first_match(r'Doppia\s+Chance\s*\n?\s*1X\s+([0-9]+(?:[.,][0-9]+)?)\b.*?\s12\s+([0-9]+(?:[.,][0-9]+)?)\b.*?\sX2\s+([0-9]+(?:[.,][0-9]+)?)\b', block)
        if m:
            add_record(out, base, 'Doppia chance', '1X', m.group(1), .78)
            add_record(out, base, 'Doppia chance', '12', m.group(2), .74)
            add_record(out, base, 'Doppia chance', 'X2', m.group(3), .78)

        m = first_match(r'Over/Under\s+2[.,]5\s*\n?\s*Over\s*\(2[.,]5\)\s+([0-9]+(?:[.,][0-9]+)?)\b.*?Under\s*\(2[.,]5\)\s+([0-9]+(?:[.,][0-9]+)?)\b', block)
        if m:
            add_record(out, base, 'Over/Under', 'Over 2.5', m.group(1), .72)
            add_record(out, base, 'Over/Under', 'Under 2.5', m.group(2), .72)

        m = first_match(r'GG\s*/\s*NG\s*\n?\s*GG\s+([0-9]+(?:[.,][0-9]+)?)\b.*?\sNG\s+([0-9]+(?:[.,][0-9]+)?)\b', block)
        if m:
            add_record(out, base, 'Gol/No Gol', 'GG', m.group(1), .70)
            add_record(out, base, 'Gol/No Gol', 'NG', m.group(2), .70)

        m = first_match(r'Over/Under\s+1[.,]5\s*\n?\s*Over\s*\(1[.,]5\)\s+([0-9]+(?:[.,][0-9]+)?)\b.*?Under\s*\(1[.,]5\)\s+([0-9]+(?:[.,][0-9]+)?)\b', block)
        if m:
            add_record(out, base, 'Over/Under', 'Over 1.5', m.group(1), .78)
            add_record(out, base, 'Over/Under', 'Under 1.5', m.group(2), .66)

        i = max(k, i+1)

    seen = set()
    unique = []
    for r in out:
        key = (r['event_id'], r['market_name'], r['selection_column'])
        if key in seen:
            continue
        seen.add(key)
        unique.append(r)
    return unique

def counts(records):
    result = {'1X2':0, 'Doppia chance':0, 'Over/Under':0, 'Gol/No Gol':0}
    for r in records:
        if r['market_name'] in result:
            result[r['market_name']] += 1
    return result

def main():
    now = datetime.now(ROME)
    day = now.strftime('%Y-%m-%d')
    source_text, source_url = fetch_source(day)
    records = parse_markdown(source_text, day)
    c = counts(records)
    complete = all(c[k] > 0 for k in c)
    if not complete or len(records) < 20:
        raise RuntimeError(f'feed incompleto: {len(records)} record, {c}')

    payload = {
        'date': day,
        'updated_at': now.isoformat(),
        'timezone': 'Europe/Rome',
        'source_ok': True,
        'counts': c,
        'records': records,
    }

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUT_DIR / 'latest.json').write_text(json.dumps(payload, ensure_ascii=False, separators=(',',':')), encoding='utf-8')
    (OUT_DIR / f'{day}.json').write_text(json.dumps(payload, ensure_ascii=False, separators=(',',':')), encoding='utf-8')
    print(f'OK {day}: {len(records)} records {c}')

if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        print(f'ERROR: {exc}', file=sys.stderr)
        raise
