"""Standalone API criteria audit -> joint/contact repair -> physical verification."""
import argparse,copy,json,time
from pathlib import Path
from .agent import save
from .quality_suite import api_decide,PROPERTIES,validate_decision,verify_origin,API_CURSOR
from .direct_augmentation import require_prerequisites,validate_binding
from .contact_control import validate_contact_control
from .contact_evidence import inspect_contact_case,acceptance_sources
from .exploration import explore_decision
from .suite_provider import configured_provider
from .retry_state import RESUME,budgets,phase_deadline,validate_resume,offer
from .suite_evidence import install
from .suite_lock import exclusive
from .suite_physics import physical_case,check_frozen,validate_guard
from .suite_inspect import inspect_case
from ..workflow.contracts import now,file_hash

SKILL=Path(__file__).parent/'skills/contact_review/SKILL.md'

def obj(props):return {'type':'object','properties':props,'required':list(props),'additionalProperties':False}
def arr(items):return {'type':'array','items':items}
NUM={'type':'number'};STR={'type':'string'}
AUDIT={'assessment':{'type':'string','enum':['native_valid','propose_contact_revision','needs_tool']},'rationale':STR,'contact_bodies':arr(STR),'min_contact_force_N':NUM}
def bounded(lo,hi):return {'type':'number','minimum':lo,'maximum':hi}
JOINT=obj({'name':STR,'replay_blend':bounded(0,1),'offset_rad':bounded(-.35,.35),'kp_scale':bounded(.5,1.5),'kd_scale':bounded(.5,2),'force_gain_rad_per_N_s':bounded(-.02,.02)})
CONTROL=obj({'contact_bodies':arr(STR),'force_range_N':arr(bounded(0,1000))|{'minItems':2,'maxItems':2,'description':'Ordered [lower, upper], lower < upper.'},'ramp_s':bounded(.1,3),'joints':arr(JOINT),'max_offset_rad':bounded(.001,.35)|{'description':'Positive capability limit, also required with an empty joints list; it applies no correction by itself.'},'max_rate_rad_s':bounded(.001,.5),'force_stop_N':bounded(0,1500)|{'description':'Must be at least force_range_N[1].'}})
PROPS=copy.deepcopy(PROPERTIES)|{'contact_control':CONTROL,'reference_case':STR,'criteria_decision':STR,'resume':RESUME}

PROPS['action']={'type':'string','enum':['run','needs_tool','reject']}

def tools(props):return [{'type':'function','name':'decide','description':'Persist an evidenced criteria audit or explicit contact control decision.','strict':True,'parameters':obj(props)}]

def validate_audit(d,bodies):
    if set(d)!=set(AUDIT) or d['assessment'] not in AUDIT['assessment']['enum'] or not d['rationale'].strip():raise ValueError('Invalid audit')
    if not d['contact_bodies'] or not set(d['contact_bodies'])<=set(bodies):raise ValueError('Unknown contact body in audit')
    import math
    f=d['min_contact_force_N']
    if type(f) not in (int,float) or not math.isfinite(f) or not 0<=f<=1000:raise ValueError('Invalid reviewed force threshold')
    return d

def quality(r,audit):
    common=r.get('coverage')==1 and not r.get('failure')
    native=r.get('success') and r.get('independent_success')
    shadow=audit['assessment']=='propose_contact_revision' and r.get('reviewed_success') and r.get('reviewed_independent_success')
    return bool(common and (native or shadow))

def planning_evidence(value):
    if isinstance(value,dict):return {k:planning_evidence(v) for k,v in value.items() if k not in ('inspection','acceptance_source','source_hashes')}
    if isinstance(value,list):return [planning_evidence(v) for v in value]
    return value


def reusable_audit(path,spec,evidence):
    record=verify_origin(path);request=json.loads(Path(record['origin']['request']).read_text());content=request['input'][0]['content']
    if isinstance(content,list):content=next(x['text'] for x in content if x['type']=='input_text')
    context=json.loads(content)
    if context['task']!=spec['task'] or record['decision']['assessment']!='native_valid':raise ValueError('Cannot reuse audit across task or unresolved criteria')
    old=context['evidence']
    if old['acceptance_source_sha256']!=evidence['acceptance_source_sha256'] or old['replay']['source_hashes']!=evidence['replay']['source_hashes']:raise ValueError('Audit source hashes changed')
    return record['decision'],Path(path)


def run(config,provider_config):
    install();root=Path(config['output_root']);root.mkdir(parents=True,exist_ok=True)
    with exclusive(root,'study'):
        if (root/'summary.json').exists():
            from .contact_delivery import deliver
            deliver(root)
            return json.loads((root/'summary.json').read_text())
        spec=config['task'];tr=root/spec['task'];tr.mkdir(exist_ok=True)
        provider=configured_provider(provider_config,Path(config.get('api_routing_path',str(root/'.api_rotation.json'))));started=now();tick=time.monotonic()
        limit=config.get('max_physical_attempts',3)
        if type(limit) is not int or not 1<=limit<=3:raise ValueError('Physical attempt budget must be 1..3')
        initial_budget,retry_budget,total_budget=budgets(config,limit)
        deadline=tick+total_budget
        from .contact_batch import BudgetProvider
        provider=BudgetProvider(provider,phase_deadline(tick,deadline,initial_budget))
        ann=verify_origin(Path(spec['annotation']));params=ann['decision'];replay=json.loads((Path(spec['baseline'])/'result.json').read_text());require_prerequisites(replay,ann);validate_binding(spec,replay,ann)
        spec=spec|{'contact_start':params['contact_start']}
        source=acceptance_sources(spec['baseline'])
        evidence={'replay':inspect_contact_case(spec['baseline'],params['contact_start'],tr/'evidence/replay',spec.get('baseline_inspection')),
                  'acceptance_source':source['code'],'acceptance_source_sha256':source['hashes']}
        save(tr/'contact_evidence.json',evidence)
        bodies=evidence['replay']['robot_bodies'];joints=[j['name'] for j in evidence['replay']['joints']]
        cases={'replay':spec['baseline']};query_budget={'remaining':config.get('max_exploration_decisions',24)}
        if type(query_budget['remaining']) is not int or query_budget['remaining']<1:raise ValueError('Invalid exploration budget')
        def ask(folder,context,props,validator):
            return explore_decision(provider,folder,context,[params['episode']],decide=api_decide,decision_tools=tools(props),validator=validator,skill_path=SKILL,cases=cases,budget=query_budget)
        ar=tr/'criteria_audit';ar.mkdir(exist_ok=True);API_CURSOR[str(ar)]=0
        audit,ap=ask(ar,{'stage':'criteria_audit','evidence':evidence,'task':spec['task'],'native_only':True,
            'instructions':'Audit native checker against replay and annotation. Native acceptance is fixed. Query evidence yourself when needed; no historical repair or supervisor strategy is supplied.','annotation':{k:params[k] for k in ('episode','contact_start','contact_end')}},AUDIT,lambda d,ep:validate_audit(d,bodies))
        history=[];status='needs_tool' if audit['assessment']=='needs_tool' or audit['assessment']!='native_valid' else 'running'
        previous=None;offers=[];previous_review=None
        for number in range(limit if status=='running' else 0):
            if number:provider.deadline=phase_deadline(time.monotonic(),deadline,retry_budget)
            attempt=tr/f'attempt_{number:02d}';attempt.mkdir(exist_ok=True);API_CURSOR[str(attempt)]=0
            context={'stage':'contact_plan' if number==0 else 'contact_retry','task':spec['task'],'criteria_audit':audit,'evidence':planning_evidence(evidence),
                'fixed':{'reference_case':spec['baseline'],'criteria_decision':str(ap),'episode':params['episode'],'contact_start':params['contact_start'],'safety_guard':validate_guard(params.get('safety_guard')),'object_shift_xy_m':config['object_shift_xy_m']},
                'objective':'fresh_augmentation_validation','historical_success_is_current_success':False,
                'source_frames':replay['source_frames'],'initial_parameters':{k:params[k] for k in ('episode','contact_start','contact_end')},'previous_decision':previous,'previous_review':previous_review,
                'attempt_history':[{'case':h['result']['experiment_path'],'success':h['result']['success'],'failure':h['result'].get('failure'),'review':h['review']} for h in history],
                'rollback_checkpoints':offers,'remaining_physical_attempts':limit-number,
                'phase_wall_budget_s':retry_budget if number else initial_budget,'task_wall_budget_s':total_budget,
                'instructions':'Validate a NEW physical augmentation at the fixed object shift. Historical success is a reference, not completion of this test. Use only replay, annotation and observations from your own experiments to choose a falsifiable candidate; no previous repair is supplied. Do not reject merely because the reference or diagnostic already succeeds. For an actual failed candidate, choose an evidenced repair. Use exact supplied reference_case and criteria_decision paths. needs_tool/reject require a concrete capability/safety/evidence blocker. Keep source and safety fixed. Contact end may extend within source + tail.'}
            def validate(d,episodes):
                if set(d)!=set(PROPS):raise ValueError('Invalid plan fields')
                validate_decision({k:d[k] for k in PROPERTIES},episodes)
                validate_contact_control(d['contact_control'],bodies,joints)
                validate_resume(d['resume'],offers)
                if d['resume']['kind']=='checkpoint' and d['resume']['frame']>=replay['source_frames']+round(d['tail_s']/.02):raise ValueError('Rollback point beyond new trajectory end')
                if d['action'] not in ('run','needs_tool','reject'):raise ValueError('Plan action must be run/needs_tool/reject')
                if d['contact_start']!=params['contact_start'] or validate_guard(d['safety_guard'])!=validate_guard(params.get('safety_guard')) or d['reference_case']!=spec['baseline'] or d['criteria_decision']!=str(ap):raise ValueError('Fixed source/annotation/audit changed')
                if not d['contact_start']<=d['contact_end']<replay['source_frames']+round(d['tail_s']/.02):raise ValueError('Contact end beyond extended reference')
                if d['action']=='run' and previous and all(d[k]==previous[k] for k in ('contact_control','tail_s','wrist_delta_m','contact_end')):raise ValueError('Retry must change executable controls')
                return d
            plan,dp=ask(attempt,context,PROPS,validate)
            if plan['action']!='run':status=plan['action'];break
            if time.monotonic()>provider.deadline-45:raise TimeoutError('Insufficient task budget for new physics and evidence review')
            cached=attempt/'physics.result.json'
            if cached.exists():r=json.loads(cached.read_text())
            else:
                if (attempt/'physics.started.json').exists():raise RuntimeError('Unresolved started attempt; no implicit repeat')
                save(attempt/'physics.started.json',{'started_at':now(),'decision':str(dp),'sha256':file_hash(dp)})
                r=physical_case(spec['dataset'],plan['episode'],tr,'token_prefix_mode1',config['object_shift_xy_m'],decision_path=dp,contact_control=True);save(cached,r)
            case=Path(r['experiment_path']);check_frozen(case)
            if not (case/'inspection/inspection.json').exists():inspect_case(case,case/'inspection')
            current=inspect_contact_case(case,plan['contact_start'],attempt/'review_images');current['reviewed_success']=r.get('reviewed_success');current['reviewed_independent_success']=r.get('reviewed_independent_success')
            reviewroot=attempt/'review';reviewroot.mkdir(exist_ok=True);API_CURSOR[str(reviewroot)]=0
            review_props={'action':{'type':'string','enum':['accept','retry','reject','needs_tool']},'rationale':STR}
            def vr(d,ep):
                if set(d)!=set(review_props) or d['action'] not in review_props['action']['enum'] or not d['rationale'].strip():raise ValueError('Invalid review')
                if d['action']=='accept' and not quality(r,audit):raise ValueError('No acceptable native/reviewed physical success')
                if d['action']=='retry' and number==limit-1:raise ValueError('Retry budget exhausted')
                return d
            cases[f'attempt_{number:02d}']=str(case)
            checkpoint=offer(case)
            if checkpoint:offers.append(checkpoint)
            review,rp=ask(reviewroot,{'stage':'contact_review','criteria_audit':audit,'evidence':current,'executed_parameters':plan,'remaining_physical_attempts':limit-1-number,'rollback_checkpoints':offers,'attempt_history':[{'case':h['result']['experiment_path'],'review':h['review']} for h in history],'instructions':'Accept only evidenced physical quality; distinguish native and revised criteria. Retry requires a specific evidence-based hypothesis.'},review_props,vr)
            item={'result':r,'review':review,'review_path':str(rp),'accepted':quality(r,audit) and review['action']=='accept','video':str(case/'inspection/review_zh.mp4')}
            history.append(item);save(root/'progress.json',history);save(attempt/'result.json',item)
            previous=plan;previous_review=review
            evidence={'replay':evidence['replay'],'latest':current,'joint_reference_difference':evidence.get('joint_reference_difference'), 'instructions':'Use new measured evidence for next repair. No manual supervisor tuning.'}
            status=review['action']
            if status!='retry':break
        calls=[json.loads(p.read_text()) for p in root.glob('**/api/*/attempt_*.json')]
        summary={'experiment_id':config.get('study_id',root.name),'started_at':started,'ended_at':now(),'total_wall_s':time.monotonic()-tick,'status':status,'initial_reference_policy':'replay_and_annotation_only','exploration_decisions_remaining':query_budget['remaining'],'time_budgets_s':{'initial':initial_budget,'per_retry':retry_budget,'total':total_budget},'criteria_audit':audit,'criteria_decision':str(ap),'attempts':history,'physical_attempts':len(history),'native_successes':sum(h['result']['success'] for h in history),'reviewed_successes':sum(bool(h['result'].get('reviewed_success',False)) for h in history),'accepted':sum(h['accepted'] for h in history),'api_requests':len(calls),'known_tokens':sum((c.get('usage') or {}).get('total_tokens',0) for c in calls),'unknown_usage_calls':sum(c.get('usage') is None for c in calls),'api_wall_s':sum(c.get('wall_s') or 0 for c in calls),'simulation_wall_s':sum(h['result']['simulation_wall_s'] for h in history),'rendering_wall_s':sum(json.loads((Path(h['result']['experiment_path'])/'inspection/inspection.json').read_text())['rendering_and_inspection_wall_s'] for h in history),'training_ready':False,'token_alone_replays_execution':False}
        save(root/'summary.json',summary)
        lines=[f"# {config.get('study_id',root.name)}",f"{started} — {summary['ended_at']}",'','API 审查验收标准；API 选择关节/接触控制；本地执行、原生及修订接触判据分别验收。','原 token 前缀保留；mode1 后叠加显式关节参考、PD 与接触力反馈，属于轨迹重定向。Token 单独不能复现。','',audit['rationale'],'','|尝试|原生|接触复核|API|视频|','|---|---|---|---|---|']
        for i,h in enumerate(history):lines.append(f"|{i+1}|{h['result']['success']}|{h['result'].get('reviewed_success')}|{h['review']['action']}|[MP4]({h['video']})|")
        lines+=['',json.dumps({k:v for k,v in summary.items() if k not in ('attempts','criteria_audit')},ensure_ascii=False,indent=2)]
        (root/'REPORT.md').write_text('\n'.join(lines))
        from .contact_delivery import deliver
        deliver(root)
        return summary

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--config',required=True);p.add_argument('--provider',required=True);a=p.parse_args();run(json.loads(Path(a.config).read_text()),json.loads(Path(a.provider).read_text()))
