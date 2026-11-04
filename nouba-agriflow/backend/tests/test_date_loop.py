"""Regression for actual extraction/normalization/SQLite/Meta date flow."""
import asyncio
from tests.legacy_support import setUpModule, tearDownModule
import importlib
import json
import tempfile
import subprocess
import sys
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch
import httpx
from app import config, db, extraction, llm, service, whatsapp


class DateLoopTests(unittest.IsolatedAsyncioTestCase):
    async def test_http_pipeline_restart_confirmation_and_no_duplicates(self):
        real_client = httpx.AsyncClient
        replies = []
        inputs = []
        patches = iter([
            {'intent':'machinery_request','machine_type':'tractor','operation':'plowing','area_hectares':2},
            {'location':'الرماني'}, {'preferred_date':None},
            {'preferred_date':None}, {'preferred_date':None}, {'preferred_date':None},
            {'preferred_date':None},
        ])
        def respond(req):
            body = json.loads(req.content)
            if req.url.path == '/api/chat':
                inputs.append(json.loads(body['messages'][1]['content']))
                return httpx.Response(200,json={'done':True,'message':{'content':json.dumps(next(patches))}})
            replies.append(body['text']['body'])
            return httpx.Response(200,json={'messages':[{'id':'out'}]})
        async def incoming(mid,text):
            await service.handle_message({'from':'private-farmer','id':mid,'type':'text','text':{'body':text}})
        with tempfile.TemporaryDirectory() as temp, patch.object(db,'DB_PATH',str(Path(temp)/'test.db')), patch.multiple(config,LLM_PROVIDER='ollama',LLM_FALLBACK_PROVIDER='',ACCESS_TOKEN='test',PHONE_NUMBER_ID='test'), patch.object(extraction,'casablanca_today',return_value=date(2026,11,3)), patch.object(llm,'casablanca_today',return_value=date(2026,11,3)), patch.object(httpx,'AsyncClient',side_effect=lambda **kw:real_client(transport=httpx.MockTransport(respond),**kw)):
            db.init_db()
            await incoming('1','بغيت تراكتور يحرث ليا جوج هكتارات')
            self.assertEqual(replies[-1], service.QUESTIONS['location'])
            await incoming('2','فالرماني')
            self.assertEqual(replies[-1],'فاش بغيتي التراكتور يخدم ليك؟')
            # Recreate app service and startup schema; all conversation state is SQLite.
            importlib.reload(service)
            db.init_db()
            await incoming('3','غدا')
            for value in ('tractor','plowing','2.0','الرماني','2026-11-04'):
                self.assertIn(value,replies[-1])
            self.assertTrue(service.conversation('private-farmer')[2])
            await incoming('4','03/04')
            self.assertEqual(service.conversation('private-farmer')[0]['preferred_date'], '2026-11-04')
            self.assertEqual(service.missing_fields(service.conversation('private-farmer')[0]), ['preferred_date'])
            await incoming('5','ما عرفت')
            first_clarification=replies[-1]
            await incoming('6','ما عرفت')
            self.assertNotEqual(first_clarification,replies[-1])
            self.assertIn('2026-11-05', replies[-1])
            await incoming('7','5 نونبر')
            self.assertIn('2026-11-05', replies[-1])
            # A fresh Python process imports the FastAPI app and confirms using
            # only the persisted draft and delivered-summary confirmation flag.
            restarted = subprocess.run([sys.executable, '-c', """
import asyncio, sys
from unittest.mock import AsyncMock
from app import db, main, service
db.DB_PATH = sys.argv[1]
db.init_db()
service.send_text = AsyncMock()
assert service.conversation('private-farmer')[2]
asyncio.run(service.handle_message({'from':'private-farmer','id':'8','type':'text','text':{'body':'نعم'}}))
assert service.send_text.await_count == 1
""", str(Path(temp)/'test.db')], capture_output=True, text=True)
            self.assertEqual(restarted.returncode, 0, restarted.stderr)
            await incoming('8','نعم')
            await incoming('8','نعم')
            await incoming('9','نعم')
            with db.connect() as con:
                rows=con.execute('SELECT payload,status FROM requests').fetchall()
                self.assertEqual(len(rows),1)
                self.assertEqual(rows[0]['status'],'pending_review')
                payload=json.loads(rows[0]['payload'])
                self.assertEqual(payload['preferred_date'],'2026-11-05')
                self.assertEqual(payload['location'],'الرماني')
                self.assertEqual(payload['area_hectares'],2)
            self.assertTrue(all(item['today']=='2026-11-03' for item in inputs))
            self.assertEqual(len(inputs),7)

    def test_supported_dates_and_year_rollover(self):
        for text,expected in [('اليوم','2026-11-03'),('lyom','2026-11-03'),('غدا','2026-11-04'),('ghdda','2026-11-04'),('demain','2026-11-04'),('بعد غدا','2026-11-05'),('الثلاثاء الجاي','2026-11-10'),('نهار الجمعة','2026-11-06'),('5 نونبر','2026-11-05'),('5 novembre','2026-11-05'),('2026-11-05','2026-11-05'),('2 نونبر','2027-11-02'),('31 نونبر',None),('03/04',None)]:
            with self.subTest(text=text):
                self.assertEqual(extraction.resolve_date(text,date(2026,11,3)),(expected,True))

    def test_model_date_fallback_and_ambiguous_override(self):
        model = extraction.Extraction(preferred_date=date(2026,11,12))
        result = extraction.normalized_patch(model, 'douze novembre 2026', date_context=True)
        self.assertEqual(result['preferred_date'], '2026-11-12')
        for text in ('03/04', 'غدا ولا بعد غدا'):
            result = extraction.normalized_patch(model, text, date_context=True)
            self.assertIsNone(result['preferred_date'])
        self.assertNotIn('preferred_date', extraction.normalized_patch(model, 'فالرماني'))
