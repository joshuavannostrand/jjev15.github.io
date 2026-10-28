import os
import sys
import unittest
from datetime import datetime
from zoneinfo import ZoneInfo
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from fastapi.testclient import TestClient
from app import app, parse_dining, pack, unpack, valid_feed_url
from ical_feed import parse_ics, unfold

CLIENT = TestClient(app)
ICS='''BEGIN:VCALENDAR\r
VERSION:2.0\r
BEGIN:VEVENT\r
UID:a-1\r
DTSTART;TZID=America/New_York:20261028T230000\r
DTEND;TZID=America/New_York:20261028T235900\r
SUMMARY:Data Analytics Assignment Due\r
DESCRIPTION:Please submit your work\r
LOCATION:Saunders\r
END:VEVENT\r
BEGIN:VEVENT\r
UID:a-2\r
DTSTART;VALUE=DATE:20261029\r
DTEND;VALUE=DATE:20261030\r
SUMMARY:Study day\r
END:VEVENT\r
BEGIN:VEVENT\r
UID:a-3\r
DTSTART;TZID=America/New_York:20261028T090000\r
DTEND;TZID=America/New_York:20261028T100000\r
RRULE:FREQ=DAILY;COUNT=3\r
SUMMARY:Lab\r
END:VEVENT\r
END:VCALENDAR\r
'''

class DashboardTests(unittest.TestCase):
    def test_home(self):
        r=CLIENT.get('/')
        self.assertEqual(r.status_code,200)
        self.assertIn('Your RIT Day',r.text)
        self.assertIn('myCourses',r.text)
    def test_status_no_secrets(self):
        res=CLIENT.get('/api/status')
        self.assertEqual(res.status_code,200)
        self.assertNotIn('DASHBOARD_SECRET',res.text)
        self.assertIn('mycoursesConnected',res.json())
    def test_secret_roundtrip(self):
        item={'url':'https://mycourses.rit.edu/something?token=secret'}
        raw=pack(item)
        self.assertNotIn('secret',raw)
        self.assertEqual(unpack(raw),item)
    def test_feed_allowlist(self):
        self.assertEqual(valid_feed_url('webcal://mycourses.rit.edu/feed.ics'),'https://mycourses.rit.edu/feed.ics')
        for bad in ['http://mycourses.rit.edu/x','https://localhost/',
                    'https://mycourses.rit.edu.evil.com/calendar.ics','https://127.0.0.1/','https://google.com/a']:
            with self.assertRaises(Exception):valid_feed_url(bad)
    def test_calendar_parser(self):
        results=parse_ics(ICS,'mycourses',21,datetime(2026,10,28,9,tzinfo=ZoneInfo('America/New_York')))
        self.assertEqual(len(results),5)
        target=next(e for e in results if 'Assignment Due' in e['title'])
        self.assertTrue(target['due'])
        self.assertEqual(target['start'],'2026-10-28T23:00:00-04:00')
        self.assertTrue(next(e for e in results if e['title']=='Study day')['allDay'])
    def test_fold(self):
        self.assertEqual(unfold('SUMMARY:First\r\n half\r\n Second'),['SUMMARY:FirsthalfSecond'])
    def test_dining_parser(self):
        html='''<main><h2>Open Now</h2><ul><li>Gracie's</li><li>Gracie's</li><li>Dining hall</li><li>pizza, grill</li><li>10:00 am - 9:00 pm</li></ul><p>All Locations</p><h2>Today's Visiting Chefs</h2><p>RITZ</p><a>P.H. Express: 11:00 am - 2:00 pm</a><p>P.H. Express</p><p>Pakistani cuisine</p><h2>Dining Events</h2></main>'''
        parsed=parse_dining(html)
        self.assertEqual(parsed['chefs'][0]['name'],'P.H. Express')
        self.assertEqual(parsed['chefs'][0]['location'],'RITZ')
        self.assertTrue(parsed['locations'][0]['listedOpen'])
    def test_no_mock_data_in_apis(self):
        # Without an actual connected account, private API returns empty rather than fabricated events.
        r=CLIENT.get('/api/agenda')
        self.assertEqual(r.status_code,200)
        self.assertEqual(r.json()['events'],[])

if __name__=='__main__':unittest.main()
