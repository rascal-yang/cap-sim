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


def test_cli_keeps_native_diagnostics_off_json_stdout(monkeypatch,capfd):
    import os,json
    from humanoid_mimicgen.workflow import service
    from humanoid_mimicgen.workflow.__main__ import main
    def noisy(_):
        print('python diagnostic');os.write(1,b'native diagnostic\n')
        return {'status':'prepared'}
    monkeypatch.setattr(service,'inspect',noisy)
    assert main(['inspect','--experiment','.'])==0
    captured=capfd.readouterr()
    assert json.loads(captured.out)=={'status':'prepared'}
    assert 'native diagnostic' in captured.err and 'python diagnostic' in captured.err
