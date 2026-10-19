"""Live native stage execution. All task limits come from a frozen strategy."""
import json
import time
from pathlib import Path
import numpy as np
from .contracts import WorkflowError, canonical, now, phase_at
from .execution import GateMonitor, base_is_stable, finite_record
from .native import NativeAdapter, validate_physics, validate_model_identity
from .planning import measured_approach, measured_retreat, navigate_command, validate_source
from .calibration import equilibrium_command
from .feedback import IntegralFeedback,can_recover


def execute_native(contract):
    from .service import check_contract
    from cap_sim.backends.native_sonic import rot,quat
    from ..planned_approach import planned_pose
    from ..standing_root_reference import standing_root_reference
    c=check_contract(contract);root=Path(contract).parent;out=root/'execution';out.mkdir(exist_ok=False)
    strategy=c['workflow']['strategy'];policy=strategy['policy'];bindings=strategy['bindings'];phases=strategy['phases']
    targets={k:v.copy() for k,v in np.load(c['inputs']['targets']).items()}
    source=np.load(c['inputs']['reference_states'])['samples'];initial=np.load(c['inputs']['initial'])
    validate_source(targets,strategy,c['workflow']['alignment'])
    entry=phases[0]['start']+strategy['command_pose_offset'];last=phases[-1]['end'];frame=phases[0]['start']
    selected=c['workflow']['selection'];route=np.asarray(selected['navigation_path']);goal=route[-1];waypoint=1
    task=json.loads(Path(c['inputs']['task']).read_text());start=now();clock=time.monotonic();adapter=None
    states=[];observations=[];commands=[];tracking=[];events=[];failure=None;error_type=None;steps=0;stage='navigation';online_planning_s=0.
    stage_started=0.;settled=0.;gate=GateMonitor(policy['gates']);phase_id=None;completed=False;holdposes=None;plan_times=plan_poses=None
    attempted=[];retry_index=0;force_tick=False;retreat_goals=None
    recovery=policy.get('recovery',{'max_retries':0});base_targets={k:v.copy() for k,v in targets.items()}
    comp=policy['compensation'];feedback=IntegralFeedback(bindings['hands'],gain_per_s=comp['gain_per_s'],cap_m=comp['cap_m']) if comp['mode']=='bounded_integral' else None
    contact_started=None;feedback_log=[];goalposes=None
    def write(name,value):(out/name).write_bytes(canonical(finite_record(value)))
    def begin_recovery(reason,ob):
        nonlocal retry_index,stage,stage_started,holdposes,gate,phase_id,force_tick,failure
        if not can_recover(reason,ob,retry_index,recovery['max_retries'],policy['acceptance']['support_force_N']):return False
        record={'reason':reason,'retry_index':retry_index,'time_s':float(adapter.d.time),'observation':ob,
                'feedback_bias_m':{h:v.tolist() for h,v in feedback.bias.items()} if feedback else {},
                'next_action':'open supported object, retreat through checked path, reobserve and retarget, settle and retry',
                'hypothesis':'Measured wrist bias and object motion require reanchored approach; no unobserved gain changes'}
        write('retry_'+str(retry_index)+'_reflection.json',record)
        np.savez_compressed(out/('retry_'+str(retry_index)+'_checkpoint.npz'),qpos=adapter.d.qpos.copy(),qvel=adapter.d.qvel.copy(),ctrl=adapter.d.ctrl.copy(),qacc_warmstart=adapter.d.qacc_warmstart.copy())
        events.append(dict(record,event='retry_requested'));retry_index+=1
        holdposes=adapter.poses();stage='recovery_open';stage_started=float(adapter.d.time)
        phase_id=None;gate=GateMonitor(policy['gates']);force_tick=True;failure=None
        return True
    try:
        adapter=NativeAdapter(task,strategy,initial);m,d=adapter.m,adapter.d;dt=adapter.dt;stride=adapter.control_stride
        if not np.isclose(c['workflow']['alignment']['command_dt_s'],dt):raise WorkflowError('unsupported_capability','Source command resampling is not implicit')
        physics=json.loads(Path(c['physics']['physics_parameters_manifest']).read_text())
        validate_physics(m,physics)
        import mujoco
        validate_model_identity(mujoco.MjModel.from_binary_path(c['inputs']['model']),m)
        write('actual_model_files.json',adapter.model_files)
        write('actual_providers.json',adapter.providers)
        np.savez_compressed(out/'actual_initial.npz',qpos=d.qpos.copy(),qvel=d.qvel.copy(),ctrl=d.ctrl.copy(),qacc_warmstart=d.qacc_warmstart.copy())
        initial_pose=adapter.poses();initial_rot=rot(d.qpos[3:7]);rel={s:(initial_rot.inv().apply(p[:3]-d.qpos[:3]),initial_rot.inv()*rot(p[3:])) for s,p in initial_pose.items()}
        object_anchor=np.asarray(adapter.observe()['object_pose']);write('initial_observation.json',adapter.observe())
        maxsteps=int(policy['budgets']['simulation_s']/dt)
        while steps<maxsteps:
            tick=steps%stride==0 or force_tick;force_tick=False;velocity=np.zeros(2);face=float(goal[2]);source_index=-1
            if stage=='navigation':
                velocity,face,arrived,waypoint=navigate_command([*d.qpos[:2],rot(d.qpos[3:7]).as_euler('xyz')[2]],route,waypoint,policy['navigation'])
                if arrived and tick:
                    adapter.sonic.navigating=False
                    adapter.sonic.anchor=np.r_[d.qpos[:7],source[entry,1+adapter.b.qids]].copy()
                    adapter.sonic.anchor[:7]=standing_root_reference(d.qpos[:7],source[entry,1:8]);adapter.sonic.ref=None
                    holdposes=adapter.poses();stage='settle';stage_started=float(d.time);velocity[:]=0
                    events.append({'event':'navigation_stop_requested','time_s':float(d.time),'actual_root':d.qpos[:7].tolist(),'goal_xyyaw':goal.tolist()})
                elif d.time>=policy['budgets']['navigation_s']:raise WorkflowError('navigation_timeout')
            contact_stage=stage=='contact'
            phase=phase_at(phases,frame) if contact_stage else None
            if contact_stage:
                source_index=frame
                if phase_id!=phase['id']:
                    ob=adapter.observe()
                    if phase['requires_contact'] and (gate.contact_s<policy['gates']['contact_entry_s'] or ob['contacts']['hand_object_N']<=policy['gates']['contact_force_N']):
                        if begin_recovery('contact_entry_failed',ob):continue
                        raise WorkflowError('contact_entry_failed',phase['id'])
                    if phase['requires_clearance'] and ob['clearance_m']<policy['acceptance']['clearance_m']:
                        if begin_recovery('clearance_entry_failed',ob):continue
                        raise WorkflowError('clearance_entry_failed',phase['id'])
                    phase_id=phase['id'];events.append({'event':'phase_started','phase':phase_id,'time_s':float(d.time),'source_frame':frame})
            label=phase_id if contact_stage else stage
            if tick:
                if stage=='navigation':
                    heading=rot(d.qpos[3:7]);goalposes={s:np.r_[d.qpos[:3]+heading.apply(pos),quat(heading*ori)] for s,(pos,ori) in rel.items()}
                elif stage in ('settle','recovery_open'):goalposes={s:p.copy() for s,p in holdposes.items()}
                elif stage in ('approach','recovery_retreat'):goalposes={s:planned_pose(plan_times,plan_poses[s],float(d.time)-stage_started) for s in ('left','right','torso')}
                else:goalposes={s:targets[s+'_pose'][frame+strategy['command_pose_offset']].copy() for s in ('left','right','torso')}
                desired={s:p.copy() for s,p in goalposes.items()}
                comp=policy['compensation']
                if stage in ('approach','contact') and comp['mode']=='identified_equilibrium':
                    for hand in bindings['hands']:goalposes[hand][:3]=equilibrium_command(desired[hand][:3],comp['calibrations'][hand],comp['scope'])
                poses=adapter.poses()
                if feedback and stage in ('approach','contact'):
                    for hand in bindings['hands']:
                        bias=feedback.update(hand,desired[hand][:3],poses[hand][:3],policy['control_dt_s'])
                        goalposes[hand][:3]+=bias
                        feedback_log.append({'time_s':float(d.time),'retry_index':retry_index,'hand':hand,'bias_m':bias.tolist(),'error_m':(desired[hand][:3]-poses[hand][:3]).tolist()})
                row={'time_s':float(d.time),'stage':label,'source_index':source_index,'retry_index':retry_index,'mode':1}
                for side in ('left','right'):
                    p=desired[side];row[side]={'target_pose':p.tolist(),'commanded_pose':goalposes[side].tolist(),'actual_pose':poses[side].tolist(),
                        'position_error_m':float(np.linalg.norm(poses[side][:3]-p[:3])),
                        'orientation_error_rad':float((rot(p[3:]).inv()*rot(poses[side][3:])).magnitude())}
                tracking.append(row)
            fingers=targets['fingers'][frame if contact_stage else entry]
            adapter.command(goalposes,velocity,face,float(source[entry,3]),fingers,policy_tick=tick)
            attempted.append({'time_s':float(d.time),'stage':label,'source_index':source_index,'retry_index':retry_index,'mode':1,'fingers_rad':fingers.tolist(),'body_target_rad':adapter.last_target.tolist(),'ctrl_Nm':d.ctrl.tolist()})
            adapter.step();steps+=1;ob=adapter.observe();ob.update(stage=label,source_poststep_frame=source_index,retry_index=retry_index)
            states.append(np.r_[d.time,d.qpos,d.qvel,d.ctrl]);observations.append(ob);commands.append(attempted[-1])
            limits=policy['gates'];contacts=ob['contacts'];failure=gate.update(dt,hand_force=contacts['hand_object_N'],obstacle_force=contacts['robot_obstacle_N'],requires_contact=bool(phase and phase['requires_contact']))
            tilt=float(np.arccos(np.clip(rot(d.qpos[3:7]).as_matrix()[2,2],-1,1)))
            if not np.isfinite(d.qpos).all() or not np.isfinite(d.qvel).all():raise WorkflowError('nonfinite_state')
            if d.qpos[2]<limits['fall_height_m'] or tilt>limits['fall_tilt_rad']:raise WorkflowError('fall')
            if any(w.number for w in d.warning):raise WorkflowError('simulation_warning')
            if failure:
                if begin_recovery(failure,ob):continue
                break
            if not contact_stage and not stage.startswith('recovery_'):
                op=np.asarray(ob['object_pose'])
                if np.linalg.norm(op[:3]-object_anchor[:3])>limits['precontact_translation_m'] or (rot(object_anchor[3:]).inv()*rot(op[3:])).magnitude()>limits['precontact_rotation_rad']:
                    raise WorkflowError('object_moved_before_grasp')
                if contacts['robot_object_N']>limits['contact_force_N']:raise WorkflowError('premature_object_contact')
            if stage=='settle':
                elapsed=float(d.time)-stage_started;window=policy['settle']['window_s']
                rows=[x for x in observations if x['time_s']>=float(d.time)-window-dt/2]
                drift=None if elapsed<window or not rows else max(float(np.linalg.norm(np.array(x['root_pose'][:2])-rows[0]['root_pose'][:2])) for x in rows)
                ob['recent_root_drift_m']=drift;stable=base_is_stable(ob,policy['settle']);settled=settled+dt if stable else 0.
                ob.update(stable_sample=stable,continuous_stable_s=settled)
                if elapsed>=policy['settle']['minimum_s'] and steps%stride==0 and settled+1e-12>=policy['settle']['continuous_s']:
                    actual=adapter.poses()
                    if retreat_goals is None:retreat_goals={h:p.copy() for h,p in actual.items()}
                    goals={s:(targets[s+'_pose'][entry] if s in bindings['hands'] else actual[s]) for s in ('left','right')}
                    pstart=time.monotonic();plan_times,plan_poses,pa=measured_approach(m,d.qpos.copy(),goals,policy);online_planning_s+=time.monotonic()-pstart
                    write('planning_audit_'+str(retry_index)+'.json',dict(pa,planning_wall_s=online_planning_s));np.savez_compressed(out/('planned_approach_'+str(retry_index)+'.npz'),time_s=plan_times,**{k+'_pose':v for k,v in plan_poses.items()})
                    for s in ('left','right'):
                        if s not in bindings['hands']:targets[s+'_pose'][:]=actual[s]
                    targets['torso_pose'][:]=plan_poses['torso'][-1];np.savez_compressed(out/'adapted_execution_targets.npz',**targets)
                    stage='approach';stage_started=float(d.time);events.append({'event':'measured_stance_plan','time_s':float(d.time),'actual_root':d.qpos[:7].tolist()})
                elif elapsed>=policy['budgets']['settle_s']:raise WorkflowError('settling_timeout')
            elif stage=='approach':
                elapsed=float(d.time)-stage_started
                window=policy['settle']['window_s']
                rows=[x for x in observations if x['time_s']>=float(d.time)-window-dt/2]
                ob['recent_root_drift_m']=None if elapsed<window else max(float(np.linalg.norm(np.array(x['root_pose'][:2])-rows[0]['root_pose'][:2])) for x in rows)
                stable=base_is_stable(ob,policy['settle']);settled=settled+dt if stable else 0.
                ob.update(stable_sample=stable,continuous_stable_s=settled)
                if elapsed>=plan_times[-1] and settled+1e-12>=policy['settle']['continuous_s'] and steps%stride==0:
                    stage='contact';contact_started=float(d.time);events.append({'event':'contact_started','retry_index':retry_index,'time_s':contact_started,'actual_root':d.qpos[:7].tolist(),'base_stable':True,'wrist_pose_gate':False})
                elif elapsed>=policy['budgets']['approach_s']:raise WorkflowError('approach_timeout')
            elif stage=='recovery_open':
                elapsed=float(d.time)-stage_started
                if contacts['support_N']<=policy['acceptance']['support_force_N']:raise WorkflowError('recovery_support_lost')
                if elapsed>=recovery['release_min_s'] and contacts['hand_object_N']<=limits['contact_force_N'] and steps%stride==0:
                    plan_times,plan_poses,pa=measured_retreat(m,d.qpos.copy(),retreat_goals,policy)
                    write('retreat_plan_'+str(retry_index)+'.json',pa)
                    stage='recovery_retreat';stage_started=float(d.time)
                    events.append({'event':'retreat_started','retry_index':retry_index,'time_s':float(d.time)})
                elif elapsed>=recovery['release_timeout_s']:raise WorkflowError('recovery_release_timeout')
            elif stage=='recovery_retreat':
                elapsed=float(d.time)-stage_started
                if contacts['support_N']<=policy['acceptance']['support_force_N']:raise WorkflowError('recovery_support_lost')
                if elapsed>=plan_times[-1] and steps%stride==0:
                    from ..skill_planning import adapt_poses
                    current_object=np.asarray(ob['object_pose'])
                    for hand in bindings['hands']:targets[hand+'_pose']=adapt_poses(base_targets[hand+'_pose'],object_anchor,current_object)
                    base_targets={k:v.copy() for k,v in targets.items()};object_anchor=current_object.copy()
                    frame=phases[0]['start'];phase_id=None;gate=GateMonitor(policy['gates'])
                    holdposes=adapter.poses();settled=0.;stage='settle';stage_started=float(d.time)
                    events.append({'event':'retry_reanchored','retry_index':retry_index,'time_s':float(d.time),'object_pose':current_object.tolist()})
                elif elapsed>=recovery['retreat_timeout_s']:raise WorkflowError('recovery_retreat_timeout')
            elif contact_stage:
                frame+=1
                if frame==last:completed=True;break
        if not completed and failure is None:failure='simulation_budget_exhausted'
    except Exception as exc:
        failure=exc.code if isinstance(exc,WorkflowError) else 'execution_error';error_type=type(exc).__name__
        write('error.json',{'reason_code':failure,'error_type':error_type,'detail':exc.detail if isinstance(exc,WorkflowError) else str(exc)})
    finally:
        np.savez_compressed(out/'states.npz',samples=np.asarray(states))
        for name,value in [('observations',observations),('commands',commands),('attempted_commands',attempted),('tracking',tracking),('feedback',feedback_log),('events',events),('contacts',[x['contacts'] for x in observations])]:write(name+'.json',value)
        if adapter:
            write('controller.json',adapter.sonic.records);write('planner.json',adapter.sonic.planner_records)
        audit={'experiment_id':root.name,'started_at':start,'ended_at':now(),'simulation_wall_s':time.monotonic()-clock,
               'online_planning_wall_s':online_planning_s,'sim_seconds':float(adapter.d.time) if adapter else 0.,
               'recovery_count':retry_index,'completed':completed,'failure':failure,'error_type':error_type,'physics_steps':steps,'recorded_steps':len(states),'attempted_commands':len(attempted),'completed_commands':len(commands),
               'actual_encoder_modes':sorted(set(x['mode'] for x in commands)),'original_token_prefix_count':0,'mode0_calls':0,
               'midrun_state_resets':0,'fallback':False,'contact_started_at_s':contact_started,'stage':stage,'source_end_exclusive':frame}
        write('execution_audit.json',audit)
        if adapter:
            np.savez_compressed(out/'terminal.npz',qpos=adapter.d.qpos.copy(),qvel=adapter.d.qvel.copy(),ctrl=adapter.d.ctrl.copy(),qacc_warmstart=adapter.d.qacc_warmstart.copy())
            adapter.close()
    return audit
