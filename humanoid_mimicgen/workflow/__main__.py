"""JSON CLI. Use scripts/humano_python.sh for all native operations."""
import argparse
import json
from pathlib import Path
import sys
from .contracts import WorkflowError, canonical


def main(argv=None):
    p=argparse.ArgumentParser(description='Independent reproducible manipulation workflow')
    sub=p.add_subparsers(dest='command',required=True)
    for name in ('plan','prepare'):
        cmd=sub.add_parser(name);cmd.add_argument('--request',required=True)
        if name=='prepare':cmd.add_argument('--strategy-id',required=True)
    for name in ('execute','inspect','verify'):
        cmd=sub.add_parser(name);cmd.add_argument('--experiment',required=True)
    args=p.parse_args(argv)
    try:
        from . import service
        if args.command in ('plan','prepare'):
            path=Path(args.request).resolve();request=json.loads(path.read_text());base=path.parent
            request['files']={k:str((base/v).resolve()) for k,v in request['files'].items()}
            for k in ('strategy_root','experiment_root'):request[k]=str((base/request[k]).resolve())
            if request['decision']['mode']=='saved':request['decision']['path']=str((base/request['decision']['path']).resolve())
            result=service.plan(request) if args.command=='plan' else service.prepare(args.strategy_id,request)
        elif args.command=='verify':
            from .verification import verify
            result=verify(args.experiment)
        else:result=getattr(service,args.command)(args.experiment)
        print(canonical(result).decode());return 0 if result.get('status')!='failed' else 1
    except WorkflowError as exc:
        print(canonical({'status':'failed','reason_code':exc.code,'detail':exc.detail}).decode());return 2
    except (OSError,ValueError,KeyError,TypeError) as exc:
        print(canonical({'status':'failed','reason_code':'invalid_input','error_type':type(exc).__name__}).decode());return 2

if __name__=='__main__':sys.exit(main())
