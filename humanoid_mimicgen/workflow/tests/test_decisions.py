import json
import pytest
from humanoid_mimicgen.workflow.decisions import ResponsesDecision, validate_proposal, replay_decision
from humanoid_mimicgen.workflow.contracts import WorkflowError


def proposal():
    return {'action':'propose_stances','stances':[{'id':'face','xyyaw':[1.,2.,0.]}],
            'evidence_ids':['scene'],'reason':'reach target'}


def test_invalid_evidence_and_nonfinite_rejected():
    p=proposal();p['evidence_ids']=['invented']
    with pytest.raises(WorkflowError):validate_proposal(p,{'scene':{}})
    p=proposal();p['stances'][0]['xyyaw'][0]=float('nan')
    with pytest.raises(WorkflowError):validate_proposal(p,{'scene':{}})


def test_missing_key_is_audited(tmp_path,monkeypatch):
    monkeypatch.delenv('WORKFLOW_TEST_API_KEY',raising=False)
    provider=ResponsesDecision('gpt-6-astra','https://api.openai.com/v1','WORKFLOW_TEST_API_KEY',1.)
    with pytest.raises(WorkflowError,match='api_unavailable'):provider.propose({'scene':{}},tmp_path/'call')
    a=json.loads((tmp_path/'call/provenance.json').read_text());assert a['status']=='failed'
    assert 'Authorization' not in (tmp_path/'call/request.json').read_text()


def test_saved_decision_replay_validates_evidence_without_network(tmp_path):
    (tmp_path/'response.json').write_text(json.dumps(proposal()))
    p=replay_decision(tmp_path/'response.json',{'scene':{}},provider_identity='test_fixture')
    assert p['action']=='propose_stances'
    with pytest.raises(WorkflowError):replay_decision(tmp_path/'response.json',{},provider_identity='test_fixture')
