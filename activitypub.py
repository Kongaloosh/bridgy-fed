"""Handles requests for ActivityPub endpoints: actors, inbox, etc.
"""
from base64 import b64encode
import datetime
from hashlib import sha256
import logging
import re

from flask import request
from google.cloud import ndb
from granary import as2
from oauth_dropins.webutil import flask_util, util
from oauth_dropins.webutil.flask_util import error
from oauth_dropins.webutil.util import json_dumps, json_loads

from app import app, cache
import common
from common import redirect_unwrap, redirect_wrap
from models import Activity, Follower, User
from httpsig.requests_auth import HTTPSignatureAuth

logger = logging.getLogger(__name__)

CACHE_TIME = datetime.timedelta(seconds=15)

SUPPORTED_TYPES = (
    'Accept',
    'Announce',
    'Article',
    'Audio',
    'Create',
    'Delete',
    'Follow',
    'Image',
    'Like',
    'Note',
    'Undo',
    'Video',
)


def send(activity, inbox_url, user_domain):
    """Sends an ActivityPub request to an inbox.

    Args:
      activity: dict, AS2 activity
      inbox_url: string
      user_domain: string, domain of the bridgy fed user sending the request

    Returns:
      requests.Response
    """
    logger.info(f'Sending AP request from {user_domain}: {json_dumps(activity, indent=2)}')

    # prepare HTTP Signature (required by Mastodon)
    # https://w3c.github.io/activitypub/#authorization
    # https://tools.ietf.org/html/draft-cavage-http-signatures-07
    # https://github.com/tootsuite/mastodon/issues/4906#issuecomment-328844846
    key_id = request.host_url + user_domain
    user = User.get_or_create(user_domain)
    auth = HTTPSignatureAuth(secret=user.private_pem(), key_id=key_id,
                             algorithm='rsa-sha256', sign_header='signature',
                             headers=('Date', 'Digest', 'Host'))

    # deliver to inbox
    body = json_dumps(activity).encode()
    headers = {
        'Content-Type': common.CONTENT_TYPE_AS2,
        # required for HTTP Signature
        # https://tools.ietf.org/html/draft-cavage-http-signatures-07#section-2.1.3
        'Date': datetime.datetime.utcnow().strftime('%a, %d %b %Y %H:%M:%S GMT'),
        # required by Mastodon
        # https://github.com/tootsuite/mastodon/pull/14556#issuecomment-674077648
        'Digest': 'SHA-256=' + b64encode(sha256(body).digest()).decode(),
        'Host': util.domain_from_link(inbox_url, minimize=False),
    }
    return common.requests_post(inbox_url, data=body, auth=auth,
                                headers=headers)


@app.get(f'/<regex("{common.DOMAIN_RE}"):domain>')
@flask_util.cached(cache, CACHE_TIME)
def actor(domain):
    """Fetches a domain's h-card and converts to AS2 actor."""
    actor = common.actor(domain)
    return (actor, {
        'Content-Type': common.CONTENT_TYPE_AS2,
        'Access-Control-Allow-Origin': '*',
    })


@app.post(f'/inbox')
@app.post(f'/<regex("{common.DOMAIN_RE}"):domain>/inbox')
def inbox(domain=None):
    """Handles ActivityPub inbox delivery."""
    body = request.get_data(as_text=True)
    logger.info(f'Got: {body}')

    # parse and validate AS2 activity
    try:
        activity = request.json
        assert activity
    except (TypeError, ValueError, AssertionError):
        error("Couldn't parse body as JSON", exc_info=True)

    logger.info(f'Got: {json_dumps(activity, indent=2)}')

    obj = activity.get('object') or {}
    if isinstance(obj, str):
        obj = {'id': obj}

    type = activity.get('type')
    if type == 'Accept':  # eg in response to a Follow
        return ''  # noop
    if type not in SUPPORTED_TYPES:
        error('Sorry, %s activities are not supported yet.' % type,
                     status=501)

    # TODO: verify signature if there is one

    if type == 'Undo' and obj.get('type') == 'Follow':
        # skip actor fetch below; we don't need it to undo a follow
        undo_follow(redirect_unwrap(activity))
        return ''
    elif type == 'Delete':
        id = obj.get('id')

        # !!! temporarily disabled actually deleting Followers below because
        # mastodon.social sends Deletes for every Bridgy Fed account, all at
        # basically the same time, and we have many Follower objects, so we
        # have to do this table scan for each one, so the requests take a
        # long time and end up spawning extra App Engine instances that we
        # get billed for. and the Delete requests are almost never for
        # followers we have. TODO: revisit this and do it right.

        # if isinstance(id, str):
        #     # assume this is an actor
        #     # https://github.com/snarfed/bridgy-fed/issues/63
        #     for key in Follower.query().iter(keys_only=True):
        #         if key.id().split(' ')[-1] == id:
        #             key.delete()
        return ''

    # fetch actor if necessary so we have name, profile photo, etc
    actor = activity.get('actor')
    if actor and isinstance(actor, str):
        actor = activity['actor'] = common.get_as2(actor).json()

    activity_unwrapped = redirect_unwrap(activity)
    if type == 'Follow':
        return accept_follow(activity, activity_unwrapped)

    # send webmentions to each target
    as1 = as2.to_as1(activity)
    source_as2 = json_dumps(activity_unwrapped)
    sent = common.send_webmentions(as1, proxy=True, protocol='activitypub',
                                   source_as2=source_as2)

    if not sent and type in ('Create', 'Announce'):
        # check that this activity is public. only do this check for Creates,
        # not Like, Follow, or other activity types, since Mastodon doesn't
        # currently mark those as explicitly public.
        if not as2.is_public(activity_unwrapped):
            logging.info('Dropping non-public activity')
            return ''

        # normal post, deliver to BF followers
        source = activity.get('url') or activity.get('id')
        domains = []
        if actor:
            actor_id = actor.get('id')
            if actor_id:
                logging.info(f'Finding followers of {actor_id}')
                domains = [f.src for f in
                           Follower.query(Follower.dest == actor_id,
                                          projection=[Follower.src]).fetch()]
        key = Activity(source=source, target='Public', direction='in',
                       protocol='activitypub', domain=domains, status='complete',
                       source_as2=source_as2).put()
        logging.info(f'Wrote Activity {key} with {len(domains)} follower domains')

    return ''


def accept_follow(follow, follow_unwrapped):
    """Replies to an AP Follow request with an Accept request.

    Args:
      follow: dict, AP Follow activity
      follow_unwrapped: dict, same, except with redirect URLs unwrapped
    """
    logger.info('Replying to Follow with Accept')

    followee = follow.get('object')
    followee_unwrapped = follow_unwrapped.get('object')
    follower = follow.get('actor')
    if not followee or not followee_unwrapped or not follower:
        error('Follow activity requires object and actor. Got: %s' % follow)

    inbox = follower.get('inbox')
    follower_id = follower.get('id')
    if not inbox or not follower_id:
        error('Follow actor requires id and inbox. Got: %s', follower)

    # store Follower
    user_domain = util.domain_from_link(followee_unwrapped, minimize=False)
    Follower.get_or_create(dest=user_domain, src=follower_id,
                           last_follow=json_dumps(follow))

    # send AP Accept
    accept = {
        '@context': 'https://www.w3.org/ns/activitystreams',
        'id': util.tag_uri(request.host, 'accept/%s/%s' % (
            (user_domain, follow.get('id')))),
        'type': 'Accept',
        'actor': followee,
        'object': {
            'type': 'Follow',
            'actor': follower_id,
            'object': followee,
        }
    }
    resp = send(accept, inbox, user_domain)

    # send webmention
    common.send_webmentions(as2.to_as1(follow), proxy=True, protocol='activitypub',
                            source_as2=json_dumps(follow_unwrapped))

    return resp.text, resp.status_code


@ndb.transactional()
def undo_follow(undo_unwrapped):
    """Handles an AP Undo Follow request by deactivating the Follower entity.

    Args:
      undo_unwrapped: dict, AP Undo activity with redirect URLs unwrapped
    """
    logger.info('Undoing Follow')

    follow = undo_unwrapped.get('object', {})
    follower = follow.get('actor')
    followee = follow.get('object')
    if not follower or not followee:
        error('Undo of Follow requires object with actor and object. Got: %s' % follow)

    # deactivate Follower
    user_domain = util.domain_from_link(followee, minimize=False)
    follower_obj = Follower.get_by_id(Follower._id(dest=user_domain, src=follower))
    if follower_obj:
        logger.info(f'Marking {follower_obj.key} as inactive')
        follower_obj.status = 'inactive'
        follower_obj.put()
    else:
        logger.warning(f'No Follower found for {user_domain} {follower}')

    # TODO send webmention with 410 of u-follow
