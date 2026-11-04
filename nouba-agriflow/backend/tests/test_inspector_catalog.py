import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from starlette.requests import Request
from fastapi import HTTPException
from app import db,main,catalog
from scripts.seed_demo import seed


class InspectorCatalogTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        context=patch.object(db,'DB_PATH',str(Path(self.temp.name)/'test.db'))
        context.start()
        self.addCleanup(context.stop)
        db.init_db()

    def request(self,host='localhost',client='127.0.0.1',headers=None):
        return Request({'type':'http','method':'GET','path':'/api/debug/snapshot','query_string':b'',
            'server':(host,8000),'scheme':'http','client':(client,12345),
            'headers':[(b'host',host.encode()),*[(k.encode(),v.encode()) for k,v in (headers or {}).items()]]})

    def test_seed_is_idempotent_and_preserves_real_records(self):
        with db.connect() as con:
            con.execute('INSERT INTO employment_listings(owner_phone,kind,name,location,work,details,contact) VALUES (?,?,?,?,?,?,?)',
                ('real','worker','Real person','سلا جديدة','تريسيان','real details','real@example.com'))
        self.assertEqual(seed(),seed())
        with db.connect() as con:
            self.assertEqual(con.execute('SELECT count(*) FROM employment_listings WHERE is_demo=0').fetchone()[0],1)
            self.assertEqual(con.execute('SELECT count(*) FROM employment_listings WHERE is_demo=1').fetchone()[0],14)
            self.assertEqual(con.execute('SELECT count(*) FROM machines WHERE is_demo=1').fetchone()[0],6)
            self.assertEqual(con.execute('SELECT count(*) FROM messages').fetchone()[0],0)

    def test_local_snapshot_reports_real_file_and_stored_input(self):
        with db.connect() as con:
            con.execute("INSERT INTO messages(id,farmer_phone,kind,text,status) VALUES ('test','owner','text','menu','processed')")
            con.execute("INSERT INTO dialogue_audit(message_id,task,route,error_code,duration_ms) VALUES ('test','none','model','conversation_timeout',6000)")
        result=main.database_snapshot(self.request())
        self.assertEqual(result['counts']['messages'],1)
        self.assertEqual(result['messages'][0]['text'],'menu')
        self.assertEqual(result['messages'][0]['error_code'],'conversation_timeout')
        self.assertTrue(result['database_path'].endswith('test.db'))

    def test_public_inspector_access_is_denied(self):
        for req in [self.request('public.ngrok-free.dev'),self.request(client='8.8.8.8'),self.request(headers={'x-forwarded-for':'8.8.8.8'}),self.request(headers={'x-forwarded-host':'public.ngrok-free.dev'})]:
            with self.subTest(req=req),self.assertRaises(HTTPException) as caught:
                main.database_snapshot(req)
            self.assertEqual(caught.exception.status_code,403)

    def test_alias_updates_are_resolved_from_database(self):
        with db.connect() as con:
            con.execute('INSERT INTO catalog_terms(kind,code,label,aliases) VALUES (?,?,?,?)',('skill','welder','لحام',json.dumps(['سودور'])))
            entries=catalog.terms(con)
        self.assertEqual(catalog.key('سودور','skill',entries),'welder')
        self.assertEqual(catalog.key('كتريسيان','skill',entries),'electrician')
        self.assertEqual(catalog.key('فسلا جديدة','location',entries),'sale_al_jadida')


if __name__=='__main__': unittest.main()
