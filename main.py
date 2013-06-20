# Copyright (c) 2012 The Chromium Authors. All rights reserved.
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

import binascii
import datetime
import hashlib
import logging
import os
import re
import zlib

# The app engine headers are located locally, so don't worry about not finding
# them.
# pylint: disable=E0611,F0401
import webapp2
from google.appengine import runtime
from google.appengine.ext import blobstore
from google.appengine.api import files
from google.appengine.api import memcache
from google.appengine.api import taskqueue
from google.appengine.ext import ndb
from google.appengine.ext.webapp import blobstore_handlers
# pylint: enable=E0611,F0401

import acl
import config
import gsfiles
import stats
import template


# The maximum number of entries that can be queried in a single request.
MAX_KEYS_PER_CALL = 1000

# The minimum size, in bytes, an entry must be before it gets stored in Google
# Cloud Storage, otherwise it is stored as a blob property.
MIN_SIZE_FOR_GS = 501

# The maximum number of items to delete at a time.
ITEMS_TO_DELETE_ASYNC = 100

# Limit the namespace to 29 characters because app engine seems unable to
# find the blobs in cloud storage if the namespace is longer.
# TODO(csharp): Find a way to support namespaces greater than 29 characters.
MAX_NAMESPACE_LEN = 29

# Maximum size of file stored in GS to be saved in memcache. The value must be
# small enough so that the whole content can safely fit in memory.
MAX_MEMCACHE_ISOLATED = 500*1024


#### Models


class ContentNamespace(ndb.Model):
  """Used as an ancestor of ContentEntry to create mutiple content-addressed
  "tables".

  Eventually, the table name could have a prefix to determine the hashing
  algorithm, like 'sha1-'.

  There's usually only one table name:
  - default:    The default CAD.
  - temporary*: This family of namespace is a discardable namespace for testing
                purpose only.

  The table name can have suffix:
  - -gzip or -deflate: The namespace contains the content in deflated format.
                       The content key is the hash of the uncompressed data, not
                       the compressed one. That is why it is in a separate
                       namespace.

  All the tables in the temporary* family must have is_testing==True and the
  others is_testing==False.
  """
  is_testing = ndb.BooleanProperty()
  creation = ndb.DateTimeProperty(indexed=False, auto_now=True)


class ContentEntry(ndb.Model):
  """Represents the content, keyed by its SHA-1 hash."""
  # The GS filename. blobstore.create_upload_url() doesn't permit specifying a
  # reliable filename, so save the autogenerated filename. Save the file size
  # too
  filename = ndb.StringProperty(indexed=False)

  # Cache the file size for statistics purposes.
  size = ndb.IntegerProperty()

  # Serves two purposes. It is only set once the Entry had its hash been
  # verified. -1 means it wasn't verified yet.
  #
  # The value is the Cache the expanded file size for statistics purposes. Its
  # value is different from size only in compressed namespaces.
  expanded_size = ndb.IntegerProperty(default=-1)

  # The content stored inline. This is only valid if the content was smaller
  # than MIN_SIZE_FOR_GS.
  content = ndb.BlobProperty()

  # The day the content was last accessed. This is used to determine when
  # data is old and should be cleared.
  last_access = ndb.DateProperty(auto_now_add=True)

  creation = ndb.DateTimeProperty(indexed=False, auto_now=True)

  # It is an .isolated file.
  is_isolated = ndb.BooleanProperty(default=False)

  @property
  def is_compressed(self):
    """Is it the raw data or was it modified in any form, e.g. compressed, so
    that the SHA-1 doesn't match.
    """
    self.key.parent().id().endswith(('-gzip', '-deflate'))

  @property
  def gs_filepath(self):
    """Returns the full path of an object saved in GS."""
    if self.content:
      return None
    # namespace/hash_key.
    return u'%s/%s' % (self.key.parent().id(), self.filename)


### Utility


class Accumulator(object):
  """Accumulates output from a generator."""
  def __init__(self, source):
    self.accumulated = []
    self._source = source

  def __iter__(self):
    for i in self._source:
      self.accumulated.append(i)
      yield i


def get_content_by_hash(namespace, hash_key):
  """Returns the ContentEntry with the given hex encoded SHA-1 hash |hash_key|.

  This function is synchronous.

  Returns None if it no ContentEntry matches.
  """
  length = get_hash_algo(namespace).digest_size * 2
  if not re.match(r'[a-f0-9]{' + str(length) + r'}', hash_key):
    logging.error('Given an invalid key, %s', hash_key)
    return None

  try:
    return ContentEntry.get_by_id(
        hash_key, parent=ndb.Key(ContentNamespace, namespace))
  except (ndb.BadKeyError, ndb.KindError):
    pass

  return None


def create_entry(namespace, hash_key):
  """Generates a new ContentEntry from the request if one doesn't exist.

  This function is synchronous.

  Creates the ContentNamespace entity on the fly if needed.

  Returns None if there is a problem generating the entry or if an entry already
  exists with the given hex encoded SHA-1 hash |hash_key|.
  """
  length = get_hash_algo(namespace).digest_size * 2
  if not re.match(r'[a-f0-9]{' + str(length) + r'}', hash_key):
    logging.error('Given an invalid key, %s', hash_key)
    return None

  future_namespace = ContentNamespace.get_or_insert_async(
      namespace, is_testing=namespace.startswith('temporary'))
  key = ndb.Key(ContentNamespace, namespace, ContentEntry, hash_key)
  # TODO(maruel): Profile to see if it is faster to fetch the whole entity so
  # the cache is used.
  future_entry = ContentEntry.query(ContentEntry.key == key).get_async(
      keys_only=True)
  future_namespace.wait()

  if future_entry.get_result():
    return None
  # The entity was not present. Create a new one.
  return ContentEntry(key=key)


def delete_entry_and_gs_entry(to_delete):
  """Deletes ContentEntry and their GS files.

  Returns a list of Future that must be waited on.
  """
  # Note all the files to delete.
  gs_files_to_delete = filter(None, (i.gs_filepath for i in to_delete))

  entities_future = ndb.delete_multi_async(to_delete)

  # Do this one last because it is synchronous.
  # TODO(maruel): This could leak a broken BlobInfo object in the blobstore
  # table. There is no easy way to find it back.
  gsfiles.delete(gs_files_to_delete)
  return entities_future


def get_hash_algo(_namespace):
  """Returns an instance of the hashing algorithm for the namespace."""
  # TODO(maruel): Support other algorithms.
  return hashlib.sha1()


def split_payload(request, chunk_size, max_chunks):
  """Splits a binary payload into elements of |chunk_size| length.

  Returns each chunks.
  """
  content = request.request.body
  if len(content) % chunk_size:
    msg = (
        'Payload must be in increments of %d bytes, had %d bytes total, last '
        'chunk was of length %d' % (
              chunk_size,
              len(content),
              len(content) % chunk_size))
    logging.error(msg)
    request.abort(400, detail=msg)

  count = len(content) / chunk_size
  if count > max_chunks:
    msg = (
        'Requested more than %d hash digests in a single request, '
        'aborting' % count)
    logging.warning(msg)
    request.abort(400, detail=msg)

  return [content[i * chunk_size: (i + 1) * chunk_size] for i in xrange(count)]


def payload_to_hashes(request, namespace):
  """Converts a raw payload into SHA-1 hashes as bytes."""
  return split_payload(
      request, get_hash_algo(namespace).digest_size, MAX_KEYS_PER_CALL)


def expand_content(namespace, source):
  """Yields expanded data from source."""
  # TODO(maruel): Remove '-gzip' since it's a misnomer.
  if namespace.endswith(('-deflate', '-gzip')):
    zlib_state = zlib.decompressobj()
    for i in source:
      yield zlib_state.decompress(i, gsfiles.CHUNK_SIZE)
      while zlib_state.unconsumed_tail:
        yield zlib_state.decompress(
            zlib_state.unconsumed_tail, gsfiles.CHUNK_SIZE)
    yield zlib_state.flush()
    # Forcibly delete the state.
    del zlib_state
  else:
    # Returns the source as-is.
    for i in source:
      yield i


def delete_blobinfo_async(blobinfos):
  """Deletes BlobInfo properly.

  blobstore.delete*() do not accept a list of BlobInfo, they only accept a list
  BlobKey.

  Returns a list of Rpc objects.
  """
  return [blobstore.delete_async((b.key() for b in blobinfos))]


def incremental_delete(query, delete, check=None):
  """Applies |delete| to objects in a query asynchrously.

  This function is itself synchronous.

  Arguments:
  - query: iterator of items to process.
  - delete: callback that accepts a list of objects to delete and returns a list
            of objects that have a method .wait() to make sure all calls
            completed.
  - check: optional callback that can filter out items from |query| from
          deletion.

  Returns True if at least one object was found.
  """
  to_delete = []
  found = False
  count = 0
  futures = []
  for item in query:
    count += 1
    if not (count % 1000):
      logging.debug('Found %d items', count)
    if check and not check(item):
      continue
    to_delete.append(item)
    found = True
    if len(to_delete) == ITEMS_TO_DELETE_ASYNC:
      logging.info('Deleting %s entries', len(to_delete))
      futures.extend(delete(to_delete))
      to_delete = []

    # That's a lot of on-going operations which could take a significant amount
    # of memory. Wait a little on the oldest operations.
    # TODO(maruel): Profile memory usage to see if a few thousands of on-going
    # RPC objects is a problem in practice.
    while len(futures) > 10 * ITEMS_TO_DELETE_ASYNC:
      futures.pop(0).wait()

  if to_delete:
    logging.info('Deleting %s entries', len(to_delete))
    futures.extend(delete(to_delete))

  for future in futures:
    future.wait()
  return found


def save_in_memcache(namespace, hash_key, content):
  try:
    if not memcache.set(hash_key, content, namespace='table_%s' % namespace):
      logging.error(
          'Attempted to save %d bytes of content in memcache but failed',
          len(content))
  except ValueError as e:
    logging.error(e)


### Restricted handlers


class RestrictedCleanupOldEntriesWorkerHandler(webapp2.RequestHandler):
  """Removes the old data from the datastore.

  Only a task queue task can use this handler.
  """
  def post(self):
    if not self.request.headers.get('X-AppEngine-QueueName'):
      self.abort(405, detail='Only internal task queue tasks can do this')
    logging.info('Deleting old datastore entries')
    old_cutoff = datetime.datetime.today() - datetime.timedelta(
        days=config.settings().retension_days)

    incremental_delete(
        ContentEntry.query(ContentEntry.last_access < old_cutoff),
        delete=delete_entry_and_gs_entry)
    logging.info('Done deleting old entries')


class RestrictedCleanupTestingEntriesWorkerHandler(webapp2.RequestHandler):
  """Removes the testing data from the datastore.

  Keep stuff under testing for only one full day.

  Only a task queue task can use this handler.
  """
  def post(self):
    if not self.request.headers.get('X-AppEngine-QueueName'):
      self.abort(405, detail='Only internal task queue tasks can do this')
    logging.info('Deleting testing entries')
    old_cutoff_testing = datetime.datetime.today() - datetime.timedelta(days=1)
    # For each testing namespace.
    namespace_query = ContentNamespace.query(
        ContentNamespace.is_testing == True)
    orphaned_namespaces = []
    # TODO(maruel): These could be run in parallel with @ndb.synctasklet.
    for namespace in namespace_query.iter(keys_only=True):
      logging.debug('Namespace %s', namespace.id())
      found = incremental_delete(
          ContentEntry.query(
              ContentEntry.last_access < old_cutoff_testing,
              ancestor=namespace).iter(keys_only=True),
          delete=ndb.delete_multi_async)
      if not found:
        orphaned_namespaces.append(namespace)
    if orphaned_namespaces:
      # Since delete_async() is used, the stale ContentNamespace will
      # likely stay for another full day, so keep it an extra day.
      logging.info('Deleting %s testing namespaces', len(orphaned_namespaces))
      ndb.delete_multi_async(orphaned_namespaces)
    logging.info('Done deleting testing namespaces')


class RestrictedObliterateWorkerHandler(webapp2.RequestHandler):
  """Deletes all the stuff."""
  def post(self):
    if not self.request.headers.get('X-AppEngine-QueueName'):
      self.abort(405, detail='Only internal task queue tasks can do this')
    logging.info('Deleting ContentEntry')
    incremental_delete(
        ContentEntry.query().iter(keys_only=True), ndb.delete_multi_async)

    logging.info('Deleting Namespaces')
    incremental_delete(
        ContentNamespace.query().iter(keys_only=True), ndb.delete_multi_async)

    logging.info('Deleting blobs')
    incremental_delete(blobstore.BlobInfo.all(), delete_blobinfo_async)

    gs_bucket = config.settings().gs_bucket
    logging.info('Deleting GS bucket %s', gs_bucket)
    incremental_delete(
        gsfiles.list_files(gs_bucket, None),
        lambda x: gsfiles.delete_files(gs_bucket, x))

    logging.info('Flushing memcache')
    # High priority (.isolated files) are cached explicitly. Make sure ghosts
    # are zapped too.
    memcache.flush_all()
    logging.info('Finally done!')


class RestrictedCleanupTriggerHandler(webapp2.RequestHandler):
  """Triggers a taskqueue to clean up."""
  def get(self, name):
    if name in ('obliterate', 'old', 'orphaned', 'testing'):
      url = '/restricted/taskqueue/cleanup/' + name
      # The push task queue name must be unique over a ~7 days period so use
      # the date at second precision, there's no point in triggering each of
      # time more than once a second anyway.
      now = datetime.datetime.utcnow().strftime('%Y-%m-%d_%I-%M-%S')
      taskqueue.add(url=url, queue_name='cleanup', name=name + '_' + now)
      self.response.out.write('Triggered %s' % url)
    else:
      self.abort(404, 'Unknown job')


class RestrictedTagWorkerHandler(webapp2.RequestHandler):
  """Tags .last_access for HashEntries tested for with /content/contains.

  This makes sure they are not evicted from the LRU cache too fast.
  """
  def post(self, namespace, year, month, day):
    if not self.request.headers.get('X-AppEngine-QueueName'):
      self.abort(405, detail='Only internal task queue tasks can do this')
    raw_hash_digests = payload_to_hashes(self, namespace)
    logging.info(
        'Stamping %d entries in namespace %s', len(raw_hash_digests), namespace)

    today = datetime.date(int(year), int(month), int(day))
    # It is safe to assume ContentNamespace entity exists. If it doesn't the
    # query will simply return nothing.
    parent_key = ndb.Key(ContentNamespace, namespace)
    # Fire up all the RPCs.
    rpcs = [
      ContentEntry.get_by_id_async(
          binascii.hexlify(raw_hash_digest), parent=parent_key)
      for raw_hash_digest in raw_hash_digests
    ]

    to_save = []
    for future in rpcs:
      item = future.get_result()
      if item and item.last_access != today:
        # Update the timestamp.
        item.last_access = today
        to_save.append(item)
    ndb.put_multi(to_save)
    logging.info('Done timestamping %d entries', len(to_save))


class RestrictedVerifyWorkerHandler(webapp2.RequestHandler):
  """Verify the SHA-1 matches for an object stored in BlobStore."""
  def post(self, namespace, hash_key):
    if not self.request.headers.get('X-AppEngine-QueueName'):
      self.abort(405, detail='Only internal task queue tasks can do this')

    entry = get_content_by_hash(namespace, hash_key)
    if not entry:
      logging.error('Failed to find entity')
      return
    if entry.expanded_size != -1:
      logging.warning('Was already verified')
      return
    if entry.content:
      logging.error('Should not be called with inline content')
      return

    save_to_memcache = entry.size <= MAX_MEMCACHE_ISOLATED and entry.is_isolated

    expanded_size = 0
    is_verified = False
    digest = get_hash_algo(namespace)
    try:
      # Start a loop where it reads the data in block.
      # TODO(maruel): Calculate the number of bytes read and assert it's the
      # same as entry.size.
      blob = gsfiles.open_file_for_reading(
          config.settings().gs_bucket, entry.gs_filepath)
      if save_to_memcache:
        # Wraps blob with a generator that accumulate the data.
        blob = Accumulator(blob)
      for data in expand_content(namespace, blob):
        expanded_size += len(data)
        digest.update(data)
      is_verified = digest.hexdigest() == hash_key
    except runtime.DeadlineExceededError:
      # Failed to read it through. If it's compressed, at least no zlib error
      # was thrown so the object is fine.
      logging.warning('Got DeadlineExceededError, giving up')
      return
    except (blobstore.BlobNotFoundError, zlib.error) as e:
      # It's broken. At that point, is_verified is False.
      logging.error(e)

    if not is_verified:
      # Delete the entity since it's corrupted.
      logging.error(
          'SHA-1 and data do not match, %d bytes (%d bytes expanded)',
          entry.size, expanded_size)
      for future in delete_entry_and_gs_entry([entry]):
        future.wait()
      # Do not return failure since we don't want the task scheduler to retry.
      return

    entry.expanded_size = expanded_size
    future = entry.put_async()
    logging.info(
        '%d bytes (%d bytes expanded) verified', entry.size, expanded_size)
    if save_to_memcache:
      save_in_memcache(namespace, hash_key, ''.join(blob.accumulated))
    future.wait()


class RestrictedStoreBlobstoreContentByHashHandler(
    acl.ACLRequestHandler,
    blobstore_handlers.BlobstoreUploadHandler):
  """Assigns the newly stored GS entry to the correct hash key."""

  # Requires special processing.
  enforce_token_on_post = False

  def dispatch(self):
    """Disable ACLRequestHandler.dispatch() checks here because the originating
    IP is always an AppEngine IP, which confuses the authentication code.
    """
    return webapp2.RequestHandler.dispatch(self)

  @staticmethod
  def _delete(contents):
    """Deletes unnecessary files stored in Cloud Storage."""
    items = [c.gs_object_name for c in contents]
    try:
      files.delete(*items)
    except runtime.DeadlineExceededError:
      logging.warning('Leaking files: %s', ', '.join(items))

  # pylint: disable=W0221
  def post(self, namespace, hash_key, original_access_id):
    # In particular, do not use self.request.remote_addr because the request
    # has as original an AppEngine local IP.
    self.access_id = original_access_id
    self.enforce_valid_token()
    contents = self.get_file_infos('content')
    if len(contents) != 1:
      # Delete all upload files since they aren't linked to anything.
      self._delete(contents)
      msg = 'Found %d files, there should only be 1.' % len(contents)
      logging.error(msg)
      self.abort(400, detail=msg)

    if not contents[0].gs_object_name.startswith(
        gsfiles.to_filepath(config.settings().gs_bucket, namespace)):
      self._delete(contents)
      msg = 'Unexpected namespace or GS bucket.'
      logging.error(msg)
      self.abort(400, detail=msg)

    entry = create_entry(namespace, hash_key)
    if not entry:
      stats.log(stats.DUPE, contents[0].size, '')
      self.response.out.write('Entry already existed')
      self._delete(contents)
      # Still report success.
      return

    try:
      priority = int(self.request.get('priority'))
    except ValueError:
      priority = 1

    # No need to save the full path, only the base name.

    entry.filename = os.path.basename(contents[0].gs_object_name)
    entry.size = contents[0].size
    # TODO(maruel): Add a new parameter.
    entry.is_isolated = (priority == 0)
    entry.put()

    if entry.size < MIN_SIZE_FOR_GS:
      logging.error(
          'User stored a file too small %d in GS, fix client code.', entry.size)

    # Trigger a verification. It can't be done inline since it could be too
    # long to complete.
    url = '/restricted/taskqueue/verify/%s/%s' % (namespace, hash_key)
    try:
      taskqueue.add(url=url, queue_name='verify')
    except runtime.DeadlineExceededError as e:
      msg = 'Unable to add task to verify blob.\n%s' % e
      logging.warning(msg)
      self.response.out.write(msg)
      self.response.set_status(500)

      for future in delete_entry_and_gs_entry([entry]):
        future.wait()
      return

    stats.log(stats.STORE, entry.size, 'GS; %s' % entry.filename)
    self.response.out.write('Content saved.')
    self.response.headers['Content-Type'] = 'text/plain'


### Non-restricted handlers


class ContainsHashHandler(acl.ACLRequestHandler):
  """Returns the presence of each hash key in the payload as a binary string.

  For each SHA-1 hash key in the request body in binary form, a corresponding
  chr(1) or chr(0) is in the 'string' returned.
  """
  def post(self, namespace):
    """This is a POST even though it doesn't modify any data[1], but it makes
    it easier for python scripts.

    [1] It does modify the timestamp of the objects.
    """
    raw_hash_digests = payload_to_hashes(self, namespace)
    logging.info(
        'Checking namespace %s for %d hash digests',
        namespace, len(raw_hash_digests))

    # No need to verify the entity is present, no object exists nor will be
    # found if the ancestor doesn't exist.
    namespace_model_key = ndb.Key(ContentNamespace, namespace)

    # Convert to entity keys.
    keys = (
        ndb.Key(
            ContentEntry,
            binascii.hexlify(raw_hash_digest),
            parent=namespace_model_key)
        for raw_hash_digest in raw_hash_digests
    )

    # Start the queries in parallel. It must be a list so the calls are executed
    # right away.
    # TODO(maruel): Queries are not cached so in practice it could be faster to
    # use get_by_id_async() or count_async instead even if this means pulling
    # all the data in. This needs to be profiled. Another option is to do the
    # caching ourself (?).
    queries = [
        ContentEntry.query(ContentEntry.key == key).get_async(keys_only=True)
        for key in keys
    ]

    # Convert the Future to True/False, then to byte, chr(0) if not present,
    # chr(1) if it is.
    contains = [bool(q.get_result()) for q in queries]
    self.response.out.write(bytearray(contains))
    self.response.headers['Content-Type'] = 'application/octet-stream'
    found = sum(contains, 0)
    stats.log(stats.LOOKUP, len(raw_hash_digests), found)
    if found:
      # For all the ones that exist, update their last_access in a task queue.
      hashes_to_tag = ''.join(
          raw_hash_digest for i, raw_hash_digest in enumerate(raw_hash_digests)
          if contains[i])
      url = '/restricted/taskqueue/tag/%s/%s' % (
          namespace, datetime.date.today())
      try:
        taskqueue.add(url=url, payload=hashes_to_tag, queue_name='tag')
      except (taskqueue.Error, runtime.DeadlineExceededError) as e:
        logging.warning('Problem adding task to update last_access. These '
                        'objects may get deleted sooner than intended.\n%s', e)


class GenerateBlobstoreHandler(acl.ACLRequestHandler):
  """Generate an upload url to directly load files into the GS bucket."""
  def post(self, namespace, hash_key):

    if len(namespace) > MAX_NAMESPACE_LEN:
      self.response.out.write('Unable to handle namespaces with more than %d '
                              'characters', MAX_NAMESPACE_LEN)
      self.response.set_status(400)

    self.response.headers['Content-Type'] = 'text/plain'
    url = '/restricted/content/store_blobstore/%s/%s/%s?token=%s' % (
        namespace,
        hash_key,
        self.access_id,
        self.request.get('token'))

    full_gs_path = '%s/%s' % (config.settings().gs_bucket, namespace)

    # Sadly, it is impossible to control the filename, only the path.
    # An option is to create a single file per directory but we could get into
    # an edge case with large number of directories.
    # TODO(maruel): Look at the alternatives.
    self.response.out.write(blobstore.create_upload_url(
        url,
        gs_bucket_name=full_gs_path))
    self.response.headers['Content-Type'] = 'text/plain'


class StoreContentByHashHandler(acl.ACLRequestHandler):
  """The handler for adding small content."""
  def post(self, namespace, hash_key):
    # webapp2 doesn't like reading the body if it's empty.
    if self.request.headers.get('content-length'):
      content = self.request.body
    else:
      content = ''

    entry = create_entry(namespace, hash_key)
    if not entry:
      stats.log(stats.DUPE, len(content), 'inline')
      self.response.out.write('Entry already existed')
      return

    try:
      priority = int(self.request.get('priority'))
    except ValueError:
      priority = 1

    # Verify the data while at it since it's already in memory but before
    # storing it in memcache and datastore.
    expanded_size = 0
    is_verified = False
    digest = get_hash_algo(namespace)
    try:
      for data in expand_content(namespace, [content]):
        expanded_size += len(data)
        digest.update(data)
      is_verified = digest.hexdigest() == hash_key
    except zlib.error as e:
      msg = 'Data is corrupted: %s' % e
      logging.error(msg)
      self.abort(400, msg)

    if not is_verified:
      # Delete the entity since it's corrupted.
      msg = 'SHA-1 and data do not match, %d bytes (%d bytes expanded)' % (
          len(content), expanded_size)
      logging.error(msg)
      self.abort(400, msg)

    if len(content) < MIN_SIZE_FOR_GS:
      entry.content = content
    else:
      filepath = '%s/%s' % (namespace, hash_key)
      if not gsfiles.store_content(
          config.settings().gs_bucket, filepath, content):
        # Returns 503 so the client automatically retries.
        self.abort(503, detail='Unable to save the content to GS.')
      entry.filename = hash_key

    # TODO(maruel): Add a new parameter to explicitly signal it is an
    # .isolated file.
    entry.is_isolated = (priority == 0)
    entry.size = len(content)
    entry.expanded_size = expanded_size
    future = entry.put_async()
    self.response.out.write('Content saved.')
    self.response.headers['Content-Type'] = 'text/plain'

    if (entry.filename and
        entry.is_isolated and
        entry.size <= MAX_MEMCACHE_ISOLATED):
      # There's no point in saving inline blobs in memcache because ndb already
      # memcaches them.
      save_in_memcache(namespace, hash_key, content)

    where = 'GS; ' + entry.filename if entry.filename else 'inline'
    stats.log(stats.STORE, len(content), where)
    future.wait()


class RetrieveContentByHashHandler(acl.ACLRequestHandler,
                                   blobstore_handlers.BlobstoreDownloadHandler):
  """The handlers for retrieving contents by its SHA-1 hash |hash_key|."""
  def get(self, namespace, hash_key):  #pylint: disable=W0221
    memcache_entry = memcache.get(hash_key, namespace='table_%s' % namespace)

    if memcache_entry:
      stats.log(stats.RETURN, len(memcache_entry), 'memcache')
      self.response.out.write(memcache_entry)
      return

    entry = get_content_by_hash(namespace, hash_key)
    if not entry:
      msg = 'Unable to find an ContentEntry with key \'%s\'.' % hash_key
      self.abort(404, detail=msg)

    # Result can be safely cached for 12 hours if it is present.
    self.response.headers['Cache-Control'] = 'public, max-age=43200'

    # If AppEngine wasn't silly and would stop stripping off the header, we'd
    # want that to have the HTTP level code decompress the response on the fly:
    #self.response.headers['Content-Encoding'] = 'deflate'

    if entry.content is not None:
      # Serve directly.
      stats.log(stats.RETURN, len(entry.content), 'inline')
      self.response.headers['Content-Disposition'] = (
          'attachment; filename="%s"' % hash_key)
      self.response.headers['Content-Type'] = 'application/octet-stream'
      self.response.out.write(entry.content)
    else:
      if not entry.filename:
        # Corrupted entry. Delete.
        msg = 'Corrupted entry with key \'%s\'.' % hash_key
        logging.error(msg)
        entry.delete()
        self.abort(404, detail=msg)

      # send_blob() will call create_gs_key() itself but this only works if
      # the string is encoded as utf-8.
      blobkey = gsfiles.to_filepath(
          config.settings().gs_bucket, entry.gs_filepath).encode('utf-8')
      stats.log(stats.RETURN, entry.size, 'GS; %s' % entry.filename)
      self.send_blob(
          blobkey,
          save_as=hash_key,
          content_type='application/octet-stream')


class RootHandler(webapp2.RequestHandler):
  """Tells the user to RTM."""
  def get(self):
    self.response.write(template.get('root.html').render({}))
    self.response.headers['Content-Type'] = 'text/html'


class WarmupHandler(webapp2.RequestHandler):
  def get(self):
    self.response.write('ok')
    self.response.headers['Content-Type'] = 'text/plain'


def CreateApplication():
  """Creates the url router.

  The basic layouts is as follow:
  - /restricted/.* requires being an instance administrator.
  - /restricted/taskqueue/.* are task queues.
  - /content/.* has the public HTTP API.

  Set in app.yaml:
  - /css/(.*) links to static/css/(\1)
  - /images/(.*) links to static/images/(\1)
  """
  acl.bootstrap()

  # Namespace can be letters, numbers and '-'.
  namespace = r'/<namespace:[a-z0-9A-Z\-]+>'
  # Do not enforce a length limit to support different hashing algorithm. This
  # should represent a valid hex value.
  hashkey = r'/<hash_key:[a-f0-9]{4,}>'
  # This means a complete key is required.
  namespace_key = namespace + hashkey

  return webapp2.WSGIApplication([
      # Triggers a taskqueue.
      webapp2.Route(
          r'/restricted/cleanup/trigger/<name:[a-z]+>',
          RestrictedCleanupTriggerHandler),

      # Cleanup tasks.
      webapp2.Route(
          r'/restricted/taskqueue/cleanup/old',
          RestrictedCleanupOldEntriesWorkerHandler),
      webapp2.Route(
          r'/restricted/taskqueue/cleanup/testing',
          RestrictedCleanupTestingEntriesWorkerHandler),
      webapp2.Route(
          r'/restricted/taskqueue/cleanup/obliterate',
          RestrictedObliterateWorkerHandler),

      # Tasks triggered by other request handlers.
      webapp2.Route(
          r'/restricted/taskqueue/tag' + namespace +
            r'/<year:\d\d\d\d>-<month:\d\d>-<day:\d\d>',
          RestrictedTagWorkerHandler),
      webapp2.Route(
          r'/restricted/taskqueue/verify' + namespace_key,
          RestrictedVerifyWorkerHandler),

      # Administrative urls.
      webapp2.Route(
          r'/restricted/whitelistip', acl.RestrictedWhitelistIPHandler),
      webapp2.Route(
          r'/restricted/whitelistdomain', acl.RestrictedWhitelistDomainHandler),

      # Internal AppEngine handling for blobstore uploads.
      webapp2.Route(
          r'/restricted/content/store_blobstore' + namespace_key +
            r'/<original_access_id:[^\/]+>',
          RestrictedStoreBlobstoreContentByHashHandler),

      # The public API:
      webapp2.Route(
          r'/content/contains' + namespace,
          ContainsHashHandler),
      webapp2.Route(
          r'/content/generate_blobstore_url' + namespace_key,
          GenerateBlobstoreHandler),
      webapp2.Route(r'/content/get_token', acl.GetTokenHandler),
      webapp2.Route(
          r'/content/store' + namespace_key, StoreContentByHashHandler),
      webapp2.Route(
          r'/content/retrieve' + namespace_key, RetrieveContentByHashHandler),

      # AppEngine-specific url:
      webapp2.Route(r'/_ah/warmup', WarmupHandler),

      # Must be last.
      webapp2.Route(r'/', RootHandler),
  ])


app = CreateApplication()
