import json
from tests.legacy_support import setUpModule, tearDownModule
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock,patch
import httpx
from app import ai,config,db,service,whatsapp
from app.diagnostics import event


class DiagnosticTests(unittest.IsolatedAsyncioTestCase):
    def test_allowlist_drops_private_fields(self):
        with self.assertLogs('nouba.diagnostics',level='INFO') as logs:
            event('test',phone='212600000000',text='private-message',api_key='secret-key',outcome='ok')
        output=' '.join(logs.output)
        for secret in ['212600000000','private-message','secret-key']:
            self.assertNotIn(secret,output)
        record=json.loads(logs.records[0].message)
        self.assertEqual(record['stage'],'test')
        self.assertIn('duration_ms',record)

    async def test_meta_error_logs_status_without_response_contents(self):
        real_client=httpx.AsyncClient
        def respond(request):
            return httpx.Response(401,json={'error':{'code':190,'message':'private-secret-error'}})
        def factory(**kw):
            return real_client(transport=httpx.MockTransport(respond),**kw)
        with patch.object(config,'ACCESS_TOKEN','private-token'),patch.object(config,'PHONE_NUMBER_ID','test-phone-id'),patch.object(whatsapp.httpx,'AsyncClient',side_effect=factory),self.assertLogs('nouba.diagnostics',level='INFO') as logs:
            with self.assertRaises(httpx.HTTPStatusError):
                await whatsapp.send_text('212600000000','private-message')
        output=' '.join(logs.output)
        for value in ['private-token','212600000000','private-message','private-secret-error']:
            self.assertNotIn(value,output)
        record=json.loads(logs.records[-1].message)
        self.assertEqual(record['meta_http_status'],401)
        self.assertEqual(record['meta_error_code'],190)
        self.assertEqual(record['exception_type'],'HTTPStatusError')

    async def test_ai_failure_and_failed_fallback_are_observable(self):
        with tempfile.TemporaryDirectory() as temp,patch.object(db,'DB_PATH',str(Path(temp)/'test.db')):
            db.init_db()
            with patch.object(service,'extract',AsyncMock(side_effect=ai.AIError('gemini_invalid_json'))),patch.object(service,'send_text',AsyncMock(side_effect=RuntimeError('private-secret'))),self.assertLogs('nouba.diagnostics',level='INFO') as logs:
                await service.handle_message({'from':'212600000000','id':'test-only','type':'text','text':{'body':'private-message'}})
            records=[json.loads(r.message) for r in logs.records]
            self.assertTrue(any(r['stage']=='conversation.fallback' and r['gemini_error_code']=='gemini_invalid_json' for r in records))
            self.assertTrue(any(r['stage']=='reply.fallback_failed' for r in records))
            for secret in ['212600000000','private-message','private-secret']:
                self.assertNotIn(secret,' '.join(logs.output))
            self.assertTrue(all(r['trace_id'] for r in records))


if __name__=='__main__': unittest.main()
