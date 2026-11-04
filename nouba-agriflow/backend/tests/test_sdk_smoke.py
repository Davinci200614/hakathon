"""Exercise actual SDK request serialization over a local mock HTTP transport."""
import json
import unittest
from unittest.mock import patch
import httpx
from app import ai, config

try:
    from google import genai
    from google.genai import types
    SDK_AVAILABLE = True
except ImportError:
    SDK_AVAILABLE = False


@unittest.skipUnless(SDK_AVAILABLE, 'google-genai not installed; install requirements.txt')
class InstalledSDKTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_sdk_serializes_exclusive_minimum_as_json_schema(self):
        requests=[]
        def respond(request):
            requests.append(request)
            return httpx.Response(200,json={'candidates':[{'content':{'role':'model','parts':[{'text':json.dumps({'intent':'request_machine','area_hectares':2,'language':'ary'})}]},'finishReason':'STOP'}]})
        transport=httpx.MockTransport(respond)
        client=genai.Client(api_key='test-only',http_options=types.HttpOptions(
            httpx_async_client=httpx.AsyncClient(transport=transport)))
        with patch.object(genai,'Client',return_value=client),patch.object(config,'GEMINI_API_KEY','test-only'),patch.object(config,'GEMINI_MODEL','same-configured-model'):
            result=(await ai._generate(config.GEMINI_MODEL,'بغيت تراكتور لجوج هكتارات',ai.AgriculturalRequest,'Extract facts.')).model_dump(mode='json')
        client.close()
        self.assertEqual(result['area_hectares'],2)
        self.assertEqual(len(requests),1)
        self.assertIn('same-configured-model',requests[0].url.path)
        generation=json.loads(requests[0].content)['generationConfig']
        self.assertNotIn('responseSchema',generation)
        self.assertIn('exclusiveMinimum',json.dumps(generation['responseJsonSchema']))

    async def test_real_sdk_transcribes_inline_ogg(self):
        requests=[]
        def respond(request):
            requests.append(request)
            return httpx.Response(200,json={'candidates':[{'content':{'parts':[{'text':'{"text":"بغيت تراكتور","intelligible":true}'}]},'finishReason':'STOP'}]})
        client=genai.Client(api_key='test-only',http_options=types.HttpOptions(
            httpx_async_client=httpx.AsyncClient(transport=httpx.MockTransport(respond))))
        with patch.object(genai,'Client',return_value=client),patch.object(config,'GEMINI_API_KEY','test-only'),patch.object(config,'GEMINI_MODEL','test-model'):
            result=await ai.transcribe(b'OggSOpusHead')
        client.close()
        self.assertEqual(result,'بغيت تراكتور')
        self.assertEqual(json.loads(requests[0].content)['contents'][0]['parts'][0]['inlineData']['mimeType'],'audio/ogg')


if __name__=='__main__': unittest.main()
