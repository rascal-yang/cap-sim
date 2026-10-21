from datetime import datetime
from zoneinfo import ZoneInfo
import json
from pathlib import Path
import pytest
from humanoid_mimicgen.augmentation.records import create_record, index_history


def test_new_record_uses_shanghai_date_and_preserves_collision(tmp_path):
    clock=datetime(2026,10,21,16,7,tzinfo=ZoneInfo('Asia/Shanghai'))
    first=create_record(tmp_path,'test',clock);(first/'keep').write_text('original')
    second=create_record(tmp_path,'test',clock)
    assert first.relative_to(tmp_path).as_posix()=='2026-10/21/20261021_1607_test'
    assert second.name=='20261021_1607_test_01'
    assert (first/'keep').read_text()=='original'
    assert json.loads((first/'creation.json').read_text())['created_at'].endswith('+08:00')


def test_history_index_preserves_paths_bytes_and_date_only_names(tmp_path):
    old=tmp_path/'20261021_old';old.mkdir();(old/'frozen.json').write_bytes(b'untouched')
    unknown=tmp_path/'apple_pipeline_v5';unknown.mkdir()
    x=index_history(tmp_path)
    alias=tmp_path/'2026-10/21'/old.name
    assert alias.is_symlink() and alias.resolve()==old
    assert old.is_dir() and not old.is_symlink()
    assert (old/'frozen.json').read_bytes()==b'untouched'
    assert x['unclassified']==['apple_pipeline_v5']
    assert index_history(tmp_path)['created_links']==0


def test_conflicting_history_entry_is_never_overwritten(tmp_path):
    (tmp_path/'20261021_old').mkdir()
    target=tmp_path/'2026-10/21/20261021_old';target.mkdir(parents=True)
    with pytest.raises(FileExistsError):index_history(tmp_path)
    assert not target.is_symlink()
