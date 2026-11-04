"""Full handler pipeline with real SDK serialization and mock provider HTTP only."""
import hashlib
from tests.legacy_support import setUpModule, tearDownModule
import hmac
import json
import tempfile
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import httpx
from fastapi import BackgroundTasks
from starlette.requests import Request
from app import config,db,main,whatsapp
try:
    from google import genai
    from google.genai import types
    HAS_SDK=True
except ImportError:
    HAS_SDK=False


@unittest.skipUnless(HAS_SDK,'google-genai is required for actual transport test')
class TextPipelineTests(unittest.IsolatedAsyncioTestCase):
    async def test_signed_text_through_gemini_sqlite_and_meta_then_confirmation(self):
        google_calls=[]
        meta_calls=[]
        data={'intent':'machinery_request','machine_type':'tractor','operation':'ploughing',
              'area_hectares':2,'location':'Doukkala','preferred_date':'2027-02-10'}
        def google_response(req):
            google_calls.append(req)
            return httpx.Response(200,json={'candidates':[{'content':{'parts':[{'text':json.dumps(data)}]},'finishReason':'STOP'}]})
        def meta_response(req):
            meta_calls.append(req)
            return httpx.Response(200,json={'messages':[{'id':'test-outbound'}]})
        real_client=httpx.AsyncClient
        gemini_client=genai.Client(api_key='test-only',http_options=types.HttpOptions(
            httpx_async_client=real_client(transport=httpx.MockTransport(google_response))))
        meta_http=SimpleNamespace(AsyncClient=lambda **kw:real_client(transport=httpx.MockTransport(meta_response),**kw),HTTPStatusError=httpx.HTTPStatusError)
        async def incoming(msg_id,text):
            body=json.dumps({'entry':[{'changes':[{'value':{'messages':[{
                'id':msg_id,'from':'test-farmer','type':'text','text':{'body':text}}]}}]}]}).encode()
            signature='sha256='+hmac.new(b'test-secret',body,hashlib.sha256).hexdigest()
            async def receive(): return {'type':'http.request','body':body,'more_body':False}
            req=Request({'type':'http','method':'POST','path':'/webhook/whatsapp','query_string':b'',
                         'headers':[(b'x-hub-signature-256',signature.encode())]},receive)
            tasks=BackgroundTasks()
            self.assertEqual(await main.receive_webhook(req,tasks),{'received':True})
            await tasks()
        with tempfile.TemporaryDirectory() as temp,patch.object(db,'DB_PATH',str(Path(temp)/'test.db')),patch.object(config,'APP_SECRET','test-secret'),patch.object(config,'ACCESS_TOKEN','test-token'),patch.object(config,'PHONE_NUMBER_ID','test-phone'),patch.object(config,'GEMINI_API_KEY','test-only'),patch.object(config,'GEMINI_MODEL','configured-live-model'),patch.object(config,'LLM_PROVIDER','gemini'),patch.object(config,'LLM_FALLBACK_PROVIDER',''),patch.object(genai,'Client',return_value=gemini_client),patch.object(whatsapp,'httpx',meta_http):
            db.init_db()
            await incoming('one','بغيت تراكتور للحرث جوج هكتارات فدكالة 2027-02-10')
            with db.connect() as con:
                self.assertEqual(con.execute('SELECT count(*) FROM requests').fetchone()[0],0)
                self.assertEqual(con.execute('SELECT awaiting_confirmation FROM conversations').fetchone()[0],1)
            self.assertEqual(len(google_calls),1)
            self.assertIn('configured-live-model',google_calls[0].url.path)
            self.assertIn('responseJsonSchema',json.loads(google_calls[0].content)['generationConfig'])
            self.assertEqual(len(meta_calls),1)
            self.assertIn('واش هاد المعلومات صحيحة',json.loads(meta_calls[0].content)['text']['body'])
            self.assertTrue(meta_calls[0].url.path.endswith('/test-phone/messages'))
            await incoming('two','نعم')
            await incoming('two','نعم')
            with db.connect() as con:
                self.assertEqual(con.execute('SELECT status FROM requests').fetchall()[0][0],'pending_review')
                self.assertEqual(con.execute('SELECT count(*) FROM requests').fetchone()[0],1)
            self.assertEqual(len(google_calls),1)
            self.assertEqual(len(meta_calls),2)
        gemini_client.close()


if __name__=='__main__': unittest.main()
