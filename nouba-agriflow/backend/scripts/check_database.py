"""Non-destructive schema and rolled-back read/write check. No customer output."""
import json
import sqlite3
from app.db import diagnose, log_database_error


def main():
    try:
        result=diagnose()
    except (sqlite3.Error, OSError) as exc:
        log_database_error(exc, 'database.diagnostic_failed')
        print(json.dumps({'check':'failed','exception_type':type(exc).__name__}))
        return 1
    print(json.dumps(result, indent=2))
    return 0 if not result['schema_problems'] and result['quick_check']=='ok' else 1


if __name__=='__main__':
    raise SystemExit(main())
