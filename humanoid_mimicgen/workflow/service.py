"""Independent public workflow orchestration with content-bound preflight."""
import copy
import json
from pathlib import Path
import shutil
import subprocess
import sys
import time
import numpy as np
from .contracts import WorkflowError, canonical, digest, file_hash, now, validate_strategy
from .store import Store
from .profile import validate_policy
from .planning import candidate_plan, rank_candidates, validate_source, nominal_stance
from .decisions import ResponsesDecision, replay_decision

REPO=Path(__file__).resolve().parents[2]


def input_context(files):
    result={}
    for name,path in files.items():
        p=Path(path)
        if not p.is_file():raise WorkflowError('invalid_input','Missing evidence '+name)
        result[name]=file_hash(p)
    return result


def write_new(path,value):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    with path.open('xb') as f:f.write(canonical(value))


def _strategy(request):
    s=copy.deepcopy(request['strategy']);s['context']=input_context(request['files'])
    validate_strategy(s);validate_policy(s['policy'])
    return s


def _geometry(request,s,proposal=None):
    import mujoco
    from ..privileged import no_physics
    from ..stance_selection import yaw_of
    files=request['files'];targets=dict(np.load(files['targets']));initial=np.load(files['initial'])['qpos']
    source=np.load(files['reference_states'])['samples'];alignment=json.loads(Path(files['alignment']).read_text())
    validate_source(targets,s,alignment);entry=s['phases'][0]['start']+s['command_pose_offset']
    with no_physics():
        model=mujoco.MjModel.from_binary_path(files['model']);data=mujoco.MjData(model)
        if len(initial)!=model.nq or source.ndim!=2 or len(source)<s['source_frame_count']:
            raise WorkflowError('invalid_input','State dimensions')
        data.qpos[:]=initial;mujoco.mj_forward(model,data)
        objid=model.body(s['bindings']['object']).id
        obj=data.xpos[objid].copy();target_object=np.r_[obj,data.xquat[objid]]
        data.qpos[:]=source[entry,1:1+model.nq];mujoco.mj_forward(model,data)
        source_object=np.r_[data.xpos[objid],data.xquat[objid]]
        nominal=nominal_stance(source[entry,1:8],source_object,target_object)
        candidates=[{'id':'current','xyyaw':[*initial[:2],yaw_of(initial[3:7])]},
                    {'id':'object_relative_demonstration','xyyaw':nominal.tolist()}]
        if proposal:candidates+=proposal['stances']
        rows=[]
        for candidate in candidates:
            rows.append(candidate_plan(model,initial,targets,entry,s['bindings'],s['policy'],candidate))
        selected=rank_candidates(rows)
        return {'selected':selected,'candidates':rows,'physics_steps':0,'dynamic_execution_verified':False,
                'evidence':{'scene':{'object_xyz':obj.tolist(),'initial_root':initial[:7].tolist()},
                            'failed_geometry':rows,'source':{'entry_frame':entry,'hands':s['bindings']['hands'],'nominal_stance_xyyaw':nominal.tolist()}}}


def plan(request):
    """Save a new context-bound strategy; this function never advances physics."""
    from ..cli import create_experiment
    s=_strategy(request)
    work=create_experiment(request['experiment_root'],'workflow_plan')
    write_new(work/'request.json',request);start=time.monotonic();status='failed'
    try:
        geometry=_geometry(request,s)
        decision=request['decision'];proposal=None;mode=decision['mode']
        if mode=='saved':
            proposal=replay_decision(decision['path'],geometry['evidence'],provider_identity=decision['identity'])
            write_new(work/'saved_decision.json',{'identity':decision['identity'],'proposal':proposal,'source_sha256':file_hash(decision['path'])})
        elif mode=='responses' and (geometry['selected'] is None or decision.get('request_alternatives',False)):
            provider=ResponsesDecision(decision['model'],decision['base_url'],decision['key_env'],decision['timeout_s'])
            proposal=provider.propose(geometry['evidence'],work/'decision')
        elif mode not in ('deterministic','responses'):raise WorkflowError('invalid_input','Decision mode')
        if proposal:
            if proposal['action']!='propose_stances':raise WorkflowError('needs_observation' if proposal['action']=='needs_observation' else 'unsupported_capability',proposal['reason'])
            geometry=_geometry(request,s,proposal)
        write_new(work/'geometry.json',geometry)
        if geometry['selected'] is None:raise WorkflowError('unreachable','No checked stance; request a model proposal or new evidence')
        s['proposal']={'stance':geometry['selected'],'decision':proposal,'method':'geometry_checked_candidates'}
        artifacts={f'inputs/{name}{Path(path).suffix}':path for name,path in request['files'].items()}
        artifacts.update({f'planning/{p.relative_to(work).as_posix()}':p for p in work.rglob('*') if p.is_file() and p.name!='request.json'})
        # The request contains deployment paths; the immutable strategy context contains hashes.
        ident=Store(request['strategy_root']).save(s,artifacts);status='completed'
        result={'status':status,'strategy_id':ident,'planning_directory':str(work),'physics_steps':0}
        write_new(work/'result.json',result);return result
    except Exception as exc:
        write_new(work/'failure.json',{'status':'failed','reason_code':exc.code if isinstance(exc,WorkflowError) else 'planning_error','error_type':type(exc).__name__});raise
    finally:write_new(work/'timing.json',{'ended_at':now(),'planning_wall_s':time.monotonic()-start,'physics_steps':0,'status':status})


def check_contract(path):
    path=Path(path)
    try:
        if file_hash(path)!=(path.parent/'experiment.sha256').read_text().strip():raise WorkflowError('preflight_failed','Contract digest')
        c=json.loads(path.read_text())
        if c['record_status']!='frozen_ready' or not c['preflight']['passed']:raise WorkflowError('preflight_failed','Not frozen ready')
        validate_strategy(c['workflow']['strategy']);validate_policy(c['workflow']['strategy']['policy'])
        hashes=c['preflight']['evidence_sha256']
        if str(Path(__file__).resolve()) not in hashes or str(Path(__file__).with_name('runner.py')) not in hashes:
            raise WorkflowError('preflight_failed','Workflow source not bound')
        for name in ('model','initial','targets','task','reference_states'):
            if c['inputs'][name] not in hashes:raise WorkflowError('preflight_failed','Input not bound '+name)
        if c['runtime']['actual_loaded_session_evidence'] not in hashes:raise WorkflowError('preflight_failed','Runtime not bound')
        for name,sha in hashes.items():
            if not Path(name).is_file() or file_hash(name)!=sha:raise WorkflowError('preflight_failed','Evidence changed '+name)
        runtime=json.loads(Path(c['runtime']['actual_loaded_session_evidence']).read_text())
        if not runtime['passed'] or not runtime['sessions_loaded'] or len(runtime['actual_session_providers'])!=3 or any(v!=['CPUExecutionProvider'] for v in runtime['actual_session_providers'].values()):raise WorkflowError('preflight_failed','Actual runtime providers')
        if runtime['versions']['tensorrt']!='10.13.0.35' or runtime['versions']['onnxruntime']!='1.23.2' or runtime['sonic_version']!='1.0':raise WorkflowError('preflight_failed','Runtime versions')
        if c['control']['fallback_allowed'] or c['budget']['attempts_per_case']!=1:raise WorkflowError('preflight_failed','Execution scope')
        return c
    except (OSError,KeyError,TypeError,json.JSONDecodeError) as exc:
        raise WorkflowError('preflight_failed','Incomplete frozen evidence: '+type(exc).__name__) from None


def prepare(strategy_id,request):
    """Revalidate stored strategy and freeze a fresh, fully evidenced experiment."""
    from ..cli import create_experiment
    bundle=Store(request['strategy_root']).load(strategy_id);s=bundle['strategy'];current=_strategy(request)
    if s['context']!=current['context'] or any(s[k]!=current[k] for k in ('bindings','phases','policy','command_pose_offset','source_frame_count','adapter')):
        raise WorkflowError('context_changed','Create a new strategy revision and revalidate')
    geometry=_geometry(request,s,s['proposal'].get('decision'))
    selected=next((r for r in geometry['candidates'] if r['id']==s['proposal']['stance']['id'] and r['feasible']),None)
    if selected is None:raise WorkflowError('preflight_failed','Saved stance no longer feasible')
    files=request['files'];template=json.loads(Path(files['record']).read_text())
    # Historical physical evidence may be reused only after verifying every bound file.
    for name,sha in template['preflight']['evidence_sha256'].items():
        if not Path(name).is_file() or file_hash(name)!=sha:raise WorkflowError('preflight_failed','Template evidence changed '+name)
    root=create_experiment(request['experiment_root'],'workflow_native');started=now()
    write_new(root/'planning_started.json',{'started_at':started});write_new(root/'request.json',request)
    try:
        for name in ('model','initial','targets','reference_states'):
            dest=root/(name+Path(files[name]).suffix);shutil.copyfile(files[name],dest);template['inputs'][name]=str(dest)
        task=json.loads(Path(files['task']).read_text());task['resources']['initial_state']=template['inputs']['initial']
        write_new(root/'task.json',task);template['inputs']['task']=str(root/'task.json')
        from .native import NativeAdapter,validate_physics
        from ..privileged import no_physics
        with no_physics():
            native=NativeAdapter(task,s,np.load(template['inputs']['initial']))
            try:
                validate_physics(native.m,json.loads(Path(template['physics']['physics_parameters_manifest']).read_text()))
                if not np.isclose(native.dt,json.loads(Path(files['alignment']).read_text())['command_dt_s']):raise WorkflowError('unsupported_capability','Source resampling required')
                write_new(root/'native_preflight.json',{'physics_steps':0,'actual_session_providers':native.providers,'initial_observation':native.observe(),
                    'model_nq':native.m.nq,'model_nv':native.m.nv,'model_nu':native.m.nu,'physics_dt_s':native.dt,'control_stride':native.control_stride,'bindings_verified':True,'physics_matches_manifest':True})
            finally:native.close()
        alignment=json.loads(Path(files['alignment']).read_text());write_new(root/'strategy.json',s);write_new(root/'stance_selection.json',{'selected':selected,'candidates':geometry['candidates']})
        with (root/'runtime.log').open('w') as log:
            subprocess.run([str(REPO/'scripts/humano_python.sh'),str(REPO/'scripts/check_humano_runtime.py'),'--load-models','--out',str(root/'runtime.json')],check=True,stdout=log,stderr=subprocess.STDOUT,cwd=REPO)
        policy=s['policy'];template.update(experiment_id=root.name,created_at=started,record_status='frozen_ready')
        template['purpose']={'hypothesis':'Structured strategy executes with explicit model/task settings and source-aligned contact phases',
            'baseline_paths':[str(Path(files['record']).parent)],'planned_changes':['New independent workflow adapter; dynamic phases/named entities; geometry-derived stance; compensation mode '+policy['compensation']['mode']],
            'invariants':['No changes to other pipelines','No original-token prefix or implicit fallback','No physical state writes after reset'],
            'known_confounders':['Not a matched controller comparison; new strategy/stance/approach timing','Single-condition execution does not establish generality']}
        template['workflow']={'schema_version':1,'strategy_id':strategy_id,'strategy':s,'selection':selected,'alignment':alignment}
        template['runtime']['actual_loaded_session_evidence']=str(root/'runtime.json')
        template['control']={'fallback_allowed':False,'actual_mode':'SONIC encoder1','prefix':'none','stage_interfaces':[
            {'stage':'navigation','mode':'SONIC encoder1 + live locomotion planner','fields':'world velocity m/s; facing yaw rad; body-relative initial wrist/torso hold'},
            {'stage':'settle','mode':'SONIC encoder1','fields':'source entry legs and roll/pitch reference, measured root XYZ/yaw; physically measured support/motion gate'},
            {'stage':'approach','mode':'SONIC encoder1','fields':'measured-state IK/RRT world wrist/torso reference only; explicit timing limits'},
            {'stage':'contact_phases','mode':'SONIC encoder1','fields':'source world poses with declared command alignment; original finger commands; calibration compensation only if declared'}],
            'settings_and_origins':policy}
        template['budget']={'attempts_per_case':1,'recovery_attempts':0,'simulation_seconds_per_attempt':policy['budgets']['simulation_s'],'stage_budgets':policy['budgets']}
        template['physics']['safety_gates']=policy['gates'];template['physics']['acceptance_thresholds']=policy['acceptance']
        template['physics']['control_dt_s']=policy['control_dt_s']
        template['physics']['acceptance_source_and_hash']['independent_validator']=str(Path(__file__).with_name('verification.py'))
        template['pipeline']['entrypoint']='python -m humanoid_mimicgen.workflow execute --experiment '+str(root)
        template['pipeline']['feedback_and_reflection_rules']='No automatic retry; persist failed attempt and reflection before new strategy revision; API cannot override frozen contract'
        write_new(root/'preflight_evidence.json',{'physics_steps':0,'fresh_geometry':geometry,'strategy_id':strategy_id,'actual_sessions':'runtime.json','template_dependencies_verified':True})
        write_new(root/'comparison_diff.json',{'baseline':str(Path(files['record']).parent),'changes':template['purpose']['planned_changes'],'not_a_matched_method_comparison':True})
        hashes=dict(template['preflight']['evidence_sha256']);snap=root/'source_snapshot';snap.mkdir()
        for source in sorted((REPO/'humanoid_mimicgen').rglob('*.py')):
            hashes[str(source)]=file_hash(source);dest=snap/source.relative_to(REPO/'humanoid_mimicgen');dest.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(source,dest)
        for source in root.iterdir():
            if source.is_file():hashes[str(source)]=file_hash(source)
        hashes[str(Path(files['record']).resolve())]=file_hash(files['record'])
        hashes[str(bundle['path']/'manifest.json')]=file_hash(bundle['path']/'manifest.json')
        template['preflight'].update(passed=True,evidence_paths=list(hashes),evidence_sha256=hashes,comparison_diff_path=str(root/'comparison_diff.json'))
        template['pipeline']['source_snapshot_and_hashes']=hashes
        write_new(root/'experiment.json',template);(root/'experiment.sha256').write_text(file_hash(root/'experiment.json')+'\n')
        write_new(root/'freeze.json',{'frozen_at':now(),'sha256':file_hash(root/'experiment.json')});check_contract(root/'experiment.json')
        return {'status':'prepared','experiment_id':root.name,'experiment_path':str(root),'strategy_id':strategy_id}
    except Exception as exc:
        write_new(root/'preflight_failure.json',{'ended_at':now(),'reason_code':exc.code if isinstance(exc,WorkflowError) else 'preflight_failed','error_type':type(exc).__name__});raise


def inspect(experiment):
    root=Path(experiment)
    if (root/'result.json').exists():return json.loads((root/'result.json').read_text())
    if (root/'execution/execution_audit.json').exists():return {'status':'awaiting_verification','audit':json.loads((root/'execution/execution_audit.json').read_text())}
    if (root/'execution').exists():return {'status':'execution_artifacts_present','liveness':'unknown','experiment_id':root.name}
    if (root/'experiment.sha256').exists():return {'status':'prepared','experiment_id':root.name}
    return {'status':'unprepared','experiment_id':root.name}


def execute(experiment):
    from .runner import execute_native
    from .verification import verify
    root=Path(experiment);execute_native(root/'experiment.json');return verify(root)
