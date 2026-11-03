"""Offline contract tests; no real keys, database records or network calls."""
import asyncio
import importlib.util
import json
import httpx
import sys
import tempfile
import types as pytypes
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from pydantic import ValidationError
from app import ai, config, db, service

FULL = dict(intent='request_machine', machine_type='تراكتور', operation='الحرث',
            area_hectares=2, location='دكالة', preferred_date='2027-02-10', language='ary')


class SchemaTests(unittest.TestCase):
    def test_valid_request_and_missing_values(self):
        data = ai.AgriculturalRequest(intent='request_machine').model_dump(mode='json')
        self.assertIsNone(data['location'])
        self.assertIsNone(data['preferred_date'])
        self.assertEqual(ai.AgriculturalRequest(**FULL).model_dump(mode='json')['preferred_date'], '2027-02-10')

    def test_invalid_json_fields_and_dates(self):
        for change in [{'preferred_date':'2027-02-30'}, {'area_hectares':-1},
                       {'area_hectares':float('nan')}, {'area_hectares':True},
                       {'location':{'invented':'object'}}, {'price':100}, {'intent':'confirmed'}]:
            with self.subTest(change=change), self.assertRaises(ValidationError):
                ai.AgriculturalRequest(**(FULL | change))


class FlowTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_patch = patch.object(db, 'DB_PATH', str(Path(self.tmp.name) / 'test.db'))
        self.db_patch.start()
        db.init_db()
        db.init_db()  # additive schema is restart-safe
        self.send = AsyncMock()
        self.send_patch = patch.object(service, 'send_text', self.send)
        self.send_patch.start()
        self.extract = AsyncMock(return_value=ai.AgriculturalRequest(**FULL).model_dump(mode='json'))
        self.extract_patch = patch.object(service, 'extract', self.extract)
        self.extract_patch.start()

    async def asyncTearDown(self):
        self.extract_patch.stop()
        self.send_patch.stop()
        self.db_patch.stop()
        self.tmp.cleanup()

    def msg(self, id, text='بغيت تراكتور', phone='test-farmer'):
        return {'id':id,'from':phone,'type':'text','text':{'body':text}}

    def count(self):
        with db.connect() as con:
            return con.execute('SELECT count(*) FROM requests').fetchone()[0]

    async def test_text_confirmation_and_duplicate_delivery(self):
        await service.handle_message(self.msg('one'))
        self.assertEqual(self.count(),0)
        self.assertIn('واش هاد المعلومات صحيحة',self.send.call_args.args[1])
        await service.handle_message(self.msg('two','نعم'))
        self.assertEqual(self.count(),1)
        with db.connect() as con:
            self.assertEqual(con.execute('SELECT status FROM requests').fetchone()[0],'pending_review')
            self.assertEqual(con.execute('SELECT count(*) FROM conversations').fetchone()[0],0)
        await service.handle_message(self.msg('two','نعم'))
        await service.handle_message(self.msg('three','نعم'))
        self.assertEqual(self.count(),1)
        self.assertEqual(self.extract.await_count,1)

    async def test_missing_fields_one_question_at_a_time_and_memory(self):
        partial = ai.AgriculturalRequest(intent='request_machine',machine_type='تراكتور').model_dump(mode='json')
        self.extract.return_value=partial
        await service.handle_message(self.msg('one'))
        self.assertEqual(self.send.call_args.args[1],service.QUESTIONS['operation'])
        await service.handle_message(self.msg('two','نعم'))
        self.assertEqual(self.count(),0)
        self.assertEqual(self.send.call_args.args[1],service.QUESTIONS['operation'])
        await service.handle_message(self.msg('three','الحرث'))
        args=self.extract.call_args.args
        self.assertEqual(args[1]['machine_type'],'تراكتور')
        self.assertEqual(args[2][-1]['text'],service.QUESTIONS['operation'])
        # Read back from a new SQLite connection, not an in-memory Python chat.
        self.assertEqual(service.conversation('test-farmer')[0]['machine_type'],'تراكتور')

    async def test_correction_requires_another_confirmation(self):
        await service.handle_message(self.msg('one'))
        self.extract.return_value=ai.AgriculturalRequest(**(FULL|{'area_hectares':3})).model_dump(mode='json')
        await service.handle_message(self.msg('two','لا، ثلاثة هكتارات'))
        self.assertEqual(self.count(),0)
        await service.handle_message(self.msg('three','نعم'))
        with db.connect() as con:
            payload=json.loads(con.execute('SELECT payload FROM requests').fetchone()[0])
        self.assertEqual(payload['area_hectares'],3)

    async def test_voice_download_transcription_and_extraction(self):
        msg={'id':'voice','from':'test-farmer','type':'audio','audio':{'id':'real-media-reference','mime_type':'audio/ogg; codecs=opus'}}
        with patch.object(service,'download_media',AsyncMock(return_value=b'OggSvoice')) as download, patch.object(service,'transcribe',AsyncMock(return_value='بغيت تراكتور للحرث')) as stt:
            await service.handle_message(msg)
        download.assert_awaited_once_with('real-media-reference')
        stt.assert_awaited_once_with(b'OggSvoice','audio/ogg; codecs=opus')
        self.assertEqual(self.extract.call_args.args[0],'بغيت تراكتور للحرث')
        self.assertEqual(self.count(),0)
        with db.connect() as con:
            self.assertEqual(con.execute('SELECT text FROM messages').fetchone()[0],'بغيت تراكتور للحرث')

    async def test_unclear_voice_never_creates_request(self):
        msg={'id':'voice','from':'test-farmer','type':'audio','audio':{'id':'media'}}
        with patch.object(service,'download_media',AsyncMock(return_value=b'OggSvoice')), patch.object(service,'transcribe',AsyncMock(side_effect=ai.AIError('transcription_unclear'))):
            await service.handle_message(msg)
        self.extract.assert_not_awaited()
        self.assertEqual(self.count(),0)
        with db.connect() as con:
            self.assertEqual(con.execute('SELECT status FROM messages').fetchone()[0],'needs_text')

    async def test_rate_limit_is_recoverable(self):
        self.extract.side_effect=ai.AIError('gemini_rate_limited')
        await service.handle_message(self.msg('one'))
        self.assertEqual(self.count(),0)
        with db.connect() as con:
            self.assertEqual(con.execute('SELECT error FROM messages').fetchone()[0],'gemini_rate_limited')
        self.extract.side_effect=None
        await service.handle_message(self.msg('one'))
        self.assertTrue(service.conversation('test-farmer')[2])

    async def test_confirmation_reply_retry_does_not_duplicate_request(self):
        await service.handle_message(self.msg('one'))
        self.send.side_effect=RuntimeError('sensitive provider error')
        with self.assertRaisesRegex(RuntimeError,'^message_processing_failed$'):
            await service.handle_message(self.msg('two','نعم'))
        self.assertEqual(self.count(),1)
        self.send.side_effect=None
        await service.handle_message(self.msg('two','نعم'))
        self.assertEqual(self.count(),1)

    async def test_summary_delivery_failure_requires_successful_summary(self):
        self.send.side_effect=RuntimeError('sensitive')
        with self.assertRaises(RuntimeError):
            await service.handle_message(self.msg('one'))
        self.assertFalse(service.conversation('test-farmer')[2])
        self.send.side_effect=None
        await service.handle_message(self.msg('two','نعم'))
        self.assertEqual(self.count(),0)
        await service.handle_message(self.msg('three','نعم'))
        self.assertEqual(self.count(),1)

    async def test_unrelated_message_and_farmer_isolation(self):
        self.extract.return_value=ai.AgriculturalRequest(intent='other').model_dump(mode='json')
        await service.handle_message(self.msg('one','سلام'))
        self.assertIsNone(service.conversation('test-farmer')[0])
        self.extract.return_value=ai.AgriculturalRequest(**FULL).model_dump(mode='json')
        await service.handle_message(self.msg('two'))
        await service.handle_message(self.msg('three','نعم',phone='other-farmer'))
        self.assertEqual(self.count(),0)

    async def test_legacy_draft_is_preserved_and_reconfirmed(self):
        with db.connect() as con:
            con.execute('INSERT INTO requests(farmer_phone,payload) VALUES (?,?)',('test-farmer',json.dumps(FULL)))
        await service.handle_message(self.msg('one','نعم'))
        with db.connect() as con:
            self.assertEqual(con.execute('SELECT status FROM requests').fetchone()[0],'draft')
        await service.handle_message(self.msg('two','نعم'))
        self.assertEqual(self.count(),1)
        with db.connect() as con:
            self.assertEqual(con.execute('SELECT status FROM requests').fetchone()[0],'pending_review')


class GeminiContractTests(unittest.IsolatedAsyncioTestCase):
    """Mock official SDK boundary, so failure cases run without live credentials."""
    def sdk(self, response=None, error=None):
        class APIError(Exception):
            def __init__(self,code): self.code=code
        if error is not None:
            error=APIError(error)
        generate=AsyncMock(return_value=pytypes.SimpleNamespace(text=response),side_effect=error)
        async_client=AsyncMock()
        async_client.__aenter__.return_value=pytypes.SimpleNamespace(models=pytypes.SimpleNamespace(generate_content=generate))
        factory=pytypes.SimpleNamespace(
            HttpOptions=lambda **kw:kw, HttpRetryOptions=lambda **kw:kw,
            GenerateContentConfig=lambda **kw:kw,
            Part=pytypes.SimpleNamespace(from_bytes=lambda **kw:kw))
        genai=pytypes.ModuleType('google.genai')
        genai.Client=lambda **kw:pytypes.SimpleNamespace(aio=async_client)
        genai.types=factory
        genai.errors=pytypes.SimpleNamespace(APIError=APIError)
        google=pytypes.ModuleType('google');google.genai=genai
        return patch.dict(sys.modules,{'google':google,'google.genai':genai,'google.genai.types':factory}),generate

    async def test_transcription_ogg_bytes_darija_and_model_override(self):
        stub,generate=self.sdk(json.dumps({'text':'بغيت نحرث جوج هكتارات','intelligible':True},ensure_ascii=False))
        with stub, patch.object(config,'GEMINI_API_KEY','test-only'),patch.object(config,'GEMINI_STT_MODEL','test-stt'):
            text=await ai.transcribe(b'OggSOpusHeadfake','audio/ogg; codecs=opus')
        self.assertEqual(text,'بغيت نحرث جوج هكتارات')
        self.assertEqual(generate.call_args.kwargs['model'],'test-stt')
        self.assertEqual(generate.call_args.kwargs['contents'][0],{'data':b'OggSOpusHeadfake','mime_type':'audio/ogg'})

    async def test_structured_json_extraction(self):
        stub,generate=self.sdk(json.dumps(FULL))
        with stub,patch.object(config,'GEMINI_API_KEY','test-only'),patch.object(config,'GEMINI_MODEL','test-model'):
            result=await ai.extract('جوج هكتارات',{'machine_type':'تراكتور'},[{'role':'assistant','text':'شحال من هكتار؟'}])
        self.assertEqual(result['area_hectares'],2)
        sent=json.loads(generate.call_args.kwargs['contents'])
        self.assertEqual(sent['previous']['machine_type'],'تراكتور')
        self.assertEqual(len(sent['history']),1)
        self.assertIs(generate.call_args.kwargs['config']['response_schema'],ai.AgriculturalRequest)

    async def test_api_errors_are_classified_without_payloads(self):
        for status,code in [(429,'gemini_rate_limited'),(404,'gemini_model_or_input_unavailable'),(403,'gemini_auth_error'),(503,'gemini_unavailable')]:
            stub,generate=self.sdk(error=status)
            with self.subTest(status=status),stub,patch.object(config,'GEMINI_API_KEY','test-only'),patch.object(config,'GEMINI_MODEL','test-model'),patch.object(ai.asyncio,'sleep',AsyncMock()):
                with self.assertRaises(ai.AIError) as caught:
                    await ai.extract('text')
                self.assertEqual(str(caught.exception),code)
                self.assertEqual(generate.await_count,3 if status in (429,503) else 1)

    async def test_invalid_empty_or_blocked_responses(self):
        for response in ['not JSON','{"intent":"request_machine","area_hectares":-3}',None]:
            stub,_=self.sdk(response)
            with stub,patch.object(config,'GEMINI_API_KEY','test-only'),patch.object(config,'GEMINI_MODEL','test-model'):
                with self.assertRaises(ai.AIError): await ai.extract('text')

    async def test_silence_and_invalid_audio(self):
        stub,_=self.sdk('{"text":null,"intelligible":false}')
        with stub,patch.object(config,'GEMINI_API_KEY','test-only'),patch.object(config,'GEMINI_MODEL','test-model'):
            with self.assertRaisesRegex(ai.AIError,'transcription_unclear'):
                await ai.transcribe(b'OggSsilence')
        for audio in [b'',b'not an ogg']:
            with self.assertRaises(ai.AIError): await ai.transcribe(audio)

    async def test_network_error_is_sanitized(self):
        stub,generate=self.sdk()
        generate.side_effect=httpx.ConnectError('sensitive request URL')
        with stub,patch.object(config,'GEMINI_API_KEY','test-only'),patch.object(config,'GEMINI_MODEL','test-model'):
            with self.assertRaisesRegex(ai.AIError,'^gemini_network_error$'):
                await ai.extract('text')

    async def test_stt_defaults_to_main_model(self):
        stub,generate=self.sdk('{"text":"سلام","intelligible":true}')
        with stub,patch.object(config,'GEMINI_API_KEY','test-only'),patch.object(config,'GEMINI_MODEL','main-model'),patch.object(config,'GEMINI_STT_MODEL',''):
            self.assertEqual(await ai.transcribe(b'OggSvoice'),'سلام')
        self.assertEqual(generate.call_args.kwargs['model'],'main-model')

    async def test_missing_configuration(self):
        with patch.object(config,'GEMINI_API_KEY',''):
            with self.assertRaisesRegex(ai.AIError,'gemini_not_configured'): await ai.extract('text')


if __name__=='__main__':
    unittest.main()
