# Copyright 2014 The Swarming Authors. All rights reserved.
# Use of this source code is governed by the Apache v2.0 license that can be
# found in the LICENSE file.

"""Gerrit functions for GAE environment."""

import collections
import json
import logging
import urllib
import urlparse

from components import auth
from components import net
from components import utils


AUTH_SCOPE = 'https://www.googleapis.com/auth/gerritcodereview'
RESPONSE_PREFIX = ")]}'"


Owner = collections.namedtuple('Owner', ['name', 'email', 'username'])


Revision = collections.namedtuple(
    'Revision',
    [
      # Commit sha, such as d283186300411e4d05ef0ced6c29fe77e8767a43.
      'commit',
      # Ordinal of the revision within a GerritChange, starting from 1.
      'number',
      # A ref where this commit can be fetched.
      'fetch_ref',
    ])


Change = collections.namedtuple(
    'Change',
    [
      # A "long" change id, such as
      # chromium/src~master~If1bfd2e7d0ad2c14908e5d45a513b5335d36ff01
      'id',
      # A "short" change id, such as If1bfd2e7d0ad2c14908e5d45a513b5335d36ff01
      'change_id',
      'project',
      'branch',
      'subject',
      # Owner of the Change, of type Owner.
      'owner',
      # Sha of the current revision's commit.
      'current_revision',
      # A list of Revision objects.
      'revisions',
    ])


def get_change(
    hostname, change_id, include_all_revisions=True,
    include_owner_details=False):
  """Gets a single Gerrit change by id.

  Returns Change object, or None if change was not found.
  """
  path = 'changes/%s' % change_id
  if include_owner_details:
    path += '/detail'
  if include_all_revisions:
    path += '?o=ALL_REVISIONS'
  data = fetch_json(hostname, path)
  if data is None:
    return None

  owner = None
  ownerData = data.get('owner')
  if ownerData:  # pragma: no branch
    owner = Owner(
        name=ownerData.get('name'),
        email=ownerData.get('email'),
        username=ownerData.get('username'))

  revisions = [
    Revision(
        commit=key,
        number=int(value['_number']),
        fetch_ref=value['fetch']['http']['ref'],
    ) for key, value in data.get('revisions', {}).iteritems()]
  revisions.sort(key=lambda r: r.number)

  return Change(
      id=data['id'],
      project=data.get('project'),
      branch=data.get('branch'),
      subject=data.get('subject'),
      change_id=data.get('change_id'),
      current_revision=data.get('current_revision'),
      revisions=revisions,
      owner=owner)


def set_review(
    hostname, change_id, revision, message=None, labels=None, notify=None):
  """Sets review on a revision.

  Args:
    hostname (str): Gerrit hostname.
    change_id: Gerrit change id, such as project~branch~I1234567890.
    revision: a commit sha for the patchset to review.
    message: text message.
    labels: a dict of label names and their values, such as {'Verified': 1}.
    notify: who to notify. Supported values:
      None - use default behavior, same as 'ALL'.
      'NONE': do not notify anyone.
      'OWNER': notify owner of the change_id.
      'OWNER_REVIEWERS': notify owner and OWNER_REVIEWERS.
      'ALL': notify anyone interested in the Change.
  """
  if notify is not None:
    notify = str(notify).upper()
  assert notify in (None, 'NONE', 'OWNER', 'OWNER_REVIEWERS', 'ALL')
  body = {
    'labels': labels,
    'message': message,
    'notify': notify,
  }
  body = {k:v for k, v in body.iteritems() if v is not None}

  path = 'changes/%s/revisions/%s/review' % (change_id, revision)
  fetch_json(hostname, path, method='POST', payload=body)


def fetch(hostname, path, **kwargs):
  """Sends request to Gerrit, returns raw response.

  See 'net.request' for list of accepted kwargs.

  Returns:
    Response body on success.
    None on 404 response.

  Raises:
    net.Error on communication errors.
  """
  assert not path.startswith('/'), path
  assert 'scopes' not in kwargs, kwargs['scopes']
  try:
    url = urlparse.urljoin('https://' + hostname, 'a/' + path)
    return net.request(url, scopes=[AUTH_SCOPE], **kwargs)
  except net.NotFoundError:
    return None


def fetch_json(hostname, path, payload=None, headers=None, **kwargs):
  """Sends JSON request to Gerrit, parses prefixed JSON response.

  See 'fetch' for the list of arguments.

  Returns:
    Deserialized response body on success.
    None on 404 response.

  Raises:
    net.Error on communication errors.
  """
  headers = (headers or {}).copy()
  headers['Accept'] = 'application/json'
  if payload is not None:
    headers['Content-Type'] = 'application/json; charset=utf-8'
  content = fetch(
      hostname=hostname,
      path=path,
      payload=utils.encode_to_json(payload) if payload is not None else None,
      headers=headers,
      **kwargs)
  if content is None:
    return None
  if not content.startswith(RESPONSE_PREFIX):
    msg = (
        'Unexpected response format. Expected prefix %s. Received: %s' %
        (RESPONSE_PREFIX, content))
    raise net.Error(msg, status_code=200, response=content)
  return json.loads(content[len(RESPONSE_PREFIX):])