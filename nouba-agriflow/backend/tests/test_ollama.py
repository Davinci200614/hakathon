"""Local-provider contracts, deterministic dates/merging and failure isolation."""
import asyncio
from tests.legacy_support import setUpModule, tearDownModule
import json
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import AsyncMock,patch
import httpx
from pydantic import ValidationError
from app import ai,config,db,llm,service
from app.extraction import Extraction,normalized_patch,parse_extraction,resolve_date


class ExtractionTests(unittest.TestCase):
    def test_nullable_schema_and_invalid_values(self):
        self.assertIsNone(Extraction().machine_type)
        for obj in [{'area_hectares':-2},{'area_hectares':True},{'area_hectares':float('inf')},
                    {'preferred_date':'2027-02-30'},{'location':[]},{'booking':True}]:
            with self.subTest(obj=obj),self.assertRaises(ValidationError): Extraction(**obj)

    def test_darija_numbers_and_normalization(self):
        data=normalized_patch(Extraction(intent='machinery_request',machine_type='تراكتور',operation='الحرث'),'بغيت تراكتور يحرث ليا جوج هكتارات')
        self.assertEqual(data['machine_type'],'tractor')
        self.assertEqual(data['operation'],'plowing')
        self.assertEqual(data['area_hectares'],2)
        self.assertNotIn('location',data)

    def test_dates_casablanca_reference_and_ambiguity(self):
        today=date(2026,11,3) # Tuesday
        self.assertEqual(resolve_date('الثلاثاء الجاي',today),('2026-11-10',True))
        self.assertEqual(resolve_date('غدا',today),('2026-11-04',True))
        self.assertEqual(resolve_date('بعد غدا',today),('2026-11-05',True))
        self.assertEqual(resolve_date('mardi prochain',today),('2026-11-10',True))
        self.assertEqual(resolve_date('الثلاثاء',today),('2026-11-03',True))
        self.assertEqual(resolve_date('03/04',today),(None,True))
        self.assertEqual(resolve_date('31/02/2027',today),(None,True))
        self.assertEqual(resolve_date('10 فبراير 2027',today),('2027-02-10',True))
        self.assertEqual(resolve_date('10 février 2027',today),('2027-02-10',True))

    def test_date_not_present_in_message_is_not_invented(self):
        data=normalized_patch(Extraction(preferred_date=date(2027,2,10)),'فالرماني')
        self.assertNotIn('preferred_date',data)

    def test_recovery_only_parses_fenced_json_and_never_invents(self):
        data=parse_extraction('```json\n{"location":"الرماني"}\n```')
        self.assertEqual(data.location,'الرماني')
        self.assertIsNone(data.area_hectares)
        with self.assertRaises(ValidationError): parse_extraction('location: somewhere')

    def test_python_merge_preserves_old_fields_and_accepts_correction(self):
        old={'intent':'request_machine','machine_type':'tractor','area_hectares':2,'location':'الرماني'}
        merged=service.merge_fields(old,{'location':None,'area_hectares':3})
        self.assertEqual(merged['location'],'الرماني')
        self.assertEqual(merged['machine_type'],'tractor')
        self.assertEqual(merged['area_hectares'],3)

    def test_ambiguous_new_date_preserves_prior_date_and_requires_clarification(self):
        merged=service.merge_fields({'intent':'request_machine','preferred_date':'2027-02-10'},
            {'preferred_date':None,'preferred_date_text':'03/04'})
        self.assertEqual(merged['preferred_date'], '2027-02-10')
        self.assertIn('preferred_date', service.missing_fields(merged))


class OllamaProviderTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.settings=patch.multiple(config,LLM_PROVIDER='ollama',LLM_FALLBACK_PROVIDER='',
            OLLAMA_BASE_URL='http://127.0.0.1:11434',OLLAMA_MODEL='qwen3:1.7b',
            OLLAMA_TIMEOUT_SECONDS=0.5,OLLAMA_NUM_CTX=1024,OLLAMA_NUM_PREDICT=180,OLLAMA_KEEP_ALIVE='2m')
        self.settings.start()
        self.lock_patch=patch.object(llm,'_inference_lock',asyncio.Lock())
        self.lock_patch.start()

    async def asyncTearDown(self):
        self.lock_patch.stop()
        self.settings.stop()

    def transport(self,handler):
        real=httpx.AsyncClient
        return patch.object(llm.httpx,'AsyncClient',side_effect=lambda **kw:real(transport=httpx.MockTransport(handler),**kw))

    async def test_request_uses_supported_low_memory_options_and_field_patch(self):
        calls=[]
        def respond(req):
            calls.append(json.loads(req.content))
            return httpx.Response(200,json={'done':True,'message':{'content':'{"intent":"machinery_request","machine_type":"tractor","operation":"plowing","area_hectares":2}'}})
        with self.transport(respond):
            result=await ai.extract('بغيت تراكتور يحرث ليا جوج هكتارات',{'location':'private-old-location'},[{'role':'assistant','text':'فين كاينة الأرض؟'}])
        body=calls[0]
        self.assertFalse(body['stream']);self.assertFalse(body['think'])
        self.assertEqual(body['options'],{'num_ctx':1024,'num_predict':180,'temperature':0})
        self.assertEqual(body['keep_alive'],'2m')
        self.assertIsInstance(body['format'],dict)
        self.assertIn('private-old-location',json.dumps(body))
        self.assertEqual(result['area_hectares'],2)
        self.assertEqual(result['intent'],'request_machine')

    async def test_timeout_and_disabled_fallback(self):
        def fail(req): raise httpx.ReadTimeout('private error')
        with self.transport(fail),patch.object(llm.GeminiProvider,'extract',AsyncMock()) as fallback:
            with self.assertRaisesRegex(ai.AIError,'ollama_timeout'): await ai.extract('طلب')
            fallback.assert_not_awaited()

    async def test_ollama_offline(self):
        def fail(req): raise httpx.ConnectError('private URL')
        with self.transport(fail):
            with self.assertRaisesRegex(ai.AIError,'ollama_offline'): await ai.extract('طلب')

    async def test_configured_gemini_fallback(self):
        def fail(req): raise httpx.ConnectError('offline')
        fallback=AsyncMock(return_value=Extraction(location='الرماني'))
        with self.transport(fail),patch.object(config,'LLM_FALLBACK_PROVIDER','gemini'),patch.object(config,'GEMINI_API_KEY','test-only'),patch.object(config,'GEMINI_MODEL','test-model'),patch.object(llm.GeminiProvider,'extract',fallback):
            result=await ai.extract('فالرماني')
        self.assertEqual(result['location'],'الرماني')
        fallback.assert_awaited_once()

    async def test_both_providers_fail_without_repeated_remote_attempts(self):
        with patch.object(llm.OllamaProvider,'extract',AsyncMock(side_effect=ai.AIError('ollama_timeout'))),patch.object(llm.GeminiProvider,'extract',AsyncMock(side_effect=ai.AIError('gemini_unavailable'))) as remote,patch.object(config,'LLM_FALLBACK_PROVIDER','gemini'),patch.object(config,'GEMINI_API_KEY','test-only'),patch.object(config,'GEMINI_MODEL','test-model'):
            with self.assertRaisesRegex(ai.AIError,'gemini_unavailable'):
                await ai.extract('طلب')
            remote.assert_awaited_once()

    async def test_missing_gemini_configuration_prevents_fallback(self):
        def fail(req): raise httpx.ConnectError('offline')
        with self.transport(fail),patch.object(config,'LLM_FALLBACK_PROVIDER','gemini'),patch.object(config,'GEMINI_API_KEY',''),patch.object(llm.GeminiProvider,'extract',AsyncMock()) as fallback:
            with self.assertRaisesRegex(ai.AIError,'ollama_offline'): await ai.extract('طلب')
            fallback.assert_not_awaited()

    async def test_gemini_extraction_has_only_one_attempt(self):
        generate=AsyncMock(return_value=Extraction(location='الرماني'))
        with patch.object(llm,'_generate',generate):
            await llm.GeminiProvider().extract('فالرماني','فين؟')
        self.assertEqual(generate.call_args.kwargs['max_attempts'],1)
        self.assertEqual(generate.call_args.kwargs['total_timeout'],16)

    async def test_truncated_and_invalid_json_safe_failure(self):
        for payload in [{'done':True,'done_reason':'length','message':{'content':'{}'}},
                        {'done':True,'message':{'content':'{"area_hectares":-2}'}}]:
            with self.subTest(payload=payload),self.transport(lambda req:httpx.Response(200,json=payload)):
                with self.assertRaises(ai.AIError): await ai.extract('طلب')

    async def test_inference_is_serialized_across_farmers(self):
        active=0;peak=0
        async def infer(text,question):
            nonlocal active,peak
            active+=1;peak=max(peak,active)
            await asyncio.sleep(0.01)
            active-=1
            return Extraction(machine_type='tractor')
        with patch.object(llm.OllamaProvider,'extract',side_effect=infer):
            await asyncio.gather(ai.extract('طلب واحد'),ai.extract('طلب ثاني'))
        self.assertEqual(peak,1)


class ConversationTests(unittest.IsolatedAsyncioTestCase):
    async def test_multiturn_patch_restart_correction_confirmation_and_duplicates(self):
        with tempfile.TemporaryDirectory() as temp,patch.object(db,'DB_PATH',str(Path(temp)/'test.db')),patch.object(service,'send_text',AsyncMock()) as send:
            db.init_db()
            def msg(id,text): return {'from':'test-farmer','id':id,'type':'text','text':{'body':text}}
            patches=[{'intent':'request_machine','machine_type':'tractor','operation':'plowing','area_hectares':2},
                     {'location':'الرماني'},{'preferred_date':'2027-02-10'},{'area_hectares':3}]
            with patch.object(service,'extract',AsyncMock(side_effect=patches)):
                await service.handle_message(msg('1','بغيت تراكتور يحرث ليا جوج هكتارات'))
                self.assertEqual(send.call_args.args[1],service.QUESTIONS['location'])
                # Reinitializing schema/new SQLite connections preserve the active draft.
                db.init_db()
                await service.handle_message(msg('2','فالرماني'))
                self.assertEqual(send.call_args.args[1],service.QUESTIONS['preferred_date'])
                await service.handle_message(msg('3','2027-02-10'))
                await service.handle_message(msg('4','لا ثلاثة هكتارات'))
                with db.connect() as con: self.assertEqual(con.execute('SELECT count(*) FROM requests').fetchone()[0],0)
                await service.handle_message(msg('5','نعم'))
                before=send.await_count
                await service.handle_message(msg('5','نعم'))
                self.assertEqual(send.await_count,before)
            with db.connect() as con:
                rows=con.execute('SELECT status,payload FROM requests').fetchall()
                self.assertEqual(len(rows),1);self.assertEqual(rows[0]['status'],'pending_review')
                self.assertEqual(json.loads(rows[0]['payload'])['area_hectares'],3)
                self.assertEqual(json.loads(rows[0]['payload'])['location'],'الرماني')

    async def test_failure_preserves_state_and_fallback_reply_deduplicates(self):
        with tempfile.TemporaryDirectory() as temp,patch.object(db,'DB_PATH',str(Path(temp)/'test.db')),patch.object(service,'send_text',AsyncMock()) as send:
            db.init_db()
            with db.connect() as con:
                con.execute('INSERT INTO conversations(farmer_phone,payload) VALUES (?,?)',('test-farmer','{"intent":"request_machine","machine_type":"tractor"}'))
            message={'from':'test-farmer','id':'failed','type':'text','text':{'body':'فالرماني'}}
            with patch.object(service,'extract',AsyncMock(side_effect=ai.AIError('ollama_timeout'))):
                await service.handle_message(message)
                await service.handle_message(message)
            self.assertEqual(send.await_count,1)
            self.assertEqual(service.conversation('test-farmer')[0]['machine_type'],'tractor')

    async def test_real_ollama_http_adapter_through_conversation_and_dashboard(self):
        from app.main import list_requests
        real=httpx.AsyncClient
        calls=[]
        def respond(req):
            calls.append(json.loads(req.content))
            content={'intent':'machinery_request','machine_type':'tractor','operation':'plowing',
                     'area_hectares':2,'location':'الرماني','preferred_date':'2027-02-10'}
            return httpx.Response(200,json={'done':True,'message':{'content':json.dumps(content)}})
        with tempfile.TemporaryDirectory() as temp,patch.object(db,'DB_PATH',str(Path(temp)/'test.db')),patch.object(service,'send_text',AsyncMock()) as send,patch.object(config,'LLM_PROVIDER','ollama'),patch.object(config,'LLM_FALLBACK_PROVIDER',''),patch.object(llm,'_inference_lock',asyncio.Lock()),patch.object(llm.httpx,'AsyncClient',side_effect=lambda **kw:real(transport=httpx.MockTransport(respond),**kw)):
            db.init_db()
            message={'from':'ollama-test-farmer','id':'ollama-first','type':'text','text':{'body':'بغيت تراكتور يحرث جوج هكتارات فالرماني 2027-02-10'}}
            await asyncio.gather(service.handle_message(message),service.handle_message(message))
            self.assertEqual(len(calls),1)
            self.assertEqual(send.await_count,1)
            self.assertEqual(list_requests(),[])
            await service.handle_message({'from':'ollama-test-farmer','id':'ollama-confirm','type':'text','text':{'body':'نعم'}})
            requests=list_requests()
            self.assertEqual(len(requests),1)
            self.assertEqual(requests[0]['status'],'pending_review')
            self.assertEqual(requests[0]['payload']['location'],'الرماني')

    async def test_voice_stt_then_ollama_adapter(self):
        real=httpx.AsyncClient
        def respond(req):
            return httpx.Response(200,json={'done':True,'message':{'content':'{"intent":"machinery_request","machine_type":"tractor"}'}})
        with tempfile.TemporaryDirectory() as temp,patch.object(db,'DB_PATH',str(Path(temp)/'test.db')),patch.object(service,'send_text',AsyncMock()),patch.object(service,'download_media',AsyncMock(return_value=b'OggSvoice')),patch.object(service,'transcribe',AsyncMock(return_value='بغيت تراكتور')) as stt,patch.object(config,'LLM_PROVIDER','ollama'),patch.object(config,'LLM_FALLBACK_PROVIDER',''),patch.object(llm,'_inference_lock',asyncio.Lock()),patch.object(llm.httpx,'AsyncClient',side_effect=lambda **kw:real(transport=httpx.MockTransport(respond),**kw)):
            db.init_db()
            await service.handle_message({'from':'voice-test','id':'voice-local','type':'audio','audio':{'id':'test-media','mime_type':'audio/ogg; codecs=opus'}})
            stt.assert_awaited_once()
            self.assertEqual(service.conversation('voice-test')[0]['machine_type'],'tractor')

    async def test_cancel_is_deterministic_and_never_creates_request(self):
        with tempfile.TemporaryDirectory() as temp,patch.object(db,'DB_PATH',str(Path(temp)/'test.db')),patch.object(service,'send_text',AsyncMock()),patch.object(service,'extract',AsyncMock()) as extract:
            db.init_db()
            with db.connect() as con:
                con.execute('INSERT INTO conversations(farmer_phone,payload) VALUES (?,?)',('test-farmer','{"intent":"request_machine"}'))
            await service.handle_message({'from':'test-farmer','id':'cancel','type':'text','text':{'body':'إلغاء'}})
            extract.assert_not_awaited()
            self.assertIsNone(service.conversation('test-farmer')[0])
            with db.connect() as con:self.assertEqual(con.execute('SELECT count(*) FROM requests').fetchone()[0],0)


if __name__=='__main__':unittest.main()
