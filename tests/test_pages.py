"""Unit tests for pages.py."""
from oauth_dropins.webutil import util
from oauth_dropins.webutil.util import json_dumps, json_loads
from granary import as2, atom, microformats2, rss

from models import Follower, Activity
from . import testutil
from .test_activitypub import LIKE, MENTION, NOTE, REPLY


def contents(activities):
    return [a['object']['content'] for a in activities]


class PagesTest(testutil.TestCase):

    EXPECTED = contents([as2.to_as1(REPLY), as2.to_as1(NOTE)])

    @staticmethod
    def add_activities():
        Activity(id='a', domain=['foo.com'], direction='in',
                 source_as2=json_dumps(NOTE)).put()
        # different domain
        Activity(id='b', domain=['bar.org'], direction='in',
                 source_as2=json_dumps(MENTION)).put()
        # empty, should be skipped
        Activity(id='c', domain=['foo.com'], direction='in').put()
        Activity(id='d', domain=['foo.com'], direction='in',
                 source_as2=json_dumps(REPLY)).put()
        # wrong direction
        Activity(id='e', domain=['foo.com'], direction='out',
                 source_as2=json_dumps(NOTE)).put()
        # skip Likes
        Activity(id='f', domain=['foo.com'], direction='in',
                 source_as2=json_dumps(LIKE)).put()

    def test_feed_html_empty(self):
        got = self.client.get('/user/foo.com/feed')
        self.assert_equals(200, got.status_code)
        self.assert_equals([], microformats2.html_to_activities(got.text))

    def test_feed_html(self):
        self.add_activities()
        got = self.client.get('/user/foo.com/feed')
        self.assert_equals(200, got.status_code)
        self.assert_equals(self.EXPECTED,
                           contents(microformats2.html_to_activities(got.text)))

    def test_feed_atom_empty(self):
        got = self.client.get('/user/foo.com/feed?format=atom')
        self.assert_equals(200, got.status_code)
        self.assert_equals([], atom.atom_to_activities(got.text))

    def test_feed_atom(self):
        self.add_activities()
        got = self.client.get('/user/foo.com/feed?format=atom')
        self.assert_equals(200, got.status_code)
        self.assert_equals(self.EXPECTED, contents(atom.atom_to_activities(got.text)))

    def test_feed_rss_empty(self):
        got = self.client.get('/user/foo.com/feed?format=rss')
        self.assert_equals(200, got.status_code)
        self.assert_equals([], rss.to_activities(got.text))

    def test_feed_rss(self):
        self.add_activities()
        got = self.client.get('/user/foo.com/feed?format=rss')
        self.assert_equals(200, got.status_code)
        self.assert_equals(self.EXPECTED, contents(rss.to_activities(got.text)))
