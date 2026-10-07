"""Behavior tests for the connector helper; no network."""
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
import urllib.error
from unittest.mock import patch

SCRIPT = Path(__file__).parents[1] / 'scripts/connector.py'
TOKEN = 'tok-' + 'x' * 40


def load_module(configs_dir):
    with patch.dict(os.environ, {'CONNECTOR_CONFIGS_DIR': str(configs_dir)}):
        spec = importlib.util.spec_from_file_location('connector_skill', SCRIPT)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    return module


class FakeResponse:
    def __init__(self, body, status=200):
        self.body = body.encode()
        self.status = status

    def read(self, _limit=None):
        return self.body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class ConnectorHelperTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.dir = Path(self.temp.name)
        self.write('google_calendar', {
            'auth_type': 'oauth', 'access_token': TOKEN, 'refresh_token': 'r', 'refresh': True,
            'expires_at': int(time.time()) + 3600, 'user_email': 'me@example.com',
            'scopes': ['https://www.googleapis.com/auth/calendar.events'],
        })
        self.mod = load_module(self.dir)

    def write(self, code, entry):
        (self.dir / f'{code}_access_tokens.json').write_text(json.dumps({'connectors': {code: entry}}))

    def run_main(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = self.mod.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def test_list_names_connector_without_secret(self):
        code, out, _ = self.run_main('list')
        self.assertEqual(code, 0)
        self.assertIn('google_calendar: connected (me@example.com, oauth)', out)
        self.assertNotIn(TOKEN, out)

    def test_list_reports_unreadable_file_as_verification_failure(self):
        (self.dir / 'broken_access_tokens.json').write_text('{not json')
        code, _, err = self.run_main('list')
        self.assertEqual(code, 3)
        self.assertIn('verification failed', err)

    def test_info_has_no_secret(self):
        code, out, _ = self.run_main('info', 'google_calendar')
        self.assertEqual(code, 0)
        info = json.loads(out)
        self.assertTrue(info['auto_refresh'])
        self.assertFalse(info['expired'])
        self.assertNotIn(TOKEN, out)

    def test_call_sends_header_and_encodes_query(self):
        seen = {}

        def fake_urlopen(req, timeout):
            seen['url'] = req.full_url
            seen['auth'] = req.get_header('Authorization')
            return FakeResponse('{"items": []}')

        with patch.object(self.mod.urllib.request, 'urlopen', fake_urlopen):
            code, out, _ = self.run_main(
                'call', 'google_calendar', 'GET',
                'https://www.googleapis.com/calendar/v3/calendars/primary/events',
                '--query', 'timeMin=2026-07-13T00:00:00+07:00')
        self.assertEqual(code, 0)
        self.assertEqual(seen['auth'], 'Bearer ' + TOKEN)
        self.assertIn('timeMin=2026-07-13T00%3A00%3A00%2B07%3A00', seen['url'])
        self.assertEqual(json.loads(out), {'items': []})

    def test_http_error_goes_to_stderr_only(self):
        def fake_urlopen(req, timeout):
            raise urllib.error.HTTPError(req.full_url, 401, 'Unauthorized', {}, io.BytesIO(b'{"error":{"code":401}}'))

        with patch.object(self.mod.urllib.request, 'urlopen', fake_urlopen):
            code, out, err = self.run_main('call', 'google_calendar', 'GET', 'https://www.googleapis.com/x')
        self.assertEqual(code, 1)
        self.assertEqual(out, '')
        self.assertIn('HTTP 401 from www.googleapis.com', err)
        self.assertIn('auto_refresh=yes', err)

    def test_refuses_foreign_host_for_known_connector(self):
        with patch.object(self.mod.urllib.request, 'urlopen') as urlopen:
            code, _, err = self.run_main('call', 'google_calendar', 'GET', 'https://evil.example.com/collect')
        self.assertEqual(code, 4)
        self.assertIn('refusing', err)
        urlopen.assert_not_called()

    def test_refuses_lookalike_host(self):
        code, _, _ = self.run_main('call', 'google_calendar', 'GET', 'https://googleapis.com.evil.example/x')
        self.assertEqual(code, 4)

    def test_refuses_plain_http(self):
        code, _, _ = self.run_main('call', 'google_calendar', 'GET', 'http://www.googleapis.com/x')
        self.assertEqual(code, 4)

    def test_refuses_caller_authorization_header(self):
        code, _, err = self.run_main('call', 'google_calendar', 'GET', 'https://www.googleapis.com/x',
                                     '--header', 'Authorization: Basic abc')
        self.assertEqual(code, 2)
        self.assertIn('Authorization', err)

    def test_google_app_password_is_refused(self):
        self.write('gmail', {'auth_type': 'pat', 'api_key': 'app-pass', 'credentials': {'email': 'me@example.com'}})
        code, _, err = self.run_main('call', 'gmail', 'GET', 'https://gmail.googleapis.com/gmail/v1/users/me/messages')
        self.assertEqual(code, 3)
        self.assertIn('IMAP', err)

    def test_not_connected(self):
        code, _, err = self.run_main('call', 'google_drive', 'GET', 'https://www.googleapis.com/drive/v3/files')
        self.assertEqual(code, 3)
        self.assertIn('not connected', err)

    def test_token_param_uses_query_not_header(self):
        self.write('facebook', {'api_key': TOKEN, 'credentials': {'page_id': '123'}})
        seen = {}

        def fake_urlopen(req, timeout):
            seen['url'] = req.full_url
            seen['auth'] = req.get_header('Authorization')
            return FakeResponse('{"success": true}')

        with patch.object(self.mod.urllib.request, 'urlopen', fake_urlopen):
            code, out, _ = self.run_main('call', 'facebook', 'DELETE', 'https://graph.facebook.com/v19.0/1_2',
                                         '--token-param', 'access_token')
        self.assertEqual(code, 0)
        self.assertIsNone(seen['auth'])
        self.assertIn('access_token=' + TOKEN, seen['url'])
        self.assertNotIn(TOKEN, out)

    def test_response_echoing_token_is_scrubbed(self):
        with patch.object(self.mod.urllib.request, 'urlopen', lambda req, timeout: FakeResponse(TOKEN)):
            code, out, _ = self.run_main('call', 'google_calendar', 'GET', 'https://www.googleapis.com/x')
        self.assertEqual(code, 0)
        self.assertNotIn(TOKEN, out)

    def test_multipart_upload(self):
        image = self.dir / 'pic.jpg'
        image.write_bytes(b'\xff\xd8data')
        body, ctype = self.mod.multipart([('source', '@' + str(image)), ('message', 'hi')])
        self.assertTrue(ctype.startswith('multipart/form-data; boundary='))
        self.assertIn(b'filename="pic.jpg"', body)
        self.assertIn(b'\xff\xd8data', body)
        self.assertIn(b'name="message"\r\n\r\nhi', body)


if __name__ == '__main__':
    unittest.main()
