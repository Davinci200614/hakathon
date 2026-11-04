"""Acceptance tests for v2: real state/transport, mocked language understanding only."""
import asyncio
import json
import tempfile
import subprocess
import sys
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import AsyncMock, patch
import httpx
from app import config, conversation, db, extraction, main, service, understanding
from app.ai import AIError
from app.understanding import Facts, Understanding


class ConversationCoreTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        for target, attr, value in [
            (db, 'DB_PATH', str(Path(self.temp.name) / 'test.db')),
            (config, 'CONVERSATION_CORE', 'v2'),
            (extraction, 'casablanca_today', lambda: date(2026, 11, 4)),
        ]:
            context = patch.object(target, attr, value)
            context.start()
            self.addCleanup(context.stop)
        self.send = AsyncMock()
        self.nlu = AsyncMock(side_effect=AIError('test_unexpected_model_call'))
        for target, attr, value in [(service, 'send_text', self.send), (conversation, 'understand', self.nlu)]:
            context = patch.object(target, attr, value)
            context.start()
            self.addCleanup(context.stop)
        async def interactive_send(phone, payload):
            return await self.send(phone, payload['body']['text'])
        context = patch.object(service, 'send_interactive', interactive_send)
        context.start()
        self.addCleanup(context.stop)
        db.init_db()
        self.counter = 0

    async def turn(self, text, phone='owner', mid=None):
        self.counter += 1
        await service.handle_message({'from': phone, 'id': mid or f'{phone}-{self.counter}', 'type': 'text', 'text': {'body': text}})
        return self.send.call_args.args[1]

    async def pick(self, index, phone='owner', mid=None, choice_id=None):
        with db.connect() as con:
            choices = conversation.context_data(con, phone).get('choices', {})
        selected = choice_id or list(choices)[index - 1]
        self.counter += 1
        await service.handle_message({'from': phone, 'id': mid or f'{phone}-{self.counter}',
            'type': 'interactive', 'interactive': {'type': 'list_reply', 'list_reply': {'id': selected, 'title': 'اختيار'}}})
        return self.send.call_args.args[1]

    def seed_machine(self, location='الرماني', when='2026-11-05'):
        data = {'intent': 'request_machine', 'machine_type': 'tractor', 'operation': 'plowing',
                'area_hectares': 2, 'location': location, 'preferred_date': when}
        with db.connect() as con:
            con.execute('INSERT INTO conversations(farmer_phone,payload) VALUES (?,?)', ('owner', json.dumps(data)))
        return data

    async def test_transcript_corrections_and_past_date(self):
        self.seed_machine(location='جوج', when='2026-11-03')
        reply = await self.turn('المكان دكالة')
        self.assertIn('نهار من اليوم لقدام', reply)
        self.assertEqual(service.conversation('owner')[0]['location'], 'دكالة')
        await self.turn('أنا في دكالة')
        await self.turn('أنا ف بني مسكين مشي جوج')
        self.assertEqual(service.conversation('owner')[0]['location'], 'بني مسكين')
        reply = await self.turn('غدا')
        self.assertIn('2026-11-05', reply)
        await self.turn('نعم')
        with db.connect() as con:
            row = con.execute('SELECT payload FROM requests').fetchone()
            self.assertEqual(json.loads(row['payload'])['location'], 'بني مسكين')
        self.nlu.assert_not_awaited()

    async def test_actual_hiring_variants_switch_without_model(self):
        self.seed_machine()
        for phrase in ['أه ،و بغيت واحد إخدم عندي', 'سلام نوبة بغيت شيواحد إخدم عندي', 'ah bghit wahed dri 25dm 3ndi']:
            reply = await self.turn(phrase)
            self.assertIn('كتقلب على عمال', reply)
            self.assertNotIn('الماكينة:', reply)
            await self.turn('إلغاء')
        with db.connect() as con:
            self.assertEqual(con.execute('SELECT count(*) FROM requests').fetchone()[0], 0)
        self.nlu.assert_not_awaited()

    async def test_fast_machinery_collection(self):
        reply = await self.turn('سلام نوبة بغيت تراكتور يحرث ليا جوج هكتارات')
        self.assertEqual(reply, conversation.QUESTIONS['location'])
        await self.turn('في دكالة')
        reply = await self.turn('غدا')
        self.assertIn('دكالة', reply)
        self.assertIn('2026-11-05', reply)
        self.nlu.assert_not_awaited()

    async def test_delivered_summary_exact_revision_and_deduplication(self):
        self.seed_machine()
        await self.turn('رجع للتراكتور')
        await self.turn('المكان دكالة')
        await self.turn('نعم', mid='confirm')
        await self.turn('نعم', mid='confirm')
        await self.turn('نعم')
        with db.connect() as con:
            rows = con.execute('SELECT payload FROM requests').fetchall()
            self.assertEqual(len(rows), 1)
            self.assertEqual(json.loads(rows[0]['payload'])['location'], 'دكالة')

    async def test_failed_delivery_cannot_arm_confirmation(self):
        self.seed_machine()
        self.send.side_effect = RuntimeError('delivery failed')
        with self.assertRaises(RuntimeError):
            await self.turn('رجع للتراكتور', mid='summary')
        self.send.side_effect = None
        await self.turn('نعم')
        with db.connect() as con:
            self.assertEqual(con.execute('SELECT count(*) FROM requests').fetchone()[0], 0)
        await self.turn('نعم')
        with db.connect() as con:
            self.assertEqual(con.execute('SELECT count(*) FROM requests').fetchone()[0], 1)

    async def test_old_cached_summary_cannot_arm_changed_draft(self):
        self.seed_machine()
        self.send.side_effect = RuntimeError('delivery failed')
        with self.assertRaises(RuntimeError):
            await self.turn('رجع للتراكتور', mid='old-summary')
        self.send.side_effect = None
        await self.turn('المكان دكالة')
        with db.connect() as con:
            con.execute('UPDATE dialogue_confirmations SET armed=0')
        await self.turn('رجع للتراكتور', mid='old-summary')
        with db.connect() as con:
            self.assertEqual(con.execute('SELECT armed FROM dialogue_confirmations').fetchone()[0], 0)

    async def test_unknown_topic_never_repeats_machine_summary(self):
        self.seed_machine()
        self.nlu.side_effect = None
        self.nlu.return_value = Understanding(action='unknown')
        reply = await self.turn('واش تقدر تشرح ليا شي حاجة اخرى؟')
        self.assertNotIn('الماكينة:', reply)
        self.assertIn('قول ليا', reply)
        self.nlu.assert_awaited_once()

    async def test_model_handles_unseen_hiring_and_all_facts_at_once(self):
        self.seed_machine()
        self.nlu.side_effect = None
        self.nlu.return_value = Understanding(task='post_job', action='switch', facts=Facts(
            name='حسن', location='صفرو', work='الجني', details='جوج عمال غدا', contact='contact@example.com'))
        reply = await self.turn('عندي ضيعة وباغي نحط اعلان باش نلقى ناس يعاونوني، حسن فصفرو للجني جوج عمال غدا، contact@example.com')
        self.assertIn('موافق على النشر', reply)
        self.assertIn('صفرو', reply)
        await self.turn('نعم')
        with db.connect() as con:
            self.assertEqual(con.execute('SELECT kind FROM employment_listings').fetchone()[0], 'job')
            self.assertEqual(con.execute('SELECT count(*) FROM requests').fetchone()[0], 0)
        self.nlu.assert_awaited_once()

    async def test_suspended_post_draft_survives_other_search(self):
        await self.turn('نشر عامل')
        await self.turn('حسن')
        await self.turn('بغيت عمال')
        await self.turn('مكناس')
        reply = await self.turn('رجع لبروفايل العامل')
        self.assertIn('مدينة', reply)
        with db.connect() as con:
            data = json.loads(con.execute("SELECT payload FROM dialogue_drafts WHERE task='post_worker'").fetchone()[0])
            self.assertEqual(data['name'], 'حسن')

    async def test_model_cannot_approve_mixed_message(self):
        self.seed_machine()
        await self.turn('رجع للتراكتور')
        self.nlu.side_effect = None
        self.nlu.return_value = Understanding(task='machine', action='confirm')
        await self.turn('yes and something completely different')
        with db.connect() as con:
            self.assertEqual(con.execute('SELECT count(*) FROM requests').fetchone()[0], 0)

    async def test_expired_confirmation_requires_new_summary(self):
        self.seed_machine()
        await self.turn('رجع للتراكتور')
        with db.connect() as con:
            con.execute("UPDATE dialogue_confirmations SET created_at=datetime('now','-1 hour')")
        reply = await self.turn('نعم')
        self.assertIn('واش هاد المعلومات', reply)
        with db.connect() as con:
            self.assertEqual(con.execute('SELECT count(*) FROM requests').fetchone()[0], 0)

    async def test_provider_failure_preserves_state_and_does_not_loop(self):
        self.seed_machine()
        reply = await self.turn('something outside known patterns')
        self.assertIn('المسودة ديالك باقية', reply)
        self.assertEqual(service.conversation('owner')[0]['location'], 'الرماني')
        self.nlu.assert_awaited_once()

    async def test_cancel_only_active_task_and_isolation(self):
        self.seed_machine()
        await self.turn('نشر عامل')
        await self.turn('حسن')
        await self.turn('إلغاء')
        self.assertIsNotNone(service.conversation('owner')[0])
        self.assertIn('قول ليا', await self.turn('نعم', phone='other'))

    async def test_owner_listing_commands_do_not_lock_database(self):
        await self.turn('my listings')
        self.assertIn('تحيد', await self.turn('remove worker'))

    async def test_repeated_invalid_answers_offer_escape(self):
        self.nlu.side_effect = None
        self.nlu.return_value = Understanding(action='unknown')
        replies = [await self.turn('ما فهمتش') for _ in range(3)]
        self.assertIn('ما تقدمناش', replies[-1])

    async def test_rich_message_extracts_location_instead_of_ignoring_it(self):
        self.nlu.side_effect = None
        self.nlu.return_value = Understanding(task='machine', facts=Facts(machine_type='tractor',
            operation='plowing', area_hectares=2, location='مكناس', preferred_date=date(2026, 11, 5)))
        reply = await self.turn('بغيت تراكتور يحرث ليا جوج هكتارات فمكناس غدا')
        self.assertIn('مكناس', reply)
        self.assertIn('واش هاد المعلومات', reply)
        self.nlu.assert_not_awaited()  # catalog + deterministic facts cover this message

    async def test_six_hour_old_context_needs_explicit_resume(self):
        self.seed_machine()
        await self.turn('رجع للتراكتور')
        with db.connect() as con:
            con.execute("UPDATE dialogue_sessions SET updated_at=datetime('now','-7 hours')")
        await self.turn('بغيت تراكتور للحرث')
        data = service.conversation('owner')[0]
        self.assertIsNone(data['location'])
        self.assertIsNone(data['preferred_date'])

    async def test_explicit_new_request_drops_previous_facts(self):
        self.seed_machine()
        await self.turn('طلب جديد')
        self.assertIsNone(service.conversation('owner')[0]['location'])

    async def test_separate_webhook_senders_do_not_block_each_other(self):
        first_started = asyncio.Event()
        second_finished = asyncio.Event()
        async def process(message):
            if message['from'] == 'slow':
                first_started.set()
                await asyncio.wait_for(second_finished.wait(), timeout=0.3)
            else:
                await first_started.wait()
                second_finished.set()
        with patch.object(main, 'handle_message', process):
            await main.process_messages([{'from': 'slow'}, {'from': 'fast'}])
        self.assertTrue(second_finished.is_set())

    async def test_machine_save_rollback_retries_exactly_once(self):
        self.seed_machine()
        await self.turn('رجع للتراكتور')
        with db.connect() as con:
            con.execute("CREATE TRIGGER fail_save BEFORE INSERT ON requests BEGIN SELECT RAISE(ABORT,'test'); END")
        await self.turn('نعم', mid='retry-save')
        with db.connect() as con:
            self.assertEqual(con.execute('SELECT count(*) FROM requests').fetchone()[0], 0)
            self.assertEqual(con.execute('SELECT count(*) FROM conversations').fetchone()[0], 1)
            con.execute('DROP TRIGGER fail_save')
        await self.turn('نعم', mid='retry-save')
        await self.turn('نعم', mid='retry-save')
        with db.connect() as con:
            self.assertEqual(con.execute('SELECT count(*) FROM requests').fetchone()[0], 1)

    async def test_delivered_confirmation_survives_process_restart(self):
        self.seed_machine()
        await self.turn('رجع للتراكتور')
        result = subprocess.run([sys.executable, '-c', '''
import asyncio, sys
from datetime import date
from unittest.mock import AsyncMock
from app import config, db, extraction, service
config.CONVERSATION_CORE = 'v2'
db.DB_PATH = sys.argv[1]
extraction.casablanca_today = lambda: date(2026, 11, 4)
db.init_db()
service.send_text = AsyncMock()
asyncio.run(service.handle_message({'from':'owner','id':'restart-confirm','type':'text','text':{'body':'نعم'}}))
with db.connect() as con:
    assert con.execute('SELECT count(*) FROM requests').fetchone()[0] == 1
''', str(Path(self.temp.name) / 'test.db')], capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)

    async def test_worker_occupation_never_resumes_old_tractor(self):
        self.seed_machine()
        await self.turn('نشر عامل')
        await self.turn('حميد')
        await self.turn('إنزكان')
        reply = await self.turn('حصاد')
        self.assertIn('التجربة', reply)
        self.assertNotIn('الماكينة:', reply)
        with db.connect() as con:
            self.assertEqual(con.execute('SELECT task FROM dialogue_sessions').fetchone()[0], 'post_worker')
            data = json.loads(con.execute("SELECT payload FROM dialogue_drafts WHERE task='post_worker'").fetchone()[0])
            self.assertEqual(data['work'], 'حصاد')
        self.nlu.assert_not_awaited()

    async def test_salary_and_availability_are_profile_details_not_job_search(self):
        self.seed_machine()
        for text in ['نشر عامل', 'حميد', 'إنزكان', 'تريسيان']:
            await self.turn(text)
        details = 'من سيمانة جاية نبظا نخدم ، كنقلب على 4000 dh فشهر ،وأنا لبغيت نخدم'
        reply = await self.turn(details)
        self.assertIn('وسيلة الاتصال', reply)
        await self.turn('contact@example.com')
        reply = await self.turn('نعم')
        self.assertIn('تنشر', reply)
        with db.connect() as con:
            row = con.execute('SELECT * FROM employment_listings').fetchone()
            self.assertEqual(row['kind'], 'worker')
            self.assertEqual(row['details'], details)
            self.assertEqual(row['work'], 'تريسيان')
            self.assertEqual(con.execute('SELECT count(*) FROM requests').fetchone()[0], 0)
        self.nlu.assert_not_awaited()

    async def test_rich_name_answer_stays_in_worker_profile(self):
        await self.turn('بغيت ننشر المعلومات ديالي باش نلقا خدمة')
        reply = await self.turn('سميتي حميد بغيت نلقا خدمة فشي كاريان')
        self.assertIn('مدينة', reply)
        with db.connect() as con:
            data = json.loads(con.execute("SELECT payload FROM dialogue_drafts WHERE task='post_worker'").fetchone()[0])
            self.assertEqual(data['name'], 'حميد')
            self.assertEqual(data['work'], 'كاريان')
            self.assertNotIn('location', data)
        self.nlu.assert_not_awaited()

    async def test_search_to_publication_follows_reference_and_reuses_place(self):
        await self.turn('find jobs')
        reply = await self.turn('في إنزكان')
        self.assertIn('بروفايل', reply)
        reply = await self.turn('واش ممكن نبوستي هاد أنونس و منين شيواحد إسول إلقاني ؟')
        self.assertIn('شنو سميتك', reply)
        await self.turn('حميد')
        reply = await self.turn('حصاد')
        self.assertIn('التجربة', reply)
        with db.connect() as con:
            data = json.loads(con.execute("SELECT payload FROM dialogue_drafts WHERE task='post_worker'").fetchone()[0])
            self.assertEqual(data['location'], 'إنزكان')
        self.nlu.assert_not_awaited()

    async def test_yes_to_search_offer_starts_draft_without_publishing(self):
        await self.turn('find workers')
        await self.turn('ف بولمان')
        reply = await self.turn('نعم')
        self.assertIn('عرض العمل', reply)
        with db.connect() as con:
            data = json.loads(con.execute("SELECT payload FROM dialogue_drafts WHERE task='post_job'").fetchone()[0])
            self.assertEqual(data['location'], 'بولمان')
            self.assertEqual(con.execute('SELECT count(*) FROM employment_listings').fetchone()[0], 0)

    async def test_location_prefix_and_inezgane_spellings_match(self):
        with db.connect() as con:
            con.execute('INSERT INTO employment_listings(owner_phone,kind,name,location,work,details,contact) VALUES (?,?,?,?,?,?,?)',
                ('employer', 'job', 'تعاونية', 'إنزغان', 'حصاد', 'من غدا', 'example@example.com'))
        await self.turn('find jobs')
        reply = await self.turn('في إنزكان')
        self.assertIn('تعاونية', reply)

    async def test_ambiguous_publish_asks_one_targeted_question(self):
        reply = await self.turn('نشر إعلان ديالي')
        self.assertIn('ولا عندك خدمة', reply)
        reply = await self.turn('أنا باغي نخدم')
        self.assertIn('شنو سميتك', reply)
        self.nlu.assert_not_awaited()

    async def test_reset_all_drafts_preserves_published_records_and_filters_history(self):
        self.seed_machine()
        await self.turn('نشر عامل')
        await self.turn('PRIVATE_OLD_NAME')
        with db.connect() as con:
            con.execute("INSERT INTO requests(farmer_phone,payload,status) VALUES ('owner','{}','pending_review')")
            con.execute('INSERT INTO employment_listings(owner_phone,kind,name,location,work,details,contact) VALUES (?,?,?,?,?,?,?)',
                ('owner', 'job', 'employer', 'Fes', 'work', 'details', 'contact'))
            con.execute("INSERT INTO conversations(farmer_phone,payload) VALUES ('other','{}')")
        reply = await self.turn('بغيت نلغي المسودة و نبدا مسودة جديدة')
        self.assertIn('محادثة جديدة', reply)
        with db.connect() as con:
            self.assertEqual(con.execute("SELECT count(*) FROM conversations WHERE farmer_phone='owner'").fetchone()[0], 0)
            self.assertEqual(con.execute("SELECT count(*) FROM dialogue_drafts WHERE phone='owner'").fetchone()[0], 0)
            self.assertEqual(con.execute("SELECT count(*) FROM dialogue_confirmations WHERE phone='owner'").fetchone()[0], 0)
            self.assertEqual(con.execute('SELECT count(*) FROM employment_listings').fetchone()[0], 1)
            self.assertEqual(con.execute('SELECT count(*) FROM requests').fetchone()[0], 1)
            self.assertEqual(con.execute("SELECT count(*) FROM conversations WHERE farmer_phone='other'").fetchone()[0], 1)
        self.nlu.side_effect = None
        self.nlu.return_value = Understanding(action='unknown')
        await self.turn('an unfamiliar new request')
        ctx = self.nlu.call_args.args[1]
        self.assertEqual(ctx['active_task'], 'none')
        self.assertEqual(ctx['draft'], {})
        self.assertNotIn('PRIVATE_OLD_NAME', json.dumps(ctx))
        self.assertIsNone(ctx['last_search'])

    async def test_failed_old_reply_is_not_resent_after_reset(self):
        self.seed_machine()
        self.send.side_effect = RuntimeError('delivery failed')
        with self.assertRaises(RuntimeError):
            await self.turn('رجع للتراكتور', mid='old-failure')
        self.send.side_effect = None
        await self.turn('بدا من جديد')
        calls = self.send.await_count
        await self.turn('رجع للتراكتور', mid='old-failure')
        self.assertEqual(self.send.await_count, calls)
        self.assertIsNone(service.conversation('owner')[0])

    async def test_model_proposed_switch_requires_confirmation_inside_profile(self):
        await self.turn('نشر عامل')
        self.nlu.side_effect = None
        self.nlu.return_value = Understanding(task='search_jobs', action='switch')
        reply = await self.turn('واش نقدر ندير حاجة خرا بلا هاد التسجيل؟')
        self.assertIn('واش بغيتي نوقفوه', reply)
        with db.connect() as con:
            self.assertEqual(con.execute('SELECT task FROM dialogue_sessions').fetchone()[0], 'post_worker')
        reply = await self.turn('لا')
        self.assertIn('شنو سميتك', reply)
        with db.connect() as con:
            self.assertEqual(con.execute('SELECT task FROM dialogue_sessions').fetchone()[0], 'post_worker')

    async def test_unknown_inside_profile_gives_contextual_question(self):
        for text in ['نشر عامل', 'حميد', 'إنزكان']:
            await self.turn(text)
        self.nlu.side_effect = None
        self.nlu.return_value = Understanding(action='unknown')
        reply = await self.turn('واش تقدر توضح ليا؟')
        self.assertIn('بروفايل العامل', reply)
        self.assertIn('المهارات', reply)
        self.assertNotIn('بغيت تراكتور', reply)

    async def test_field_question_can_get_explanation_without_losing_draft(self):
        for text in ['نشر عامل', 'حميد', 'إنزكان', 'تريسيان']:
            await self.turn(text)
        self.nlu.side_effect = None
        self.nlu.return_value = Understanding(task='post_worker', action='help',
            clarification='إلا ما حددتيش الأجرة دابا، تقدر تكتب قابل للتفاوض.')
        reply = await self.turn('واش تقدر تشرح ليا شنو نكتب فهاد الخطوة؟')
        self.assertIn('قابل للتفاوض', reply)
        self.assertIn('التجربة', reply)
        with db.connect() as con:
            row = con.execute("SELECT pending,payload FROM dialogue_drafts WHERE task='post_worker'").fetchone()
            self.assertEqual(row['pending'], 'details')
            self.assertEqual(json.loads(row['payload'])['name'], 'حميد')

    async def test_profile_model_name_fact_cannot_be_rerouted_as_search(self):
        await self.turn('نشر عامل')
        self.nlu.side_effect = None
        self.nlu.return_value = Understanding(task='search_jobs', action='switch', facts=Facts(name='حميد', work='كاريان'))
        reply = await self.turn('واش تسجلني باسم حميد حيث بغيت نخدم فالكاريان؟')
        self.assertIn('مدينة', reply)
        with db.connect() as con:
            self.assertEqual(con.execute('SELECT task FROM dialogue_sessions').fetchone()[0], 'post_worker')

    async def test_confirmed_model_switch_preserves_profile(self):
        await self.turn('نشر عامل')
        self.nlu.side_effect = None
        self.nlu.return_value = Understanding(task='search_jobs', action='switch')
        await self.turn('واش نقدر نبدل شنو كندير ونقلب على شي عروض؟')
        reply = await self.turn('نعم')
        self.assertIn('مدينة', reply)
        with db.connect() as con:
            self.assertEqual(con.execute('SELECT task FROM dialogue_sessions').fetchone()[0], 'search_jobs')
            self.assertEqual(con.execute("SELECT count(*) FROM dialogue_drafts WHERE task='post_worker'").fetchone()[0], 1)

    async def test_declining_public_contact_does_not_get_saved_as_contact(self):
        for text in ['نشر عامل', 'حميد', 'إنزكان', 'حصاد', 'التوفر من غدا']:
            await self.turn(text)
        reply = await self.turn('ما بغيتش ننشر النمرة ديالي')
        self.assertIn('إيميل', reply)
        with db.connect() as con:
            data = json.loads(con.execute("SELECT payload FROM dialogue_drafts WHERE task='post_worker'").fetchone()[0])
            self.assertNotIn('contact', data)

    async def test_profile_work_that_mentions_ads_is_still_an_answer(self):
        for text in ['نشر عامل', 'حميد', 'إنزكان']:
            await self.turn(text)
        reply = await self.turn('تصميم الإعلانات')
        self.assertIn('التجربة', reply)
        with db.connect() as con:
            data = json.loads(con.execute("SELECT payload FROM dialogue_drafts WHERE task='post_worker'").fetchone()[0])
            self.assertEqual(data['work'], 'تصميم الإعلانات')

    async def test_guided_menu_and_native_choices_publish_worker_without_model(self):
        await self.turn('menu')
        await self.pick(1)
        await self.turn('حميد')
        await self.turn('سلا جديدة')
        await self.pick(2)  # electricity
        await self.turn('متوفر من غدا والأجرة فوق 5000 درهم')
        await self.pick(3)  # email/account
        await self.turn('hamid@example.com')
        await self.pick(1)  # confirm the delivered summary
        with db.connect() as con:
            row = con.execute('SELECT * FROM employment_listings').fetchone()
            self.assertEqual(row['name'], 'حميد')
            self.assertEqual(row['location'], 'سلا جديدة')
            self.assertEqual(row['work'], 'الكهرباء')
            self.assertEqual(row['contact'], 'hamid@example.com')
        self.nlu.assert_not_awaited()

    async def test_numbered_menu_can_mix_with_natural_text(self):
        await self.turn('الاختيارات')
        await self.turn('1')
        await self.turn('حميد')
        await self.turn('في سلا جديدة')
        await self.turn('كنقلب نخدم فالكهرباء')
        await self.turn('الأجرة فوق 5000dh')
        await self.turn('hamid@example.com')
        await self.turn('Sf mzyan 3afak nshrha')
        with db.connect() as con:
            self.assertEqual(con.execute('SELECT count(*) FROM employment_listings').fetchone()[0], 1)

    async def test_contact_channel_name_does_not_allow_publication(self):
        for text in ['نشر عامل', 'حميد', 'سلا جديدة', 'كهرباء', 'متوفر غدا']:
            await self.turn(text)
        reply = await self.turn('2ma faecbook wla WhatsApp')
        self.assertIn('نمرة', reply)
        reply = await self.turn('موافق')
        self.assertNotIn('تنشر الإعلان', reply)
        with db.connect() as con:
            row = con.execute("SELECT payload,pending FROM dialogue_drafts WHERE task='post_worker'").fetchone()
            self.assertFalse(json.loads(row['payload']).get('contact'))
            self.assertEqual(row['pending'], 'contact')
            self.assertEqual(con.execute('SELECT count(*) FROM employment_listings').fetchone()[0], 0)

    async def test_contact_denial_clears_prior_address_and_asks_actual_contact(self):
        for text in ['نشر عامل', 'حميد', 'سلا جديدة', 'كهرباء', 'متوفر غدا', 'old@example.com']:
            await self.turn(text)
        reply = await self.turn('باقي معطيتكش وسيلة الاتصال')
        self.assertIn('وسيلة الاتصال', reply)
        with db.connect() as con:
            data = json.loads(con.execute("SELECT payload FROM dialogue_drafts WHERE task='post_worker'").fetchone()[0])
            self.assertFalse(data.get('contact'))
            self.assertEqual(con.execute('SELECT count(*) FROM dialogue_confirmations').fetchone()[0], 0)

    async def test_self_whatsapp_contact_requires_explicit_choice(self):
        phone = '212612345678'
        for text in ['نشر عامل', 'حميد', 'سلا جديدة', 'كهرباء', 'متوفر غدا']:
            await self.turn(text, phone=phone)
        with db.connect() as con:
            data = json.loads(con.execute('SELECT payload FROM dialogue_drafts').fetchone()[0])
            self.assertNotIn('contact', data)
        reply = await self.pick(1, phone=phone)
        self.assertIn('+212612345678', reply)
        await self.pick(1, phone=phone)
        with db.connect() as con:
            self.assertEqual(con.execute('SELECT contact FROM employment_listings').fetchone()[0], '+212612345678')

    async def test_fresh_search_offer_does_not_reuse_old_worker_facts(self):
        for text in ['نشر عامل', 'حميد', 'إنزكان', 'تريسيان', 'تفاصيل قديمة']:
            await self.turn(text)
        await self.turn('find jobs')
        await self.turn('فنواحي سلا جديدة')
        reply = await self.turn('أه عفاك')
        self.assertIn('شنو سميتك', reply)
        with db.connect() as con:
            data = json.loads(con.execute("SELECT payload FROM dialogue_drafts WHERE task='post_worker'").fetchone()[0])
            self.assertEqual(data['location'], 'سلا الجديدة')
            self.assertNotIn('name', data)
            self.assertNotIn('work', data)
            self.assertNotIn('details', data)

    async def test_rich_profile_answer_replaces_conflicting_name_city_work_pay(self):
        for text in ['نشر عامل', 'اسم قديم', 'إنزكان', 'تريسيان']:
            await self.turn(text)
        await self.turn('أنا بعدا سميتي حميد ولد سلا جديدة باغي نخدم كنريسيان و الأجرة فوق 5000dh')
        with db.connect() as con:
            data = json.loads(con.execute("SELECT payload FROM dialogue_drafts WHERE task='post_worker'").fetchone()[0])
            self.assertEqual(data['name'], 'حميد')
            self.assertEqual(data['location'], 'سلا جديدة')
            self.assertEqual(data['work'], 'كنريسيان')
            self.assertIn('5000', data['details'])
        self.nlu.assert_not_awaited()

    async def test_guided_field_edit_and_reconfirmation(self):
        for text in ['نشر عامل', 'حميد', 'إنزكان', 'كهرباء', 'متوفر غدا', 'hamid@example.com']:
            await self.turn(text)
        await self.pick(2)  # edit
        await self.pick(2)  # location
        await self.turn('سلا جديدة')
        await self.pick(1)
        with db.connect() as con:
            self.assertEqual(con.execute('SELECT location FROM employment_listings').fetchone()[0], 'سلا جديدة')

    async def test_old_native_button_cannot_apply_after_new_prompt(self):
        await self.turn('menu')
        with db.connect() as con:
            old = list(conversation.context_data(con, 'owner')['choices'])[0]
        await self.pick(2)
        reply = await self.pick(1, choice_id=old)
        self.assertIn('قديمة', reply)
        with db.connect() as con:
            self.assertEqual(con.execute('SELECT task FROM dialogue_sessions').fetchone()[0], 'post_job')

    async def test_failed_interactive_reply_reuses_exact_payload_on_retry(self):
        self.send.side_effect = RuntimeError('delivery failed')
        with self.assertRaises(RuntimeError):
            await self.turn('menu', mid='menu-retry')
        with db.connect() as con:
            before = con.execute("SELECT payload FROM dialogue_outgoing WHERE message_id='menu-retry'").fetchone()[0]
        self.send.side_effect = None
        await self.turn('menu', mid='menu-retry')
        with db.connect() as con:
            after = con.execute("SELECT payload FROM dialogue_outgoing WHERE message_id='menu-retry'").fetchone()[0]
        self.assertEqual(before, after)

    async def test_guided_machine_numeric_area_is_not_a_menu_choice(self):
        await self.turn('menu')
        await self.pick(5)
        await self.pick(1)
        await self.pick(1)
        await self.turn('20 هكتار')
        await self.turn('سلا جديدة')
        await self.turn('غدا')
        await self.pick(1)
        with db.connect() as con:
            data = json.loads(con.execute('SELECT payload FROM requests').fetchone()[0])
            self.assertEqual(data['area_hectares'], 20)
            self.assertEqual(data['location'], 'سلا جديدة')

    async def test_invalid_moroccan_phone_is_rejected(self):
        for text in ['نشر عامل', 'حميد', 'سلا جديدة', 'كهرباء', 'متوفر غدا']:
            await self.turn(text)
        reply = await self.turn('066666245 هادي نمرتي و hamid@example.com')
        self.assertIn('10 أرقام', reply)
        with db.connect() as con:
            data = json.loads(con.execute('SELECT payload FROM dialogue_drafts').fetchone()[0])
            self.assertFalse(data.get('contact'))

    async def test_availability_date_is_not_treated_as_phone_number(self):
        for text in ['نشر عامل', 'حميد', 'سلا جديدة', 'كهرباء']:
            await self.turn(text)
        await self.turn('متوفر من 2026-11-04')
        with db.connect() as con:
            data = json.loads(con.execute('SELECT payload FROM dialogue_drafts').fetchone()[0])
            self.assertEqual(data['details'], 'متوفر من 2026-11-04')
            self.assertNotIn('contact', data)

    async def test_copied_native_choice_from_other_phone_is_rejected(self):
        await self.turn('menu')
        with db.connect() as con:
            other_choice = list(conversation.context_data(con, 'owner')['choices'])[0]
        await self.turn('menu', phone='other')
        reply = await self.pick(1, phone='other', choice_id=other_choice)
        self.assertIn('قديمة', reply)
        with db.connect() as con:
            self.assertEqual(con.execute('SELECT count(*) FROM dialogue_drafts').fetchone()[0], 0)

    async def test_catalog_search_finds_real_profile_with_attached_place_prefix(self):
        with db.connect() as con:
            con.execute('INSERT INTO employment_listings(owner_phone,kind,name,location,work,details,contact) VALUES (?,?,?,?,?,?,?)',
                        ('published-person','worker','حميد','سلا جديدة','تريسيان','متوفر غدا','hamid@example.com'))
        reply = await self.turn('سلام بغيت نلقى شي تريسيان ف سلا جديدة')
        self.assertIn('حميد', reply)
        self.assertIn('hamid@example.com', reply)
        await self.turn('نقلب على عمال')
        self.assertIn('حميد', await self.turn('فسلا جديدة'))
        self.assertIn('حميد', await self.turn('مكاينش شي تريسيان ؟'))
        self.nlu.assert_not_awaited()

    async def test_skill_filter_does_not_return_unrelated_worker(self):
        from scripts.seed_demo import seed
        seed()
        reply = await self.turn('بغيت نلقى تريسيان فسلا جديدة')
        self.assertIn('تجريبي', reply)
        self.assertNotIn('سباك', reply)
        self.assertNotIn('نجار', reply)
        self.assertNotIn('@example.invalid', reply)

    async def test_described_harvesting_machine_resolves_without_model(self):
        reply = await self.turn('بغيت ماكينة ديال الحصاد')
        self.assertIn('حصادة', reply)
        data = service.conversation('owner')[0]
        self.assertEqual(data['machine_type'], 'harvester')
        self.assertEqual(data['operation'], 'harvesting')
        self.nlu.assert_not_awaited()

    async def test_new_database_machine_alias_requires_no_code_change(self):
        with db.connect() as con:
            con.execute('INSERT INTO catalog_terms(kind,code,label,aliases) VALUES (?,?,?,?)',
                        ('machine','olive_shaker','هزاز الزيتون',json.dumps(['نفضاضة الزيتون'],ensure_ascii=False)))
        await self.turn('بغيت نفضاضة الزيتون')
        self.assertEqual(service.conversation('owner')[0]['machine_type'], 'olive_shaker')
        self.nlu.assert_not_awaited()

    async def test_inventory_name_is_discovered_without_a_catalog_entry(self):
        with db.connect() as con:
            con.execute('INSERT INTO machines(name,machine_type,location,hourly_rate) VALUES (?,?,?,?)',
                        ('آلة زرع الحبوب','seed_drill','سلا الجديدة',150))
        await self.turn('بغيت آلة زرع الحبوب')
        self.assertEqual(service.conversation('owner')[0]['machine_type'],'seed_drill')
        self.nlu.assert_not_awaited()

    async def test_unrecognized_description_can_get_model_clarification(self):
        self.nlu.side_effect=None
        self.nlu.return_value=Understanding(action='unknown',clarification='واش كتقصد آلة كتزرع الحبوب ولا كتجمع المحصول؟')
        reply=await self.turn('عندي وصف جديد للمعدات اللي باغي')
        self.assertIn('كتزرع',reply)
        self.assertNotIn('بغيت تراكتور',reply)
        self.assertTrue(self.nlu.call_args.args[1]['catalog_options'])

    async def test_public_worker_directory_reads_only_published_profiles(self):
        from scripts.seed_demo import seed
        seed()
        with db.connect() as con:
            con.execute("INSERT INTO farmers(phone) VALUES ('PRIVATE_UNPUBLISHED_PHONE')")
        reply = await self.turn('عفاك تقدر تعطيني سجل الناس لك طلبو عمل ؟')
        self.assertIn('عامل', reply)
        self.assertNotIn('PRIVATE_UNPUBLISHED_PHONE', reply)
        self.nlu.assert_not_awaited()

    async def test_own_registration_reference_shows_own_listing(self):
        with db.connect() as con:
            con.execute('INSERT INTO employment_listings(owner_phone,kind,name,location,work,details,contact) VALUES (?,?,?,?,?,?,?)',
                        ('owner','worker','حميد','سلا جديدة','تريسيان','متوفر غدا','hamid@example.com'))
        reply = await self.turn('كنت تسجلت كتريسيان فسلا جديدة')
        self.assertIn('حميد', reply)
        self.nlu.assert_not_awaited()

    async def test_inventory_lookup_uses_seeded_machine_records(self):
        from scripts.seed_demo import seed
        seed()
        reply = await self.turn('واش عندكم ماكينة ديال الحصاد فسلا جديدة')
        self.assertIn('حصادة تجريبية سلا', reply)
        self.assertIn('تجريبي', reply)
        self.assertIsNone(service.conversation('owner')[0])

    async def test_typo_reset_has_no_model_dependency(self):
        self.seed_machine()
        reply = await self.turn('نبظاو من جديد عفاك ،ممكن دير بحالا مهدرتش معاك من لول ؟')
        self.assertIn('محادثة جديدة', reply)
        self.assertIsNone(service.conversation('owner')[0])
        self.nlu.assert_not_awaited()

    async def test_model_error_is_persisted_even_when_reply_is_processed(self):
        self.nlu.side_effect = AIError('conversation_provider_unavailable')
        await self.turn('unfamiliar message without a catalog match',mid='provider-failure')
        with db.connect() as con:
            row = con.execute("SELECT * FROM dialogue_audit WHERE message_id='provider-failure'").fetchone()
            self.assertEqual(row['error_code'],'conversation_provider_unavailable')
            self.assertEqual(row['route'],'model')
            self.assertGreaterEqual(row['duration_ms'],0)
            self.assertEqual(con.execute("SELECT status FROM messages WHERE id='provider-failure'").fetchone()[0],'processed')


def employment_question(field):
    from app.employment import QUESTIONS
    return QUESTIONS[field]


class UnderstandingProviderTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        context=patch.object(understanding,'_cooldowns',{})
        context.start()
        self.addCleanup(context.stop)

    async def test_quota_error_avoids_repeat_provider_calls(self):
        real=httpx.AsyncClient
        calls=[]
        def respond(req):
            calls.append(req)
            return httpx.Response(429,json={'error':{'status':'RESOURCE_EXHAUSTED'}})
        with patch.object(config,'CONVERSATION_PROVIDER','gemini'),patch.object(understanding.httpx,'AsyncClient',side_effect=lambda **kw:real(transport=httpx.MockTransport(respond),**kw)):
            for _ in range(2):
                with self.assertRaises(AIError) as caught:
                    await understanding.understand('test',{})
                self.assertEqual(caught.exception.code,'conversation_rate_limited')
        self.assertEqual(len(calls),1)

    async def test_auto_selects_existing_hosted_configuration(self):
        with patch.multiple(config, CONVERSATION_PROVIDER='auto', GEMINI_API_KEY='test', GEMINI_MODEL='test-model'):
            self.assertEqual(understanding.selected_provider(), 'gemini')

    async def test_single_hosted_call_has_budget_and_unified_schema(self):
        result = Understanding(task='search_workers', facts=Facts(location='Meknes'))
        real = httpx.AsyncClient
        calls = []
        def respond(req):
            calls.append(json.loads(req.content))
            return httpx.Response(200, json={'candidates': [{'content': {'parts': [{'text': result.model_dump_json()}]}, 'finishReason': 'STOP'}]})
        with patch.multiple(config, CONVERSATION_PROVIDER='gemini', CONVERSATION_TIMEOUT_SECONDS=1), patch.object(understanding.httpx, 'AsyncClient', side_effect=lambda **kw: real(transport=httpx.MockTransport(respond), **kw)):
            self.assertEqual(await understanding.understand('test', {}), result)
        self.assertEqual(len(calls), 1)
        self.assertIn('search_workers', json.dumps(calls[0]['generationConfig']['responseJsonSchema']))
        self.assertEqual(calls[0]['generationConfig']['thinkingConfig']['thinkingLevel'], 'MINIMAL')

    async def test_queue_and_provider_wait_are_bounded(self):
        real = httpx.AsyncClient
        async def stalled(req):
            await asyncio.sleep(30)
        with patch.multiple(config, CONVERSATION_PROVIDER='gemini', CONVERSATION_TIMEOUT_SECONDS=0.02), patch.object(understanding.httpx, 'AsyncClient', side_effect=lambda **kw: real(transport=httpx.MockTransport(stalled), **kw)):
            with self.assertRaises(AIError) as caught:
                await understanding.understand('test', {})
            self.assertEqual(caught.exception.code, 'conversation_timeout')

    async def test_local_provider_uses_new_schema_and_validates(self):
        real = httpx.AsyncClient
        requests = []
        def respond(req):
            requests.append(json.loads(req.content))
            return httpx.Response(200, json={'done': True, 'message': {'content': '{"task":"search_workers","facts":{"location":"Meknes"}}'}})
        with patch.multiple(config, CONVERSATION_PROVIDER='ollama'), patch.object(understanding.httpx, 'AsyncClient', side_effect=lambda **kw: real(transport=httpx.MockTransport(respond), **kw)):
            result = await understanding.understand('test', {})
        self.assertEqual(result.task, 'search_workers')
        self.assertIn('search_workers', json.dumps(requests[0]['format']))

    async def test_hosted_http_failure_never_retries(self):
        real = httpx.AsyncClient
        calls = []
        def respond(req):
            calls.append(req)
            return httpx.Response(503, json={'error': {'message': 'test'}})
        with patch.object(config, 'CONVERSATION_PROVIDER', 'gemini'), patch.object(understanding.httpx, 'AsyncClient', side_effect=lambda **kw: real(transport=httpx.MockTransport(respond), **kw)):
            with self.assertRaises(AIError) as caught:
                await understanding.understand('test', {})
        self.assertEqual(caught.exception.code, 'conversation_provider_unavailable')
        self.assertEqual(len(calls), 1)

    async def test_malformed_hosted_output_is_rejected(self):
        real = httpx.AsyncClient
        for payload in [{'candidates': []}, {'candidates': [{'content': {'parts': [{'text': '{invalid'}]}}]}, {'candidates': [{'finishReason': 'MAX_TOKENS'}]}]:
            with self.subTest(payload=payload), patch.object(config, 'CONVERSATION_PROVIDER', 'gemini'), patch.object(understanding.httpx, 'AsyncClient', side_effect=lambda **kw: real(transport=httpx.MockTransport(lambda req: httpx.Response(200, json=payload)), **kw)):
                with self.assertRaises(AIError):
                    await understanding.understand('test', {})


if __name__ == '__main__':
    unittest.main()
