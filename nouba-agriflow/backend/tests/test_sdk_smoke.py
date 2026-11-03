"""Run against the installed SDK without making network requests."""
import importlib.util
import unittest
from unittest.mock import AsyncMock, patch
from app import ai, config

try:
    from google import genai
    from google.genai import errors, types
    SDK_AVAILABLE = True
except ImportError:
    SDK_AVAILABLE = False


@unittest.skipUnless(SDK_AVAILABLE, 'google-genai not installed; install requirements.txt')
class InstalledSDKTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_types_schema_audio_and_async_client(self):
        client = genai.Client(api_key='test-only',http_options=types.HttpOptions(
            timeout=60000,retry_options=types.HttpRetryOptions(attempts=1)))
        cfg=types.GenerateContentConfig(response_mime_type='application/json',response_schema=ai.AgriculturalRequest)
        self.assertIsNotNone(cfg.response_schema)
        part=types.Part.from_bytes(data=b'OggSOpusHead',mime_type='audio/ogg')
        self.assertEqual(part.inline_data.mime_type,'audio/ogg')
        response=types.GenerateContentResponse(candidates=[types.Candidate(content=types.Content(
            parts=[types.Part.from_text(text='{"text":"بغيت تراكتور","intelligible":true}')]))])
        with patch.object(genai,'Client',return_value=client),patch.object(client.aio.models,'generate_content',AsyncMock(return_value=response)),patch.object(config,'GEMINI_API_KEY','test-only'),patch.object(config,'GEMINI_MODEL','test-model'):
            result=await ai.transcribe(b'OggSOpusHead')
        self.assertEqual(result,'بغيت تراكتور')
        client.close()


if __name__=='__main__': unittest.main()
