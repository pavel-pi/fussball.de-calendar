import http.client
import json
import tempfile
import threading
import unittest
from datetime import datetime, timezone
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from icalendar import Calendar
from app import handler, parse_games, make_calendar, refresh


def fixture(day='19.07.2026', status=''):
    return f'''<tr class="row-headline"><td>Sonntag, {day} - 15:00 Uhr | Liga</td></tr>
    <tr><td class="column-club"><div class="club-name">A FC</div></td>
    <td class="column-club"><div class="club-name">B FC</div></td>
    <td class="column-score">{status}</td>
    <td class="column-detail"><a href="https://www.fussball.de/spiel/a-b/-/spiel/ABC123">Spiel</a></td></tr>
    <tr class="row-venue"><td colspan="3">Field, Main Street 1</td></tr>'''


class CalendarTests(unittest.TestCase):
    def test_reschedule_keeps_uid_and_increases_sequence(self):
        now = datetime.now(timezone.utc)
        first, state = make_calendar(parse_games(fixture()), {}, 'Team', 105, now)
        same, _ = make_calendar(parse_games(fixture()), state, 'Team', 105, now)
        self.assertEqual(first, same)
        second, _ = make_calendar(parse_games(fixture('20.07.2026')), state, 'Team', 105, now)
        old = Calendar.from_ical(first).walk('VEVENT')[0]
        new = Calendar.from_ical(second).walk('VEVENT')[0]
        self.assertEqual(old['UID'], new['UID'])
        self.assertEqual(new['SEQUENCE'], 1)
        self.assertEqual(old.decoded('DTSTART').hour, 13)
        self.assertEqual(str(old['LOCATION']), 'Field, Main Street 1')

    def test_winter_and_cancellation(self):
        games = parse_games(fixture('20.12.2026', 'Absetzung'))
        self.assertEqual(games['ABC123']['status'], 'CANCELLED')
        self.assertIn('14:00:00+00:00', games['ABC123']['start'])

    def test_unknown_html_rejected(self):
        with self.assertRaises(ValueError):
            parse_games('<html>Login required</html>')
        with self.assertRaises(ValueError):
            parse_games(fixture().replace('15:00', '--:--'))

    def test_bye_without_kickoff_does_not_discard_real_games(self):
        bye = '''<tr class="row-headline"><td>Sonntag, 25.10.2026 -  Uhr | Kreisliga A</td></tr>
        <tr><td class="column-club"><div class="club-name">A FC</div></td>
        <td class="column-club"><span>spielfrei</span></td></tr>'''
        games = parse_games(fixture() + bye)
        self.assertEqual(list(games), ['ABC123'])

    def test_failure_preserves_snapshot(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'calendar.json'
            config = {'path': path, 'team': 'ABC', 'from': '2026-07-01', 'name': 'Test', 'duration': 105}
            with patch('app.fetch_games', return_value=parse_games(fixture())):
                with self.assertLogs('app', level='INFO') as logs:
                    refresh(config)
            before = path.read_bytes()
            self.assertIn(json.loads(before)['updated'], logs.output[0])
            with patch('app.fetch_games', side_effect=TimeoutError):
                with self.assertRaises(TimeoutError):
                    refresh(config)
            self.assertEqual(before, path.read_bytes())
            self.assertEqual(len(json.loads(before)['events']), 1)


class AccessTests(unittest.TestCase):
    def test_feed_requires_token_and_other_routes_are_unavailable(self):
        token = 'a' * 43
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'calendar.json'
            path.write_text(json.dumps({
                'team': 'ABC', 'ics': 'BEGIN:VCALENDAR\r\nEND:VCALENDAR\r\n',
                'updated': datetime.now(timezone.utc).isoformat(), 'events': {},
            }))
            config = {'path': path, 'team': 'ABC', 'interval': 21600, 'token': token}
            server = ThreadingHTTPServer(('127.0.0.1', 0), handler(config))
            thread = threading.Thread(target=server.serve_forever)
            thread.start()
            try:
                def request(route, method='GET'):
                    connection = http.client.HTTPConnection('127.0.0.1', server.server_port)
                    try:
                        connection.request(method, route)
                        response = connection.getresponse()
                        return response.status, response.read()
                    finally:
                        connection.close()

                for query in ('', '?token=wrong', f'?token={token}&token=wrong', '?token='):
                    self.assertEqual(request('/calendar.ics' + query)[0], 404)
                self.assertEqual(request('/calendar.ics?token=' + token)[0], 200)
                self.assertEqual(request('/calendar.ics?token=' + token, 'HEAD')[0], 200)
                for route in ('/status', '/healthz'):
                    self.assertEqual(request(route + '?token=' + token)[0], 404)
                    self.assertEqual(request(route + '?token=' + token, 'HEAD')[0], 404)
                self.assertIn(b'BEGIN:VCALENDAR', request('/calendar.ics?token=' + token)[1])
            finally:
                server.shutdown()
                server.server_close()
                thread.join()


if __name__ == '__main__':
    unittest.main()
