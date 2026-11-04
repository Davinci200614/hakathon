"""Real farmer wording through Ollama HTTP, deterministic state and SQLite."""
import importlib
from tests.legacy_support import setUpModule, tearDownModule
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch
import httpx
from app import config, db, extraction, llm, service


class IntelligenceTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_conversation_and_restart(self):
        real = httpx.AsyncClient
        contexts = []
        def respond(req):
            ctx = json.loads(json.loads(req.content)['messages'][1]['content'])
            contexts.append(ctx)
            # Reproduce the bad model output, including stale correction values.
            data = {'machine_type':'unknown','location':'جوج'} if len(contexts)==1 else {'location':'جوج'}
            return httpx.Response(200,json={'done':True,'message':{'content':json.dumps(data)}})
        async def turn(mid, text):
            await service.handle_message({'from':'farmer','id':str(mid),'type':'text','text':{'body':text}})
        with tempfile.TemporaryDirectory() as temp, patch.object(db,'DB_PATH',str(Path(temp)/'test.db')), patch.multiple(config,LLM_PROVIDER='ollama',LLM_FALLBACK_PROVIDER=''), patch.object(httpx,'AsyncClient',side_effect=lambda **kw:real(transport=httpx.MockTransport(respond),**kw)):
            db.init_db()
            with patch.object(service,'send_text',AsyncMock()) as send:
                await turn(1,'بغيت تراكتور يحرث ليا جوج هكتارات')
                self.assertEqual(send.call_args.args[1],service.QUESTIONS['location'])
                self.assertIsNone(service.conversation('farmer')[0]['location'])
                await turn(2,'دكالة')
                self.assertEqual(send.call_args.args[1],service.QUESTIONS['preferred_date'])
                await turn(3,'2026/11/05')
                self.assertIn('2026-11-05',send.call_args.args[1])
                self.assertTrue(service.conversation('farmer')[2])
                await turn(4,'لا')
                self.assertEqual(send.call_args.args[1],'شنو المعلومة اللي بغيتي تصحح؟')
            importlib.reload(service)
            db.init_db()
            with patch.object(service,'send_text',AsyncMock()) as send:
                await turn(5,'أنا ف بني مسكين مشي دكالة')
                state = service.conversation('farmer')[0]
                for key,value in {'machine_type':'tractor','operation':'plowing','area_hectares':2,'location':'بني مسكين','preferred_date':'2026-11-05'}.items():
                    self.assertEqual(state[key],value)
                self.assertIn('بني مسكين',send.call_args.args[1])
                await turn(6,'نعم')
                await turn(6,'نعم')
                await turn(7,'نعم')
            with db.connect() as con:
                rows=con.execute('SELECT status FROM requests').fetchall()
                self.assertEqual([r[0] for r in rows],['pending_review'])
            self.assertEqual(len(contexts),3)  # dates and controls never invoke the model
            self.assertEqual(contexts[-1]['question']['pending_field'],'correction')

    def test_invalid_fields_and_corrections(self):
        old={'intent':'request_machine','location':'جوج','machine_type':'unknown','operation':'unknown'}
        result=service.merge_fields(old,extraction.deterministic_patch('المكان دكالة'))
        self.assertEqual(result['location'],'دكالة')
        self.assertIsNone(result['machine_type'])
        self.assertIsNone(result['operation'])
        self.assertEqual(service.merge_fields(result,{'location':None})['location'],'دكالة')
        self.assertEqual(service.merge_fields(result,{'location':'unknown'})['location'],'دكالة')
        self.assertEqual(extraction.resolve_date('2026/11/03')[0],'2026-11-03')
        self.assertEqual(extraction.resolve_date('2026/02/30'),(None,True))

    async def test_unknown_fields_cannot_arm_confirmation_and_questions_progress(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(db,'DB_PATH',str(Path(temp)/'test.db')), patch.object(service,'send_text',AsyncMock()) as send, patch.object(service,'extract',AsyncMock(return_value={'intent':'request_machine','machine_type':'unknown','operation':'unknown','location':'جوج'})):
            db.init_db()
            replies=[]
            for index in range(5):
                await service.handle_message({'from':'partial','id':str(index),'type':'text','text':{'body':'ما عرفت'}})
                replies.append(send.call_args.args[1])
                self.assertFalse(service.conversation('partial')[2])
            self.assertEqual(len(set(replies)),5)
            await service.handle_message({'from':'partial','id':'yes','type':'text','text':{'body':'نعم'}})
            with db.connect() as con:
                self.assertEqual(con.execute('SELECT count(*) FROM requests').fetchone()[0],0)
