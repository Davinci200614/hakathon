"""Inspect persistence without sending any messages; optional exact sender filter."""
import argparse
import json
from app import db


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--phone', help='Exact WhatsApp sender, with country code')
    parser.add_argument('--limit', type=int, default=10)
    args = parser.parse_args()
    db.init_db()
    print('Database:', db.database_path())
    with db.connect() as con:
        where, values = ('WHERE m.farmer_phone=?', [args.phone]) if args.phone else ('', [])
        rows = con.execute(f'''SELECT m.id,m.created_at,m.kind,m.status,m.text,a.route,a.error_code,
            t.text AS reply FROM messages m LEFT JOIN dialogue_audit a ON a.message_id=m.id
            LEFT JOIN conversation_turns t ON t.message_id=m.id AND t.role='assistant'
            {where} ORDER BY m.rowid DESC LIMIT ?''', [*values,max(1,min(args.limit,100))]).fetchall()
        for row in reversed(rows):
            print(json.dumps(dict(row),ensure_ascii=False))
        for table in ('messages','requests','employment_listings','machines','conversations','dialogue_drafts'):
            print(table, con.execute(f'SELECT count(*) FROM {table}').fetchone()[0])


if __name__ == '__main__':
    main()
