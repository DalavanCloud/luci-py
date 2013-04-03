#!/usr/bin/python2.7
#
# Copyright 2012 Google Inc. All Rights Reserved.
#

"""Main entry point for TRS service.

This file contains the URL handlers for all the TRS service URLs, implemented
using the appengine webapp framework.
"""

import datetime
import json
import logging
import os.path
import urllib

from google.appengine.api import mail
from google.appengine.api import users
from google.appengine.ext import blobstore
from google.appengine.ext import db
from google.appengine.ext import ereporter
import webapp2
from google.appengine.ext.ereporter.report_generator import ReportGenerator
from google.appengine.ext.webapp import blobstore_handlers
from google.appengine.ext.webapp import template
from google.appengine.ext.webapp import util
from common import blobstore_helper
from common import test_request_message
from common import url_helper
from server import admin_user
from server import stats_manager
from server import test_manager
from server import user_manager
# pylint: enable-msg=C6204

_NUM_USER_TEST_RUNNERS_PER_PAGE = 50
_NUM_RECENT_ERRORS_TO_DISPLAY = 10

_HOME_LINK = '<a href=/secure/main>Home</a>'
_MACHINE_LIST_LINK = '<a href=/secure/machine_list>Machine List</a>'
_PROFILE_LINK = '<a href=/secure/user_profile>Profile</a>'
_STATS_LINK = '<a href=/secure/stats>Stats</a>'

_SECURE_CANCEL_URL = '/secure/cancel'
_SECURE_CHANGE_WHITELIST_URL = '/secure/change_whitelist'
_SECURE_DELETE_MACHINE_ASSIGNMENT_URL = '/secure/delete_machine_assignment'
_SECURE_GET_RESULTS_URL = '/secure/get_result'
_SECURE_MAIN_URL = '/secure/main'
_SECURE_USER_PROFILE_URL = '/secure/user_profile'

# Allow GET requests to be passed through as POST requests.
ALLOW_POST_AS_GET = False


def GenerateTopbar():
  """Generate the topbar to display on all server pages.

  Returns:
    The topbar to display.
  """
  if users.get_current_user():
    # TODO(user): These links should only be visible if the user is able to
    # access them (i.e. the user is an admin or has been given permission to
    # access these pages).
    topbar = ('%s |  <a href="%s">Sign out</a><br/> %s | %s | %s | %s' %
              (users.get_current_user().nickname(),
               users.create_logout_url('/'),
               _HOME_LINK,
               _PROFILE_LINK,
               _MACHINE_LIST_LINK,
               _STATS_LINK))
  else:
    topbar = '<a href="%s">Sign in</a>' % users.create_login_url('/')

  return topbar


def GenerateButtonWithHiddenForm(button_text, url, form_id):
  """Generate a button that when used will post to the given url.

  Args:
    button_text: The text to display on the button.
    url: The url to post to.
    form_id: The id to give the form.

  Returns:
    The html text to display the button.
  """
  button_html = '<form id="%s" method="post" action=%s>' % (form_id, url)
  button_html += (
      '<button onclick="document.getElementById(%s).submit()">%s</button>' %
      (form_id, button_text))
  button_html += '</form>'

  return button_html


def OnDevAppEngine():
  """Return True if this code is running on dev app engine.

  Returns:
    True if this code is running on dev app engine.
  """
  return os.environ['SERVER_SOFTWARE'].startswith('Development')


class MainHandler(webapp2.RequestHandler):
  """Handler for the main page of the web server.

  This handler lists all pending requests and allows callers to manage them.
  """

  def GetTimeString(self, dt):
    """Convert the datetime object to a user-friendly string.

    Arguments:
      dt: a datetime.datetime object.

    Returns:
      A string representing the datetime object to show in the web UI.
    """
    s = '--'

    if dt:
      midnight_today = datetime.datetime.now().replace(hour=0, minute=0,
                                                       second=0, microsecond=0)
      midnight_yesterday = midnight_today - datetime.timedelta(days=1)
      if dt > midnight_today:
        s = dt.strftime('Today at %H:%M')
      elif dt > midnight_yesterday:
        s = dt.strftime('Yesterday at %H:%M')
      else:
        s = dt.strftime('%d %b at %H:%M')

    return s

  def get(self):  # pylint: disable-msg=C6409
    """Handles HTTP GET requests for this handler's URL."""
    # Build info for test requests table.
    show_success = self.request.get('s', 'False') != 'False'
    sort_by = self.request.get('sort_by', 'reverse_chronological')
    page = int(self.request.get('page', 1))

    sorted_by_message = '<p>Currently sorted by: '
    ascending = True
    if sort_by == 'start':
      sorted_by_message += 'Start Time'
      sorted_by_query = 'created'
    elif sort_by == 'machine_id':
      sorted_by_message += 'Machine ID'
      sorted_by_query = 'machine_id'
    else:
      # The default sort.
      sorted_by_message += 'Reverse Start Time'
      sorted_by_query = 'created'
      ascending = False
    sorted_by_message += '</p>'

    runners = []
    for runner in test_manager.GetTestRunners(
        sorted_by_query,
        ascending=ascending,
        limit=_NUM_USER_TEST_RUNNERS_PER_PAGE,
        offset=_NUM_USER_TEST_RUNNERS_PER_PAGE * (page - 1)):
      # If this runner successfully completed, and we are not showing them,
      # just ignore it.
      if runner.done and runner.ran_successfully and not show_success:
        continue

      self._GetDisplayableRunnerTemplate(runner, detailed_output=True)
      runners.append(runner)

    errors = []
    query = test_manager.SwarmError.all().order('-created')
    for error in query.run(limit=_NUM_RECENT_ERRORS_TO_DISPLAY):
      error.log_time = self.GetTimeString(error.created)
      errors.append(error)

    if show_success:
      enable_success_message = """
        <a href="?s=False">Hide successfully completed tests</a>
      """
    else:
      enable_success_message = """
        <a href="?s=True">Show successfully completed tests too</a>
      """

    total_pages = (
        test_manager.TestRunner.all().count() / _NUM_USER_TEST_RUNNERS_PER_PAGE)

    params = {
        'topbar': GenerateTopbar(),
        'runners': runners,
        'errors': errors,
        'enable_success_message': enable_success_message,
        'sorted_by_message': sorted_by_message,
        'current_page': page,
        # Add 1 so the pages are 1-indexed.
        'total_pages': map(str, range(1, total_pages + 1, 1))
    }

    path = os.path.join(os.path.dirname(__file__), 'index.html')
    self.response.out.write(template.render(path, params))

  def _GetDisplayableRunnerTemplate(self, runner, detailed_output=False):
    """Puts different aspects of the runner in a displayable format.

    Args:
      runner: TestRunner object which will be displayed in Swarm server webpage.
      detailed_output: Flag specifying how detailed the output should be.
    """
    runner.name_string = runner.GetName()
    runner.key_string = str(runner.key())
    runner.status_string = '&nbsp;'
    runner.requested_on_string = self.GetTimeString(runner.created)
    runner.started_string = '--'
    runner.ended_string = '--'
    runner.machine_id_used = '&nbsp'
    runner.command_string = '&nbsp;'
    runner.failed_test_class_string = ''

    if not runner.started:
      runner.status_string = 'Pending'
      runner.command_string = GenerateButtonWithHiddenForm(
          'Cancel', '%s?r=%s' % (_SECURE_CANCEL_URL, runner.key_string),
          runner.key_string)

    elif not runner.done:
      if detailed_output:
        runner.status_string = 'Running on machine %s' % runner.machine_id
      else:
        runner.status_string = 'Running'

      runner.started_string = self.GetTimeString(runner.started)
      runner.machine_id_used = runner.machine_id
    else:
      runner.started_string = self.GetTimeString(runner.started)
      runner.ended_string = self.GetTimeString(runner.ended)

      runner.machine_id_used = runner.machine_id

      if runner.ran_successfully:
        if detailed_output:
          runner.status_string = (
              '<a title="Click to see results" href="%s?r=%s">Succeeded</a>' %
              (_SECURE_GET_RESULTS_URL, runner.key_string))
        else:
          runner.status_string = 'Succeeded'
      else:
        runner.failed_test_class_string = 'failed_test'
        runner.command_string = GenerateButtonWithHiddenForm(
            'Retry', '/secure/retry?r=%s' % runner.key_string,
            runner.key_string)
        if detailed_output:
          runner.status_string = (
              '<a title="Click to see results" href="%s?r=%s">Failed</a>' %
              (_SECURE_GET_RESULTS_URL, runner.key_string))
        else:
          runner.status_string = 'Failed'


class RedirectToMainHandler(webapp2.RequestHandler):
  """Handler to redirect requests to base page secured main page."""

  def get(self):  # pylint: disable-msg=C6409
    """Handles HTTP GET requests for this handler's URL."""
    self.redirect(_SECURE_MAIN_URL)


class MachineListHandler(webapp2.RequestHandler):
  """Handler for the machine list page of the web server.

  This handler lists all the machines that have ever polled the server and
  some basic information about them.
  """

  def get(self):  # pylint: disable-msg=C6409
    """Handles HTTP GET requests for this handler's URL."""
    sort_by = self.request.get('sort_by', 'machine_id')
    machines = test_manager.GetAllMachines(sort_by)

    # Add a delete option for each machine assignment.
    machines_displayable = []
    for machine in machines:
      machine.command_string = GenerateButtonWithHiddenForm(
          'Delete',
          '%s?r=%s' % (_SECURE_DELETE_MACHINE_ASSIGNMENT_URL, machine.key()),
          machine.key())
      machines_displayable.append(machine)

    params = {
        'topbar': GenerateTopbar(),
        'machines': machines_displayable
    }

    path = os.path.join(os.path.dirname(__file__), 'machine_list.html')
    self.response.out.write(template.render(path, params))


class DeleteMachineAssignmentHandler(webapp2.RequestHandler):
  """Handler to delete a machine assignment."""

  def post(self):  # pylint:disable-msg=C6409
    """Handles HTTP POST requests for this handler's URL."""
    key = self.request.get('r')

    if key and test_manager.DeleteMachineAssignment(key):
      self.response.out.write('Machine Assignment removed.')
    else:
      self.response.set_status(204)


class TestRequestHandler(webapp2.RequestHandler):
  """Handles test requests from clients."""

  def get(self):  # pylint: disable-msg=C6409
    if ALLOW_POST_AS_GET:
      return self.post()
    else:
      self.response.set_status(405)

  def post(self):  # pylint: disable-msg=C6409
    """Handles HTTP POST requests for this handler's URL."""
    if not AuthenticateRemoteMachine(self.request):
      SendAuthenticationFailure(self.request, self.response)
      return

    # Validate the request.
    if not self.request.get('request'):
      self.response.set_status(402)
      self.response.out.write('No request parameter found')
      return

    test_request_manager = CreateTestManager()
    try:
      response = json.dumps(test_request_manager.ExecuteTestRequest(
          self.request.get('request')))
    except test_request_message.Error as ex:
      message = str(ex)
      logging.exception(message)
      response = 'Error: %s' % message
    self.response.out.write(response)


class ResultHandler(webapp2.RequestHandler):
  """Handles test results from remote test runners."""

  def post(self):  # pylint: disable-msg=C6409
    """Handles HTTP POST requests for this handler's URL."""
    if not AuthenticateRemoteMachine(self.request):
      SendAuthenticationFailure(self.request, self.response)
      return

    # TODO(user): Share this code between all the request handlers so we
    # can always see how often a request is being sent.
    connection_attempt = self.request.get(url_helper.COUNT_KEY)
    if connection_attempt:
      logging.info('This is the %s connection attempt from this machine to '
                   'POST these results', connection_attempt)

    logging.debug('Received Result: %s', self.request.url)

    runner = None
    runner_key = self.request.get('r')
    if runner_key:
      runner = test_manager.TestRunner.get(runner_key)

    # Find the high level success/failure from the URL. We assume failure if
    # we can't find the success parameter in the request.
    success = self.request.get('s', 'False') == 'True'
    exit_codes = urllib.unquote_plus(self.request.get('x'))
    overwrite = self.request.get('o', 'False') == 'True'
    machine_id = urllib.unquote_plus(self.request.get('id'))

    # TODO(user): The result string should probably be in the body of the
    # request.
    result_string = urllib.unquote_plus(self.request.get(
        url_helper.RESULT_STRING_KEY))

    # Mark the runner as pinging now to prevent it from timing out while
    # the results are getting stored in the blobstore.
    test_manager.PingRunner(runner.key(), machine_id)

    # If we are on dev app engine we can't use the create_upload_url method
    # because it requires 2 threads, and dev app engine isn't multithreaded
    # (It needs this thread, and another thread to handle the url POSTFORM).
    result_blob_key = None
    if OnDevAppEngine():
      result_blob_key = blobstore_helper.CreateBlobstore(result_string)
    else:
      # Create the blobstore.
      upload_url = blobstore.create_upload_url('/upload')
      result_blob_key = url_helper.UrlOpen(
          upload_url, files=[('result', 'result', result_string)],
          max_tries=blobstore_helper.MAX_BLOBSTORE_WRITE_TRIES,
          wait_duration=0, method='POSTFORM')

    if result_blob_key is None:
      self.response.out.write('The server was unable to save the results to '
                              'the blobstore')
      self.response.set_status(500)
      return

    test_request_manager = CreateTestManager()
    if test_request_manager.UpdateTestResult(runner, machine_id,
                                             success=success,
                                             exit_codes=exit_codes,
                                             result_blob_key=result_blob_key,
                                             overwrite=overwrite):
      self.response.out.write('Successfully update the runner results.')
    else:
      self.response.set_status(400)
      self.response.out.write('Failed to update the runner results.')


class CleanupDataHandler(webapp2.RequestHandler):
  """Handles cron job to delete orphaned blobs."""

  def post(self):  # pylint: disable-msg=C6409
    test_manager.DeleteOldRunners()
    test_manager.DeleteOldErrors()
    test_manager.DeleteOrphanedBlobs()

    stats_manager.DeleteOldRunnerStats()

    self.response.out.write('Successfully cleaned up old data.')

  def get(self):  # pylint: disable-msg=C6409
    """Handles HTTP GET requests for this handler's URL."""
    # Only an app engine cron job is allowed to poll via get (it currently
    # has no way to make its request a post).
    if self.request.headers.get('X-AppEngine-Cron') != 'true':
      self.response.out.write('Only internal cron jobs can do this')
      self.response.set_status(405)
      return

    self.post()


class AbortStaleRunnersHandler(webapp2.RequestHandler):
  """Handles cron job to abort stale runners."""

  def post(self):  # pylint: disable-msg=C6409
    """Handles HTTP POST requests for this handler's URL."""
    test_request_manager = CreateTestManager()

    logging.debug('Polling')
    test_request_manager.AbortStaleRunners()
    self.response.out.write("""
    <html>
    <head>
    <title>Poll Done</title>
    </head>
    <body>Poll Done</body>
    </html>
    """)

  def get(self):  # pylint: disable-msg=C6409
    """Handles HTTP GET requests for this handler's URL."""
    # Only an app engine cron job is allowed to poll via get (it currently
    # has no way to make its request a post).
    if self.request.headers.get('X-AppEngine-Cron') != 'true':
      self.response.out.write('Only internal cron jobs can do this')
      self.response.set_status(405)
      return

    self.post()


class SendEReporterHandler(ReportGenerator):
  """Handles calling EReporter with the correct parameters."""

  def get(self):  # pylint: disable-msg=C6409
    # grab the mailing admin
    admin = admin_user.AdminUser.all().get()
    if not admin:
      # Create a dummy value so it can be edited from the datastore admin.
      admin = admin_user.AdminUser(email='')
      admin.put()

    if mail.is_email_valid(admin.email):
      self.request.GET['sender'] = admin.email
      super(SendEReporterHandler, self).get()
    else:
      self.response.out.write('Invalid admin email, \'%s\'. Must be a valid '
                              'email.' % admin.email)
      self.response.set_status(400)


class ShowMessageHandler(webapp2.RequestHandler):
  """Show the full text of a test request."""

  def get(self):  # pylint: disable-msg=C6409
    """Handles HTTP GET requests for this handler's URL."""
    self.response.headers['Content-Type'] = 'text/plain'

    key = self.request.get('r', '')
    if key:
      runner = test_manager.TestRunner.get(key)

    if runner:
      self.response.out.write(runner.GetMessage())
    else:
      self.response.out.write('Cannot find message for: %s' % key)


class StatsHandler(webapp2.RequestHandler):
  """Show all the collected swarm stats."""

  def get(self):  # pylint: disable-msg=C6409
    params = {
        'topbar': GenerateTopbar(),
        'runner_wait_stats': stats_manager.GetRunnerWaitStats(),
        'runner_cutoff': stats_manager.RUNNER_STATS_EVALUATION_CUTOFF_DAYS
    }

    path = os.path.join(os.path.dirname(__file__), 'stats.html')
    self.response.out.write(template.render(path, params))


class GetMatchingTestCasesHandler(webapp2.RequestHandler):
  """Get all the keys for any test runner that match a given test case name."""

  def get(self):  # pylint: disable-msg=C6409
    """Handles HTTP GET requests for this handler's URL."""
    if not AuthenticateRemoteMachine(self.request):
      SendAuthenticationFailure(self.request, self.response)
      return

    self.response.headers['Content-Type'] = 'text/plain'

    test_case_name = self.request.get('name', '')

    matches = test_manager.GetAllMatchingTestRequests(test_case_name)
    keys = []
    for match in matches:
      keys.extend(map(str, match.GetAllKeys()))

    if keys:
      self.response.out.write(json.dumps(keys))
    else:
      self.response.out.write('No matching Test Cases')


class SecureGetResultHandler(webapp2.RequestHandler):
  """Show the full result string from a test runner."""

  def get(self):  # pylint: disable-msg=C6409
    """Handles HTTP GET requests for this handler's URL."""
    SendRunnerResults(self.response, self.request.get('r', ''))


class GetResultHandler(webapp2.RequestHandler):
  """Show the full result string from a test runner."""

  def get(self):  # pylint: disable-msg=C6409
    """Handles HTTP GET requests for this handler's URL."""
    if not AuthenticateRemoteMachine(self.request):
      SendAuthenticationFailure(self.request, self.response)
      return

    key = self.request.get('r', '')
    SendRunnerResults(self.response, key)


class CleanupResultsHandler(webapp2.RequestHandler):
  """Delete the Test Runner with the given key."""

  def post(self):  # pylint: disable-msg=C6409
    """Handles HTTP POST requests for this handler's URL."""
    test_request_manager = CreateTestManager()

    if not AuthenticateRemoteMachine(self.request):
      SendAuthenticationFailure(self.request, self.response)
      return

    self.response.headers['Content-Type'] = 'test/plain'

    key = self.request.get('r', '')
    if test_request_manager.DeleteRunner(key):
      self.response.out.write('Key deleted.')
    else:
      self.response.out.write('Key deletion failed.')
      logging.warning('Unable to delete runner [key: %s]', str(key))


class CancelHandler(webapp2.RequestHandler):
  """Cancel a test runner that is not already running."""

  def post(self):  # pylint: disable-msg=C6409
    """Handles HTTP GET requests for this handler's URL."""
    self.response.headers['Content-Type'] = 'text/plain'

    key = self.request.get('r', '')
    if key:
      runner = test_manager.TestRunner.get(key)

    # Make sure found runner is not yet running.
    if runner and not runner.started:
      test_request_manager = CreateTestManager()
      test_request_manager.AbortRunner(
          runner, reason='Runner is not already running.')
      self.response.out.write('Runner canceled.')
    else:
      self.response.out.write('Cannot find runner or too late to cancel: %s' %
                              key)


class RetryHandler(webapp2.RequestHandler):
  """Retry a test runner again."""

  def post(self):  # pylint: disable-msg=C6409
    """Handles HTTP GET requests for this handler's URL."""
    self.response.headers['Content-Type'] = 'text/plain'

    key = self.request.get('r', '')
    runner = None
    if key:
      try:
        runner = test_manager.TestRunner.get(key)
      except db.BadKeyError:
        pass

    if runner:
      runner.started = None
      # Update the created time to make sure that retrying the runner does not
      # make it jump the queue and get executed before other runners for
      # requests added before the user pressed the retry button.
      runner.machine_id = None
      runner.done = False
      runner.created = datetime.datetime.now()
      runner.ran_successfully = False
      runner.automatic_retry_count = 0
      runner.put()

      self.response.out.write('Runner set for retry.')
    else:
      self.response.set_status(204)


class RegisterHandler(webapp2.RequestHandler):
  """Handler for the register_machine of the Swarm server.

     Attempt to find a matching job for the querying machine.
  """

  def get(self):  # pylint: disable-msg=C6409
    if ALLOW_POST_AS_GET:
      return self.post()
    else:
      self.response.set_status(405)

  def post(self):  # pylint: disable-msg=C6409
    """Handles HTTP POST requests for this handler's URL."""
    if not AuthenticateRemoteMachine(self.request):
      SendAuthenticationFailure(self.request, self.response)
      return

    # Validate the request.
    if not self.request.body:
      self.response.set_status(402)
      self.response.out.write('Request must have a body')
      return

    test_request_manager = CreateTestManager()
    attributes_str = self.request.get('attributes')
    try:
      attributes = json.loads(attributes_str)
    except (TypeError, ValueError):
      message = 'Invalid attributes: ' + attributes_str
      logging.exception(message)
      response = 'Error: %s' % message
      self.response.out.write(response)
      return

    try:
      response = json.dumps(
          test_request_manager.ExecuteRegisterRequest(attributes,
                                                      self.request.host_url))
    except test_request_message.Error as ex:
      message = str(ex)
      logging.exception(message)
      response = 'Error: %s' % message

    self.response.out.write(response)


class RunnerPingHandler(webapp2.RequestHandler):
  """Handler for runner pings to the server.

     The runner pings are used to let the server know a runner is still working,
     so it won't consider it stale.
  """

  def post(self):  # pylint: disable-msg=C6409
    """Handles HTTP POST requests for this handler's URL."""
    if not AuthenticateRemoteMachine(self.request):
      SendAuthenticationFailure(self.request, self.response)
      return

    key = self.request.get('r', '')
    machine_id = self.request.get('id', '')

    if test_manager.PingRunner(key, machine_id):
      self.response.out.write('Runner successfully pinged.')
    else:
      self.response.set_status(402)
      self.response.out.write('Runner failed to ping.')


class UserProfileHandler(webapp2.RequestHandler):
  """Handler for the user profile page of the web server.

  This handler lists user info, such as their IP whitelist and settings.
  """

  def get(self):  # pylint: disable-msg=C6409
    """Handles HTTP GET requests for this handler's URL."""
    topbar = GenerateTopbar()

    display_whitelists = []

    for stored_whitelist in user_manager.MachineWhitelist().all():
      whitelist = {}
      whitelist['ip'] = stored_whitelist.ip
      whitelist['password'] = stored_whitelist.password
      whitelist['key'] = stored_whitelist.key()
      whitelist['url'] = _SECURE_CHANGE_WHITELIST_URL
      display_whitelists.append(whitelist)

    params = {
        'topbar': topbar,
        'whitelists': display_whitelists,
        'change_whitelist_url': _SECURE_CHANGE_WHITELIST_URL
    }

    path = os.path.join(os.path.dirname(__file__), 'user_profile.html')
    self.response.out.write(template.render(path, params))


class ChangeWhitelistHandler(webapp2.RequestHandler):
  """Handler for making changes to a user whitelist."""

  def post(self):  # pylint: disable-msg=C6409
    """Handles HTTP POST requests for this handler's URL."""
    ip = self.request.get('i', self.request.remote_addr)

    password = self.request.get('p', None)
    # Make sure a password '' sent by the form is stored as None.
    if not password:
      password = None

    add = self.request.get('a')
    if add == 'True':
      user_manager.AddWhitelist(ip, password)
    elif add == 'False':
      user_manager.DeleteWhitelist(ip)

    self.redirect(_SECURE_USER_PROFILE_URL, permanent=True)


class RemoteErrorHandler(webapp2.RequestHandler):
  """Handler to log an error reported by remote machine."""

  def post(self):  # pylint: disable-msg=C6409
    """Handles HTTP POST requests for this handler's URL."""
    if not AuthenticateRemoteMachine(self.request):
      SendAuthenticationFailure(self.request, self.response)
      return

    error_message = self.request.get('m', '')
    error = test_manager.SwarmError(
        name='Remote Error Report', message=error_message,
        info='Remote machine address: %s' % self.request.remote_addr)
    error.put()

    self.response.out.write('Error logged')


class UploadHandler(blobstore_handlers.BlobstoreUploadHandler):
  def post(self):  # pylint: disable-msg=C6409
    """Handles HTTP POST requests for this handler's URL."""
    upload_result = self.get_uploads('result')

    if len(upload_result) != 1:
      self.response.set_status(403)
      self.response.out.write('Expected 1 upload but received %d',
                              len(upload_result))

      blobstore.delete_async((b.key() for b in upload_result))
      return

    blob_info = upload_result[0]
    self.response.out.write(blob_info.key())


def AuthenticateRemoteMachine(request):
  """Check to see if the request is from a whitelisted machine.

  Will use the remote machine's IP and provided password (if any).

  Args:
    request: WebAPP request sent by remote machine.

  Returns:
    True if the request is from a whitelisted machine.
  """
  return user_manager.IsWhitelistedMachine(
      request.remote_addr, request.get('password', None))


def SendAuthenticationFailure(request, response):
  """Writes an authentication failure error message to response with status.

  Args:
    request: The original request that failed to authenticate.
    response: Response to be sent to remote machine.
  """
  # Log the error.
  error = test_manager.SwarmError(
      name='Authentication Failure', message='Handler: %s' % request.url,
      info='Remote machine address: %s' % request.remote_addr)
  error.put()

  response.set_status(403)
  response.out.write('Remote machine not whitelisted for operation')


def SendRunnerResults(response, key):
  """Sends the results of the runner specified by key.

  Args:
    response: Response to be sent to remote machine.
    key: Key identifying the runner.
  """
  response.headers['Content-Type'] = 'text/plain'
  test_request_manager = CreateTestManager()
  results = test_request_manager.GetRunnerResults(key)

  if results:
    response.out.write(json.dumps(results))
  else:
    response.set_status(204)
    logging.info('Unable to provide runner results [key: %s]', str(key))


def CreateTestManager():
  """Creates and returns a test manager instance.

  Returns:
    A TestManager instance.
  """
  return test_manager.TestRequestManager()


def CreateApplication():
  return webapp2.WSGIApplication([('/', RedirectToMainHandler),
                                  ('/cleanup_results',
                                   CleanupResultsHandler),
                                  ('/get_matching_test_cases',
                                   GetMatchingTestCasesHandler),
                                  ('/get_result', GetResultHandler),
                                  ('/poll_for_test', RegisterHandler),
                                  ('/remote_error', RemoteErrorHandler),
                                  ('/result', ResultHandler),
                                  ('/runner_ping', RunnerPingHandler),
                                  ('/secure/machine_list', MachineListHandler),
                                  ('/secure/retry', RetryHandler),
                                  ('/secure/show_message',
                                   ShowMessageHandler),
                                  ('/secure/stats', StatsHandler),
                                  ('/tasks/abort_stale_runners',
                                   AbortStaleRunnersHandler),
                                  ('/tasks/cleanup_data', CleanupDataHandler),
                                  ('/tasks/sendereporter',
                                   SendEReporterHandler),
                                  ('/test', TestRequestHandler),
                                  ('/upload', UploadHandler),
                                  (_SECURE_CANCEL_URL, CancelHandler),
                                  (_SECURE_CHANGE_WHITELIST_URL,
                                   ChangeWhitelistHandler),
                                  (_SECURE_DELETE_MACHINE_ASSIGNMENT_URL,
                                   DeleteMachineAssignmentHandler),
                                  (_SECURE_GET_RESULTS_URL,
                                   SecureGetResultHandler),
                                  (_SECURE_MAIN_URL, MainHandler),
                                  (_SECURE_USER_PROFILE_URL,
                                   UserProfileHandler)],
                                 debug=True)

ereporter.register_logger()
app = CreateApplication()
