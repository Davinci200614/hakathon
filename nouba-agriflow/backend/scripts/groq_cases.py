"""Synthetic, predeclared acceptance cases; no real database records or contacts."""

WORKER = {'kind':'worker','name':'أيوب','location':'إنزكان','work':'driver','details':'متوفر صباحا','contact':'old@example.invalid'}
MACHINE = {'machine_type':'tractor','operation':'plowing','area_hectares':2,'location':'دكالة','preferred_date':'2026-11-05'}


def case(name,text,task,*,active='none',pending=None,draft=None,last_search=None,rules=None,actions=None):
    return {'id':name,'message':text,'expected_tasks':task if isinstance(task,list) else [task],
            'expected_actions':actions,'rules':rules or {},
            'context':{'today':'2026-11-04','timezone':'Africa/Casablanca','active_task':active,
                       'pending_field':pending,'draft':draft or {},'recent_turns':[], 'last_search':last_search}}


CASES = [
    case('direct_electrician','سلام بغيت نلقى شي تريسيان ف سلا جديدة','search_workers',rules={'work':'electrician','location':'sale_al_jadida'}),
    case('indirect_electrician','الضو فالدار كيطفي بوحدو ومحتاج شي حد يفهم فهاد الشي يجي يشوفو، أنا فسلا الجديدة','search_workers',rules={'work':'electrician','location':'sale_al_jadida'}),
    case('arabizi_hiring','ah bghit wahed dri 25dm 3ndi','search_workers',active='machine',pending='confirm',draft=MACHINE,rules={'not_action':'confirm','empty':['machine_type','area_hectares','preferred_date']}),
    case('job_not_hiring','أنا باغي نلقى خدمة كتريسيان فسلا جديدة','search_jobs',rules={'work':'electrician','location':'sale_al_jadida'}),
    case('harvester_description','بغيت ماكينة ديال الحصاد','machine',rules={'machine_type':'harvester'}),
    case('unseen_baler_description','باغي آلة كتجمع التبن وتضغطو وتربطو فبالات باش ننقلو','machine',rules={'machine_type':'baler'}),
    case('unknown_machine','بغيت زغفلون 9000، ما عارفش سميتو الصحيحة',['none','machine'],actions=['unknown','clarify','help'],rules={'clarification':True,'not_known_machine':True}),
    case('location_correction','أنا ف بني مسكين مشي دكالة','machine',active='machine',pending='correction',draft=MACHINE,rules={'contains':{'location':'بني مسكين'},'empty':['machine_type','operation','area_hectares','preferred_date']}),
    case('relative_date','لاربع ديال سيمانة جاية مع 12','machine',active='machine',pending='preferred_date',draft={k:v for k,v in MACHINE.items() if k!='preferred_date'},rules={'date':'2026-11-11'}),
    case('mixed_yes_topic_switch','أه ،و بغيت واحد إخدم عندي','search_workers',active='machine',pending='confirm',draft=MACHINE,rules={'not_action':'confirm','empty':['machine_type','area_hectares']}),
    case('profile_skill_answer','حصاد','post_worker',active='post_worker',pending='work',draft={'kind':'worker','name':'أيوب','location':'مكناس'},rules={'work':'harvesting'}),
    case('profile_salary_answer','من سيمانة جاية نبدا نخدم، كنقلب على 4000 dh فشهر، وأنا اللي بغيت نخدم','post_worker',active='post_worker',pending='details',draft={'kind':'worker','name':'أيوب','location':'مكناس','work':'electrician'},rules={'contains':{'details':'4000'},'empty':['name','location','contact']}),
    case('rich_name_answer','سميتي أيوب بغيت نلقى خدمة فشي كاريان','post_worker',active='post_worker',pending='name',draft={'kind':'worker'},rules={'contains':{'name':'أيوب'},'work':'quarry','empty':['location','contact']}),
    case('contact_denial','باقي معطيتكش وسيلة الاتصال','post_worker',active='post_worker',pending='confirm',draft=WORKER,rules={'clear':'contact','empty':['contact']}),
    case('contact_channel_only','واتساب ولا فيسبوك، باقي معطيتكش الكونت ولا النمرة','post_worker',active='post_worker',pending='contact',draft={k:v for k,v in WORKER.items() if k!='contact'},rules={'empty':['contact']}),
    case('multi_field_correction','أنا سميتي أيوب ولد سلا جديدة باغي نخدم كتريسيان والأجرة فوق 5000dh','post_worker',active='post_worker',pending='details',draft=WORKER,rules={'contains':{'name':'أيوب','details':'5000'},'work':'electrician','location':'sale_al_jadida','empty':['contact']}),
    case('publish_reference_after_search','واش ممكن نبوستي هاد أنونس ومنين شيواحد يسول يلقاني؟','post_worker',last_search={'kind':'job','location':'سلا الجديدة'},rules={'not_action':'confirm'}),
    case('reset_with_typo','نبظاو من جديد عفاك، ممكن دير بحالا مهدرتش معاك من لول؟',['none','post_worker'],active='post_worker',pending='details',draft=WORKER,actions=['reset']),
    case('arabizi_publication_confirmation','Sf mzyan 3afak nshrha','post_worker',active='post_worker',pending='confirm',draft=WORKER,actions=['confirm']),
    case('question_about_salary','واش ضروري نحدد الأجرة دابا ولا ممكن نخليها للتفاوض؟',['post_worker','none'],active='post_worker',pending='details',draft=WORKER,actions=['help','clarify','unknown'],rules={'clarification':True,'empty':['contact']}),
]
