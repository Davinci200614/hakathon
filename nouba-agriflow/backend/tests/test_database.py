"""SQLite migrations, transactions and error replies, all on isolated databases."""
import asyncio
import json
from tests.legacy_support import setUpModule, tearDownModule
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import AsyncMock, patch
from app import config, db, main, service

LEGACY = '''CREATE TABLE conversations (
 farmer_phone TEXT PRIMARY KEY, payload TEXT NOT NULL DEFAULT '{}',
 awaiting_confirmation INTEGER NOT NULL DEFAULT 0,
 updated_at TEXT DEFAULT CURRENT_TIMESTAMP);'''


class DatabaseTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.path=Path(self.temp.name)/'nested'/'test.db'
        self.path_patch=patch.object(db,'DB_PATH',str(self.path))
        self.path_patch.start()

    def tearDown(self):
        self.path_patch.stop()
        self.temp.cleanup()

    def test_initialization_and_diagnostic_share_absolute_path(self):
        db.init_db()
        db.init_db()
        result=db.diagnose()
        self.assertEqual(result['database_path'],str(self.path.resolve()))
        self.assertEqual(result['schema_problems'],[])
        self.assertEqual(result['quick_check'],'ok')
        with db.connect() as con:
            self.assertEqual(con.execute("SELECT count(*) FROM sqlite_master WHERE name LIKE 'nouba_probe_%'").fetchone()[0],0)
            self.assertEqual(con.execute('SELECT count(*) FROM farmers').fetchone()[0],0)
            self.assertEqual(con.execute('PRAGMA user_version').fetchone()[0],db.SCHEMA_VERSION)

    def test_relative_configuration_is_backend_relative_independent_of_cwd(self):
        from tempfile import TemporaryDirectory
        before=config.resolve_db_path('nouba.db')
        previous=os.getcwd()
        try:
            with TemporaryDirectory() as other:
                os.chdir(other)
                after=config.resolve_db_path('nouba.db')
                self.assertEqual(before,after)
                self.assertEqual(after,str(config.BACKEND_DIR/'nouba.db'))
        finally:
            os.chdir(previous)

    def test_exact_legacy_error_then_additive_idempotent_migration(self):
        self.path.parent.mkdir(parents=True)
        con=sqlite3.connect(self.path)
        con.executescript(LEGACY)
        payload='{"intent":"request_machine","machine_type":"tractor"}'
        con.execute('INSERT INTO conversations(farmer_phone,payload,awaiting_confirmation,updated_at) VALUES (?,?,1,?)',('synthetic',payload,'2026-01-01'))
        con.commit()
        with self.assertRaisesRegex(sqlite3.OperationalError,'table conversations has no column named history'):
            con.execute('INSERT INTO conversations(farmer_phone,payload,history) VALUES (?,?,?)',('other','{}','[]'))
        con.close()
        db.init_db()
        db.init_db()
        with db.connect() as con:
            row=con.execute('SELECT * FROM conversations').fetchone()
            self.assertEqual(row['payload'],payload)
            self.assertEqual(row['awaiting_confirmation'],1)
            self.assertEqual(row['updated_at'],'2026-01-01')
            self.assertEqual(row['history'],'[]')
            self.assertEqual(len(con.execute('PRAGMA table_info(conversations)').fetchall()),8)
            self.assertEqual(row['failed_attempts'],0)
            self.assertIsNone(row['last_question_field'])

    def test_migration_preserves_existing_booking_and_request_rows(self):
        db.init_db()
        with db.connect() as con:
            con.execute("INSERT INTO requests(farmer_phone,payload,status) VALUES ('synthetic','{}','pending_review')")
            con.execute("INSERT INTO machines(name,machine_type) VALUES ('test-machine','tractor')")
            con.execute("INSERT INTO bookings(request_id,machine_id) VALUES (1,1)")
            con.execute('ALTER TABLE conversations DROP COLUMN history')
            con.execute('PRAGMA user_version=0')
        db.init_db()
        with db.connect() as con:
            self.assertEqual(con.execute('SELECT status FROM requests').fetchone()[0],'pending_review')
            self.assertEqual(con.execute('SELECT request_id,machine_id FROM bookings').fetchone()[:],(1,1))

    def test_transaction_rolls_back_all_writes(self):
        db.init_db()
        with self.assertRaises(sqlite3.OperationalError):
            with db.connect() as con:
                con.execute("INSERT INTO farmers(phone) VALUES ('synthetic')")
                con.execute('INSERT INTO absent_test_table VALUES (1)')
        with db.connect() as con:
            self.assertEqual(con.execute('SELECT count(*) FROM farmers').fetchone()[0],0)

    def test_migration_rolls_back_if_another_schema_is_incompatible(self):
        self.path.parent.mkdir(parents=True)
        con=sqlite3.connect(self.path)
        con.executescript(LEGACY+'CREATE TABLE requests (id INTEGER PRIMARY KEY);')
        con.close()
        with self.assertRaisesRegex(sqlite3.OperationalError,'incompatible database schema'):
            db.init_db()
        con=sqlite3.connect(self.path)
        self.assertNotIn('history',{r[1] for r in con.execute('PRAGMA table_info(conversations)')})
        self.assertEqual(con.execute('PRAGMA user_version').fetchone()[0],0)
        con.close()

    def test_lock_timeout_is_bounded_and_does_not_commit(self):
        db.init_db()
        lock=sqlite3.connect(self.path)
        lock.execute('BEGIN IMMEDIATE')
        try:
            with patch.object(db,'BUSY_TIMEOUT_MS',20), self.assertRaisesRegex(sqlite3.OperationalError,'locked'):
                with db.connect() as con:
                    con.execute("INSERT INTO farmers(phone) VALUES ('synthetic')")
        finally:
            lock.rollback();lock.close()
        with db.connect() as con:
            self.assertEqual(con.execute('SELECT count(*) FROM farmers').fetchone()[0],0)

    def test_logs_have_safe_stack_and_engine_error_without_private_values(self):
        db.init_db()
        with self.assertLogs('nouba.diagnostics',level='INFO') as output:
            try:
                with db.connect() as con:
                    con.execute('INSERT INTO missing_table VALUES (?)',('private-content',))
            except sqlite3.OperationalError:
                pass
        text=' '.join(output.output)
        self.assertNotIn('private-content',text)
        record=json.loads(output.records[-1].message)
        self.assertEqual(record['exception_type'],'OperationalError')
        self.assertEqual(record['sqlite_error_name'],'SQLITE_ERROR')
        self.assertTrue(record['stack_frames'])

    def test_full_conversation_survives_distinct_python_process_restarts(self):
        # Every turn uses a new process, imports the app anew and starts the
        # FastAPI lifespan; all continuity comes from the same SQLite file.
        script='''
import asyncio,json,sys
from unittest.mock import patch,AsyncMock
from app import db,main,service
async def run():
 async with main.lifespan(main.app):
  patch_data=json.loads(sys.argv[1]);text=sys.argv[2];msgid=sys.argv[3]
  send=AsyncMock()
  with patch.object(service,'extract',AsyncMock(return_value=patch_data)),patch.object(service,'send_text',send):
   await service.handle_message({'from':'restart-test','id':msgid,'type':'text','text':{'body':text}})
  state=service.conversation('restart-test')
  reply=send.call_args.args[1] if send.call_args else ''
  with db.connect() as con: count=con.execute('SELECT count(*) FROM requests').fetchone()[0]
  print(json.dumps({'question':next((k for k,v in service.QUESTIONS.items() if v==reply),None),'awaiting':state[2],'requests':count,'sent':send.await_count}))
asyncio.run(run())
'''
        turns=[({'intent':'request_machine','machine_type':'tractor','operation':'plowing','area_hectares':2},'بغيت تراكتور يحرث ليا جوج هكتارات','1','location'),
               ({'location':'الرماني'},'فالرماني','2','preferred_date'),
               ({'preferred_date':'2026-11-10'},'الثلاثاء الجاي','3',None),
               ({},'نعم','4',None),({},'نعم','4',None)]
        env=dict(os.environ,DB_PATH=str(self.path))
        for i,(facts,text,msgid,question) in enumerate(turns):
            result=subprocess.run([sys.executable,'-c',script,json.dumps(facts),text,msgid],cwd=config.BACKEND_DIR,env=env,text=True,capture_output=True,timeout=20)
            self.assertEqual(result.returncode,0,result.stderr)
            state=json.loads(result.stdout.strip().splitlines()[-1])
            self.assertEqual(state['question'],question)
            self.assertEqual(state['requests'],0 if i<3 else 1)
            self.assertEqual(state['sent'],0 if i==4 else 1)
            if i==2:self.assertTrue(state['awaiting'])
        with db.connect() as con:
            row=con.execute('SELECT payload,status FROM requests').fetchone()
            self.assertEqual(row['status'],'pending_review')
            self.assertEqual(json.loads(row['payload'])['location'],'الرماني')


class DatabaseFailureReplyTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.patch=patch.object(db,'DB_PATH',str(Path(self.temp.name)/'test.db'))
        self.patch.start();db.init_db()

    async def asyncTearDown(self):
        self.patch.stop();self.temp.cleanup()

    def message(self,id='failing',text='بغيت تراكتور'):
        return {'id':id,'from':'db-test','type':'text','text':{'body':text}}

    async def test_original_operational_error_sends_fallback_then_migration_allows_retry(self):
        with db.connect() as con:
            con.execute('ALTER TABLE conversations DROP COLUMN history')
        with patch.object(service,'extract',AsyncMock(return_value={'intent':'request_machine','machine_type':'tractor'})),patch.object(service,'send_text',AsyncMock()) as send,self.assertLogs('nouba.diagnostics',level='INFO') as logs:
            await service.handle_message(self.message())
            self.assertIn('مشكل تقني',send.call_args.args[1])
            records=[json.loads(r.message) for r in logs.records]
            self.assertTrue(any(r.get('sqlite_message')=='table conversations has no column named history' for r in records))
            db.init_db()
            await service.handle_message(self.message())
        self.assertEqual(service.conversation('db-test')[0]['machine_type'],'tractor')

    async def test_persistence_error_rolls_back_replies_and_same_message_can_retry(self):
        with db.connect() as con:
            con.execute("CREATE TRIGGER test_failure BEFORE INSERT ON conversations BEGIN SELECT RAISE(ABORT,'synthetic persistence failure'); END")
        # A real SQLite failure after extraction; no replacement of SQL methods.
        with patch.object(service,'extract',AsyncMock(return_value={'intent':'request_machine','machine_type':'tractor'})),patch.object(service,'send_text',AsyncMock()) as send:
            await service.handle_message(self.message())
            self.assertIn('مشكل تقني',send.call_args.args[1])
            with db.connect() as con:
                self.assertEqual(con.execute('SELECT count(*) FROM conversations').fetchone()[0],0)
                self.assertEqual(con.execute('SELECT count(*) FROM requests').fetchone()[0],0)
                self.assertEqual(con.execute('SELECT status FROM messages').fetchone()[0],'error')
                con.execute('DROP TRIGGER test_failure')
            await service.handle_message(self.message())
            self.assertEqual(service.conversation('db-test')[0]['machine_type'],'tractor')

    async def test_operational_error_before_receipt_still_sends_fallback(self):
        @contextmanager
        def unavailable():
            raise sqlite3.OperationalError('database is locked')
            yield
        with patch.object(service,'connect',unavailable),patch.object(service,'send_text',AsyncMock()) as send:
            await service.handle_message(self.message())
        send.assert_awaited_once()
        self.assertIn('مشكل تقني',send.call_args.args[1])

    async def test_confirmation_transaction_rolls_back_and_retries_without_duplicates(self):
        full={'intent':'request_machine','machine_type':'tractor','operation':'plowing',
              'area_hectares':2,'location':'الرماني','preferred_date':'2027-02-10'}
        with db.connect() as con:
            con.execute('INSERT INTO conversations(farmer_phone,payload,awaiting_confirmation) VALUES (?,?,1)',('db-test',json.dumps(full)))
            con.execute("CREATE TRIGGER fail_reply BEFORE INSERT ON conversation_turns BEGIN SELECT RAISE(ABORT,'synthetic reply failure'); END")
        with patch.object(service,'send_text',AsyncMock()) as send:
            await service.handle_message(self.message('confirm','نعم'))
            self.assertIn('مشكل تقني',send.call_args.args[1])
            with db.connect() as con:
                self.assertEqual(con.execute('SELECT count(*) FROM requests').fetchone()[0],0)
                self.assertEqual(con.execute('SELECT count(*) FROM conversations').fetchone()[0],1)
                con.execute('DROP TRIGGER fail_reply')
            await service.handle_message(self.message('confirm','نعم'))
            sent=send.await_count
            await service.handle_message(self.message('confirm','نعم'))
            self.assertEqual(send.await_count,sent)
        with db.connect() as con:
            self.assertEqual(con.execute('SELECT count(*) FROM requests').fetchone()[0],1)

    async def test_database_failure_after_accepted_reply_does_not_send_false_failure(self):
        # First send succeeds but then the delivery status write fails.
        original=service.connect
        def connect_after_send():
            if send.await_count:
                @contextmanager
                def fail():
                    raise sqlite3.OperationalError('database is locked')
                    yield
                return fail()
            return original()
        send=AsyncMock()
        with patch.object(service,'send_text',send),patch.object(service,'connect',side_effect=connect_after_send):
            await service.deliver('db-test','message','already accepted reply')
        send.assert_awaited_once_with('db-test','already accepted reply')


if __name__=='__main__': unittest.main()
