"""Signed inbound interactive messages through real controller and mocked Meta HTTP."""
import hashlib
import hmac
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch
import httpx
from fastapi import BackgroundTasks
from starlette.requests import Request
from app import config, conversation, db, main, whatsapp
from app.contacts import valid_contact


class GuidedTransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_native_list_and_button_round_trip_without_language_model(self):
        real = httpx.AsyncClient
        calls = []
        def meta(req):
            calls.append(json.loads(req.content))
            return httpx.Response(200, json={'messages': [{'id': 'outbound'}]})
        async def incoming(index, text=None, selection=None, button=False):
            msg = {'id': str(index), 'from': '212612345678'}
            if selection:
                subtype = 'button_reply' if button else 'list_reply'
                msg.update(type='interactive', interactive={'type': subtype, subtype: {'id': selection, 'title': 'اختيار'}})
            else:
                msg.update(type='text', text={'body': text})
            body = json.dumps({'entry': [{'changes': [{'value': {'messages': [msg]}}]}]}).encode()
            signature = 'sha256=' + hmac.new(b'test-secret', body, hashlib.sha256).hexdigest()
            async def receive(): return {'type': 'http.request', 'body': body, 'more_body': False}
            request = Request({'type': 'http', 'method': 'POST', 'path': '/webhook/whatsapp', 'query_string': b'',
                               'headers': [(b'x-hub-signature-256', signature.encode())]}, receive)
            tasks = BackgroundTasks()
            self.assertEqual(await main.receive_webhook(request, tasks), {'received': True})
            await tasks()
        def selected(index):
            with db.connect() as con:
                return list(conversation.context_data(con, '212612345678')['choices'])[index-1]
        with tempfile.TemporaryDirectory() as temp, patch.object(db, 'DB_PATH', str(Path(temp)/'test.db')), patch.multiple(config, CONVERSATION_CORE='v2', APP_SECRET='test-secret', ACCESS_TOKEN='test-token', PHONE_NUMBER_ID='test-phone'), patch.object(whatsapp.httpx, 'AsyncClient', side_effect=lambda **kw: real(transport=httpx.MockTransport(meta), **kw)), patch.object(conversation, 'understand', AsyncMock()) as nlu:
            db.init_db()
            await incoming(1, text='menu')
            self.assertEqual(calls[-1]['type'], 'interactive')
            self.assertEqual(calls[-1]['interactive']['type'], 'list')
            await incoming(2, selection=selected(1))
            for index, text in enumerate(['حميد', 'سلا جديدة', 'حصاد', 'متوفر غدا', 'hamid@example.com'], 3):
                await incoming(index, text=text)
            self.assertEqual(calls[-1]['interactive']['type'], 'button')
            confirm = selected(1)
            await incoming(8, selection=confirm, button=True)
            await incoming(8, selection=confirm, button=True)
            with db.connect() as con:
                self.assertEqual(con.execute('SELECT count(*) FROM employment_listings').fetchone()[0], 1)
                self.assertEqual(con.execute('SELECT contact FROM employment_listings').fetchone()[0], 'hamid@example.com')
            for call in calls:
                if call['type'] == 'interactive':
                    interactive = call['interactive']
                    self.assertLessEqual(len(interactive['body']['text']), 1024)
                    if interactive['type'] == 'button':
                        self.assertLessEqual(len(interactive['action']['buttons']), 3)
                    else:
                        rows = interactive['action']['sections'][0]['rows']
                        self.assertLessEqual(len(rows), 10)
                        self.assertTrue(all(len(row['title']) <= 24 for row in rows))
            nlu.assert_not_awaited()

    def test_concrete_contact_validation(self):
        for value in ['WhatsApp', '2ma faecbook wla WhatsApp', '066666245', 'باقي معطيتكش وسيلة الاتصال']:
            self.assertIsNone(valid_contact(value), value)
        self.assertEqual(valid_contact('0612345678 و hamid@example.com'), '0612345678 / hamid@example.com')
        self.assertEqual(valid_contact('https://facebook.com/hamid.test'), 'https://facebook.com/hamid.test')
        self.assertEqual(valid_contact('@hamid.test'), '@hamid.test')


if __name__ == '__main__':
    unittest.main()
