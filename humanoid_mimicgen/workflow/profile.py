"""Explicit robot/task/numerical settings; no experiment-tuned fallback values."""
from .contracts import WorkflowError, positive, canonical

REQUIRED={
    'navigation':('speed_m_s','waypoint_radius_m','stop_radius_m'),
    'settle':('window_s','drift_m','speed_m_s','yaw_speed_rad_s','foot_force_N','minimum_s','continuous_s'),
    'approach':('joint_speed_rad_s','wrist_speed_m_s','wrist_speed_rad_s'),
    'gates':('contact_force_N','contact_entry_s','contact_loss_s','collision_force_N','collision_duration_s','collision_instant_N',
             'fall_height_m','fall_tilt_rad','precontact_translation_m','precontact_rotation_rad'),
    'acceptance':('clearance_m','hold_duration_s','root_drift_m','support_force_N'),
    'budgets':('navigation_s','settle_s','approach_s','simulation_s')}


def validate_policy(p):
    canonical(p)
    try:
        positive(p['control_dt_s'],'control period')
        for group,keys in REQUIRED.items():
            for key in keys:positive(p[group][key],group+'.'+key)
        if p['settle']['minimum_s']>p['budgets']['settle_s'] or p['settle']['continuous_s']>p['budgets']['settle_s']:
            raise WorkflowError('invalid_input','Impossible settling budget')
        if p['gates']['collision_instant_N']<p['gates']['collision_force_N']:raise WorkflowError('invalid_input','Collision thresholds reversed')
        if p['compensation']['mode'] not in ('none','identified_equilibrium','bounded_integral'):
            raise WorkflowError('invalid_input','Unsupported compensation; no empirical fallback')
        if p['compensation']['mode']=='identified_equilibrium' and (not p['compensation'].get('calibrations') or not p['compensation'].get('scope')):
            raise WorkflowError('needs_calibration')
        if p['compensation']['mode']=='bounded_integral':
            for key in ('gain_per_s','cap_m'):positive(p['compensation'][key],key)
            if not p['compensation'].get('evidence_file'):raise WorkflowError('invalid_input','Feedback requires bound evidence')
        nav=p['navigation']
        if nav.get('standing_orientation') not in (None,'source','upright'):raise WorkflowError('invalid_input','Standing orientation')
        if nav.get('profile') not in (None,'distance_velocity'):raise WorkflowError('invalid_input','Navigation profile')
        if nav.get('profile')=='distance_velocity':
            for key in ('acceleration_m_s2','response_s'):positive(nav[key],key)
        recovery=p.get('recovery',{'max_retries':0})
        if type(recovery['max_retries']) is not int or recovery['max_retries']<0:raise WorkflowError('invalid_input','Recovery budget')
        if recovery.get('posture_restore',False):
            for key in ('joint_tolerance_rad','position_tolerance_m','angle_tolerance_rad'):positive(recovery[key],key)
        if recovery['max_retries']:
            for key in ('release_min_s','release_timeout_s','retreat_timeout_s'):positive(recovery[key],key)
            if recovery['release_min_s']>recovery['release_timeout_s']:raise WorkflowError('invalid_input','Recovery timing')
        planning=p['planning'];positive(planning['penetration_tolerance_m'],'penetration tolerance',zero=True)
        for group in ('ik','rrt','base_path'):
            if not isinstance(planning[group],dict):raise WorkflowError('invalid_input','Planning options')
            for k,v in planning[group].items():positive(v,group+'.'+k,zero=k=='seed')
        for group in (*REQUIRED,'planning','control_dt_s','compensation'):
            origin=p['origins'][group]
            if not isinstance(origin,dict) or set(origin)!={'kind','evidence'} or not isinstance(origin['evidence'],str) or not origin['evidence']:
                raise WorkflowError('invalid_input','Settings need provenance: '+group)
            if origin['kind'] not in ('robot_limit','task_requirement','numerical_setting','measured_calibration','explicit_disabled'):
                raise WorkflowError('invalid_input','Invalid settings provenance')
    except (KeyError,TypeError) as exc:
        raise WorkflowError('invalid_input','Incomplete execution profile: '+str(exc)) from None
    return p
