import tempfile
from tests.legacy_support import setUpModule, tearDownModule
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch
from app import db, employment, service


class EmploymentTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path_patch = patch.object(db, 'DB_PATH', str(Path(self.temp.name) / 'test.db'))
        self.path_patch.start()
        self.addCleanup(self.path_patch.stop)
        db.init_db()
        self.send = AsyncMock()
        self.send_patch = patch.object(service, 'send_text', self.send)
        self.send_patch.start()
        self.addCleanup(self.send_patch.stop)
        self.counter = 0

    async def incoming(self, phone, text, msg_id=None):
        self.counter += 1
        await service.handle_message({'from': phone, 'id': msg_id or str(self.counter),
                                      'type': 'text', 'text': {'body': text}})
        return self.send.call_args.args[1]

    async def draft(self, phone='owner', kind='worker', location='Meknès'):
        for text in [f'post {kind}', 'Amine', location, 'Harvesting', 'Available Monday, 5 years experience', 'contact@example.com']:
            reply = await self.incoming(phone, text)
        return reply

    async def test_post_search_update_remove_and_isolation(self):
        reply = await self.draft()
        self.assertIn('contact@example.com', reply)
        with db.connect() as con:
            self.assertEqual(con.execute('SELECT count(*) FROM employment_listings').fetchone()[0], 0)
        await self.incoming('owner', 'yes', 'confirmation')
        await self.incoming('owner', 'yes', 'confirmation')
        await self.incoming('reader', 'find workers')
        result = await self.incoming('reader', 'meknes')
        self.assertIn('Amine', result)
        self.assertIn('contact@example.com', result)
        self.assertNotIn('owner', result)
        await self.incoming('reader', 'remove worker')
        with db.connect() as con:
            self.assertEqual(con.execute("SELECT status FROM employment_listings").fetchone()[0], 'active')
        await self.draft(location='Fes')
        await self.incoming('owner', 'yes')
        with db.connect() as con:
            self.assertEqual(con.execute('SELECT count(*) FROM employment_listings').fetchone()[0], 1)
            self.assertEqual(con.execute('SELECT location FROM employment_listings').fetchone()[0], 'Fes')
        await self.incoming('owner', 'remove worker')
        await self.incoming('reader', 'find workers')
        self.assertIn('ما لقينا', await self.incoming('reader', 'Fes'))

    async def test_job_search_literal_location_and_cancel(self):
        await self.draft(kind='job')
        await self.incoming('owner', 'oui')
        await self.incoming('reader', 'find jobs')
        self.assertIn('ما لقينا', await self.incoming('reader', '%'))
        await self.incoming('reader', 'find jobs')
        self.assertIn('Amine', await self.incoming('reader', 'meknes'))
        await self.incoming('reader', 'post worker')
        await self.incoming('reader', 'cancel')
        self.assertIsNone(employment.handle('reader', 'tractor'))

    async def test_confirmation_requires_delivered_summary(self):
        for text in ['post worker', 'Amine', 'Fes', 'Harvesting', 'Available Monday']:
            await self.incoming('owner', text)
        self.send.side_effect = RuntimeError('delivery unavailable')
        with self.assertRaises(RuntimeError):
            await self.incoming('owner', 'contact@example.com', 'failed-summary')
        self.send.side_effect = None
        await self.incoming('owner', 'yes')
        with db.connect() as con:
            self.assertEqual(con.execute('SELECT count(*) FROM employment_listings').fetchone()[0], 0)
        await self.incoming('owner', 'yes')
        with db.connect() as con:
            self.assertEqual(con.execute('SELECT count(*) FROM employment_listings').fetchone()[0], 1)

    async def test_machinery_draft_preserved(self):
        with db.connect() as con:
            con.execute("INSERT INTO conversations(farmer_phone,payload) VALUES (?,?)", ('owner', '{"intent":"request_machine","location":"Fes"}'))
        await self.draft()
        await self.incoming('owner', 'cancel')
        with db.connect() as con:
            self.assertIn('Fes', con.execute('SELECT payload FROM conversations').fetchone()[0])

    async def test_arabizi_hiring_interrupts_machinery_confirmation_without_ai(self):
        with db.connect() as con:
            con.execute('INSERT INTO conversations(farmer_phone,payload,awaiting_confirmation) VALUES (?,?,1)',
                        ('owner', '{"intent":"request_machine","location":"Fes"}'))
        with patch.object(service, 'extract', AsyncMock()) as extract:
            reply = await self.incoming('owner', 'ah bghit wahed dri 25dm 3ndi')
            self.assertIn('كتقلب على عمال', reply)
            extract.assert_not_called()
            with db.connect() as con:
                self.assertEqual(con.execute('SELECT awaiting_confirmation FROM conversations').fetchone()[0], 0)
                self.assertEqual(con.execute('SELECT pending FROM employment_drafts').fetchone()[0], 'search_location')
            result = await self.incoming('owner', 'Meknes')
            self.assertIn('ما لقينا', result)
            extract.assert_not_called()

    async def test_natural_darija_embedded_location(self):
        await self.draft(location='مكناس')
        await self.incoming('owner', 'نعم')
        with patch.object(service, 'extract', AsyncMock()) as extract:
            reply = await self.incoming('reader', 'كنقلب على عمال ديال الجني فمكناس')
            self.assertIn('Amine', reply)
            extract.assert_not_called()
        reply = await self.incoming('reader', 'kan9leb 3la khedma f Fes')
        self.assertIn('ما لقينا', reply)

    def test_intents_distinguish_hiring_job_search_and_posting(self):
        for phrase, expected in [
            ('بغيت ننشر عرض عمل', 'job'), ('بغيت نسجل راسي كعامل', 'worker'),
            ('كنقلب على خدمة ففاس', 'search_job'), ('خاصني جوج عمال', 'search_worker'),
            ('bghit nkhdem', 'search_job'), ('bghit tractor', None),
        ]:
            with self.subTest(phrase=phrase):
                self.assertEqual(employment.natural_intent(employment.normalize(phrase)), expected)


if __name__ == '__main__':
    unittest.main()
