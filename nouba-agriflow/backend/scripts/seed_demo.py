"""Idempotent fictional demo data; never overwrite a real person's records."""
from datetime import datetime, timezone
import sqlite3
from app import db

PEOPLE = [
    ('worker','electrician-sale-1','ياسين التجريبي','سلا الجديدة','كهربائي','تجريبي: كهرباء المنازل، 5 سنين تجربة، متوفر أيام الأسبوع، الأجرة بالتفاوض'),
    ('worker','electrician-sale-2','أمين التجريبي','سلا جديدة','تريسيان','تجريبي: تركيب وإصلاح الكهرباء، متوفر صباحا، خدمة بالنهار'),
    ('worker','plumber-sale','سعيد التجريبي','سلا الجديدة','سباك','تجريبي: إصلاح الماء والحمامات، 7 سنين تجربة'),
    ('worker','driver-sale','رشيد التجريبي','سلا الجديدة','سائق','تجريبي: سياقة ونقل معدات فلاحية، متوفر من الأسبوع القادم'),
    ('worker','carpenter-sale','كريم التجريبي','سلا الجديدة','نجار','تجريبي: النجارة وإصلاح الأبواب، الأجرة بالتفاوض'),
    ('worker','harvest-meknes','مريم التجريبية','مكناس','الحصاد','تجريبي: الجني والحصاد، تجربة 4 سنين، متوفرة موسميا'),
    ('worker','harvest-boulemane','إدريس التجريبي','بولمان','حصاد','تجريبي: الحصاد وخدمة الأرض، متوفر صباحا'),
    ('worker','electrician-inezgane','فؤاد التجريبي','إنزكان','كهربائي','تجريبي: كهرباء المباني والصيانة'),
    ('job','electrician-job-sale','مشغل تجريبي سلا','سلا الجديدة','كهربائي','تجريبي: محتاج كهربائي عندو تجربة، 5000 درهم للشهر، البداية بالتفاوض'),
    ('job','plumber-job-sale','ورشة تجريبية','سلا الجديدة','سباك','تجريبي: خدمة إصلاح قنوات الماء، الأجرة اليومية بالتفاوض'),
    ('job','driver-job-sale','تعاونية تجريبية سلا','سلا الجديدة','سائق','تجريبي: محتاج سائق نقل معدات، رخصة مناسبة وتجربة مطلوبة'),
    ('job','harvest-job-meknes','ضيعة تجريبية مكناس','مكناس','الحصاد','تجريبي: محتاجين 4 عمال للجني، شغل موسمي'),
    ('job','harvest-job-boulemane','ضيعة تجريبية بولمان','بولمان','الحصاد','تجريبي: محتاجين جوج عمال، الأجرة بالنهار'),
    ('job','quarry-job-sale','مقلع تجريبي سلا','سلا الجديدة','الكاريان','تجريبي: خدمة فالكاريان، التجربة مطلوبة، الأجرة بالتفاوض'),
]
MACHINES = [
    ('تراكتور تجريبي سلا','tractor','سلا الجديدة',200),
    ('حصادة تجريبية سلا','harvester','سلا الجديدة',450),
    ('مكبس تبن تجريبي','baler','مكناس',250),
    ('رشاشة تجريبية','sprayer','سلا الجديدة',150),
    ('حصادة تجريبية بولمان','harvester','بولمان',400),
    ('مضخة ماء تجريبية','irrigation_pump','دكالة',100),
]


def seed():
    db.init_db()
    with db.connect() as con:
        for kind, slug, name, city, work, details in PEOPLE:
            con.execute('''INSERT INTO employment_listings(owner_phone,kind,name,location,work,details,contact,is_demo)
                VALUES (?,?,?,?,?,?,?,1) ON CONFLICT(owner_phone,kind) DO NOTHING''',
                ('demo:'+slug,kind,name,city,work,details,slug+'@example.invalid'))
        for name, kind, city, rate in MACHINES:
            con.execute('''INSERT INTO machines(name,machine_type,location,hourly_rate,available,is_demo)
                SELECT ?,?,?,?,1,1 WHERE NOT EXISTS(SELECT 1 FROM machines WHERE name=? AND is_demo=1)''',
                (name,kind,city,rate,name))
        return {'demo_workers':con.execute("SELECT count(*) FROM employment_listings WHERE kind='worker' AND is_demo=1").fetchone()[0],
                'demo_jobs':con.execute("SELECT count(*) FROM employment_listings WHERE kind='job' AND is_demo=1").fetchone()[0],
                'demo_machines':con.execute('SELECT count(*) FROM machines WHERE is_demo=1').fetchone()[0]}


if __name__ == '__main__':
    path = db.database_path()
    if path.exists():
        backup = path.with_name(path.stem+'.before-demo-'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%f')+'.db')
        with sqlite3.connect(str(path)) as source, sqlite3.connect(str(backup)) as target:
            source.backup(target)
        print('Backup:', backup)
    print(seed())
