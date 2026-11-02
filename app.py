"""Periodically publish a fussball.de match schedule as an iCalendar feed."""
import hashlib
import json
import logging
import os
import re
import secrets
import threading
from datetime import datetime, timedelta, timezone, date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

from bs4 import BeautifulSoup
from icalendar import Calendar, Event

UTC = timezone.utc
BERLIN = ZoneInfo('Europe/Berlin')
LOG = logging.getLogger(__name__)


def parse_games(html):
    soup = BeautifulSoup(html, 'html.parser')
    groups = []
    for row in soup.select('tr'):
        if 'row-headline' in row.get('class', []):
            groups.append([])
        if groups:
            groups[-1].append(str(row))
    if html.strip() and not groups:
        raise ValueError('Unknown HTML format; existing calendar preserved')
    games = {}
    for group in groups:
        block = BeautifulSoup(''.join(group), 'html.parser')
        header = block.select_one('.row-headline').get_text(' ', strip=True)
        # Old dates remain in the schedule as redirects to the actual fixture.
        # They have a new date in the score cell and no detail link. Never let
        # these rows overwrite the actual fixture, regardless of row order.
        moved = block.select_one('.column-score .info-text')
        if moved and not block.select_one('.column-detail a[href]'):
            target_date = moved.get_text(' ', strip=True)
            if re.fullmatch(r'\d{2}\.\d{2}\.\d{4}\s+\d{2}:\d{2}', target_date):
                LOG.info('Skipped old rescheduled fixture: %s -> %s', header, target_date)
                continue
        match = re.search(r'(\d{2}\.\d{2}\.\d{4}) - (\d{2}:\d{2}) Uhr\s*\|\s*(.*)', header)
        clubs = block.select('.column-club .club-name')
        link = block.select_one('.column-detail a[href*="/-/spiel/"]')
        if link is None:
            link = block.select_one('.column-score a[href*="/-/spiel/"]')
        identity = re.search(r'/-/spiel/([A-Za-z0-9]+)', link['href']) if link else None
        # A bye is listed in the schedule without a kick-off time or match link.
        if not identity and re.search(r'\bspielfrei\b', block.get_text(' ', strip=True), re.I):
            continue
        if not match or len(clubs) != 2 or not identity:
            problems = []
            if not match:
                problems.append('date/time not recognized')
            if len(clubs) != 2:
                problems.append(f'expected 2 teams, found {len(clubs)}')
            if not identity:
                problems.append('missing match ID')
            excerpt = block.get_text(' ', strip=True)[:600]
            raise ValueError(
                f"Unreadable fixture ({'; '.join(problems)}). "
                f"Entry: {excerpt!r}. Update discarded; existing calendar preserved."
            )
        start = datetime.strptime(f'{match[1]} {match[2]}', '%d.%m.%Y %H:%M').replace(tzinfo=BERLIN)
        venue = block.select_one('.row-venue td[colspan]')
        status_text = ' '.join(node.get_text(' ', strip=True) for node in block.select('.column-score'))
        cancelled = bool(re.search(r'Absetzung|abgesetzt|abgesagt|Ausfall|ausgefallen', status_text, re.I))
        games[identity[1]] = {
            'start': start.astimezone(UTC).isoformat(),
            'summary': ' - '.join(node.get_text(' ', strip=True) for node in clubs),
            'description': match[3] + (f'\nStatus: {status_text}' if status_text and not any('\ue000' <= c <= '\uf8ff' for c in status_text) else ''),
            'location': venue.get_text(' ', strip=True) if venue else '',
            'url': link['href'],
            'status': 'CANCELLED' if cancelled else 'CONFIRMED',
        }
    return games


def fetch_games(team_id, start_date):
    games = {}
    for page in range(100):
        url = ('https://www.fussball.de/ajax.team.matchplan.loadmore/-/'
               f'datum-von/{start_date}/match-type/-1/mime-type/JSON/mode/PAGE/'
               f'show-venues/true/team-id/{team_id}/max/200/offset/{page * 200}')
        with urlopen(Request(url, headers={'User-Agent': 'FussballCalendar/1.0'}), timeout=45) as response:
            body = json.load(response)
        if body.get('success') is False or not isinstance(body.get('html'), str) or not isinstance(body.get('final'), bool):
            raise ValueError('Unexpected response from fussball.de')
        games.update(parse_games(body['html']))
        if body['final']:
            if not games:
                raise ValueError('Empty schedule; existing calendar preserved')
            return games
    raise ValueError('Page limit reached; incomplete update discarded')


def make_calendar(games, previous, name, duration, now):
    calendar = Calendar()
    calendar.add('prodid', '-//Football Calendar//EN')
    calendar.add('version', '2.0')
    calendar.add('x-wr-calname', name)
    state = {}
    for game_id, game in sorted(games.items()):
        old = previous.get(game_id)
        changed = old is None or old['game'] != game
        sequence = (old['sequence'] + 1 if old else 0) if changed else old['sequence']
        modified = now.isoformat() if changed else old['modified']
        state[game_id] = {'game': game, 'sequence': sequence, 'modified': modified}
        event = Event()
        event.add('uid', f'{game_id}@fussball.de')
        event.add('sequence', sequence)
        event.add('dtstamp', datetime.fromisoformat(modified))
        event.add('last-modified', datetime.fromisoformat(modified))
        start = datetime.fromisoformat(game['start'])
        event.add('dtstart', start)
        event.add('dtend', start + timedelta(minutes=duration))
        for key in ('summary', 'description', 'location', 'url', 'status'):
            event.add(key, game[key])
        calendar.add_component(event)
    return calendar.to_ical(), state


def refresh(config):
    path = config['path']
    previous = {}
    if path.exists():
        cached = json.loads(path.read_text())
        if cached.get('team') == config['team']:
            previous = cached['events']
    games = fetch_games(config['team'], config['from'])
    ics, state = make_calendar(games, previous, config['name'], config['duration'], datetime.now(UTC))
    snapshot = {'team': config['team'], 'updated': datetime.now(UTC).isoformat(),
                'ics': ics.decode(), 'events': state}
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(snapshot), encoding='utf-8')
    temporary.replace(path)
    LOG.info('Last successful fetch: %s (%d matches)', snapshot['updated'], len(games))


def worker(config):
    while True:
        try:
            refresh(config)
        except Exception:
            LOG.exception('Update failed; existing calendar preserved')
        threading.Event().wait(config['interval'])


def subscription_url(base_url, token):
    base = urlsplit(base_url)
    if (base.scheme != 'https' or not base.hostname or base.username or base.password
            or base.query or base.fragment or any(char.isspace() for char in base_url)):
        raise ValueError('CALENDAR_BASE_URL must be an HTTPS URL without credentials, query or fragment')
    return base_url.rstrip('/') + '/calendar.ics?token=' + token


def handler(config):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format, *args):
            # The subscription token is in the URL. Never log the request line.
            pass

        def do_HEAD(self):
            self.do_GET()

        def do_GET(self):
            url = urlsplit(self.path)
            route = url.path
            if route not in ('/calendar.ics', config.get('calendar_path', '/calendar.ics')):
                self.send_error(404)
                return
            tokens = parse_qs(url.query, keep_blank_values=True).get('token', [])
            if len(tokens) != 1 or not secrets.compare_digest(tokens[0], config['token']):
                self.send_error(404)
                return
            try:
                snapshot = json.loads(config['path'].read_text())
                if snapshot['team'] != config['team']:
                    raise ValueError('Cache belongs to another team')
            except (OSError, ValueError):
                self.send_error(503, 'Calendar not available yet')
                return
            payload = snapshot['ics'].encode()
            etag = '"' + hashlib.sha256(payload).hexdigest() + '"'
            self.send_response(304 if self.headers.get('If-None-Match') == etag else 200)
            self.send_header('ETag', etag)
            self.send_header('Cache-Control', 'private, no-store')
            self.send_header('Content-Type', 'text/calendar; charset=utf-8')
            self.send_header('Content-Length', str(len(payload)))
            self.end_headers()
            if self.command != 'HEAD' and self.headers.get('If-None-Match') != etag:
                self.wfile.write(payload)
    return Handler


def main():
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    team = os.environ['TEAM_ID']
    if not re.fullmatch(r'[A-Za-z0-9]+', team):
        raise ValueError('TEAM_ID must be an alphanumeric fussball.de ID')
    start = os.environ.get('START_DATE', '2026-07-01')
    date.fromisoformat(start)
    token = os.environ['CALENDAR_TOKEN']
    if len(token) < 32 or not re.fullmatch(r'[A-Za-z0-9_-]+', token):
        raise ValueError('CALENDAR_TOKEN must contain at least 32 URL-safe characters')
    config = {'team': team, 'from': start, 'name': os.getenv('CALENDAR_NAME', 'Football Schedule'),
              'token': token,
              'interval': int(os.getenv('REFRESH_SECONDS', '21600')),
              'duration': int(os.getenv('MATCH_DURATION_MINUTES', '105')),
              'path': Path(os.getenv('DATA_DIR', '/data')) / 'calendar.json'}
    base_url = os.getenv('CALENDAR_BASE_URL')
    if base_url:
        url = subscription_url(base_url, token)
        config['calendar_path'] = urlsplit(base_url).path.rstrip('/') + '/calendar.ics'
    if config['interval'] < 300 or config['duration'] < 1:
        raise ValueError('Refresh interval must be at least 300 seconds; match duration must be positive')
    config['path'].parent.mkdir(parents=True, exist_ok=True)
    if base_url:
        LOG.info('Subscription URL: %s', url)
    threading.Thread(target=worker, args=(config,), daemon=True).start()
    ThreadingHTTPServer(('0.0.0.0', 8080), handler(config)).serve_forever()


if __name__ == '__main__':
    main()
