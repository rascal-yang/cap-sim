import pytest
from humanoid_mimicgen.workflow.service import input_context, check_contract
from humanoid_mimicgen.workflow.contracts import WorkflowError


def test_missing_evidence_does_not_get_default(tmp_path):
    with pytest.raises(WorkflowError):input_context({'targets':str(tmp_path/'missing')})
    p=tmp_path/'a';p.write_text('a');c=input_context({'targets':str(p)})
    p.write_text('b');assert input_context({'targets':str(p)})!=c


def test_unfrozen_contract_cannot_launch(tmp_path):
    p=tmp_path/'experiment.json';p.write_text('{}')
    with pytest.raises(WorkflowError):check_contract(p)
