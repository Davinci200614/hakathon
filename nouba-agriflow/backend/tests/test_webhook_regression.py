"""Exercise unchanged Meta handlers without sockets, threads or real secrets."""
import hashlib
import hmac
import json
import unittest
from unittest.mock import AsyncMock, patch
from fastapi import BackgroundTasks, HTTPException
from starlette.requests import Request
from app import config, main


def request(body=b'',signature=None,query=b''):
    async def receive():
        return {'type':'http.request','body':body,'more_body':False}
    headers=[] if signature is None else [(b'x-hub-signature-256',signature.encode())]
    return Request({'type':'http','method':'POST','path':'/webhook/whatsapp',
                    'headers':headers,'query_string':query},receive)


class WebhookRegressionTests(unittest.IsolatedAsyncioTestCase):
    async def test_signed_voice_webhook_still_routes_media_reference(self):
        message={'id':'test-message','from':'test-farmer','type':'audio',
                 'audio':{'id':'test-media','mime_type':'audio/ogg; codecs=opus'}}
        body=json.dumps({'entry':[{'changes':[{'value':{'messages':[message]}}]}]}).encode()
        signature='sha256='+hmac.new(b'test-secret',body,hashlib.sha256).hexdigest()
        with patch.object(config,'APP_SECRET','test-secret'),patch.object(main,'handle_message',AsyncMock()) as handler:
            tasks=BackgroundTasks()
            result=await main.receive_webhook(request(body,signature),tasks)
            self.assertEqual(result,{'received':True})
            await tasks()
            handler.assert_awaited_once_with(message)
            with self.assertRaises(HTTPException) as caught:
                await main.receive_webhook(request(body,'sha256=invalid'),BackgroundTasks())
            self.assertEqual(caught.exception.status_code,403)

    async def test_meta_verification_challenge_unchanged(self):
        with patch.object(config,'VERIFY_TOKEN','test-verify-token'):
            response=main.verify_webhook(request(query=b'hub.mode=subscribe&hub.verify_token=test-verify-token&hub.challenge=challenge'))
            self.assertEqual(response.body,b'challenge')
        self.assertTrue(any(r.path=='/webhook/whatsapp' and 'GET' in r.methods for r in main.app.routes))
        self.assertTrue(any(r.path=='/webhook/whatsapp' and 'POST' in r.methods for r in main.app.routes))


if __name__=='__main__': unittest.main()
