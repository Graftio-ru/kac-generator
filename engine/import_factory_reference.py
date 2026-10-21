"""Internal local technical intake and independent-review activation. No fetches."""
import argparse,json
from pathlib import Path
from .search.factory_reference import (approve_origin,import_reference,activate_reference,
 revoke_reference,review_bindings,MAX_BYTES,FactoryEvidenceError)

def _bounded(path,limit):
 path=Path(path)
 if path.is_symlink() or path.stat().st_size>limit:raise FactoryEvidenceError('factory_input_oversize')
 with path.open('rb') as stream:raw=stream.read(limit+1)
 if len(raw)>limit:raise FactoryEvidenceError('factory_input_oversize')
 return raw

def main():
 parser=argparse.ArgumentParser();commands=parser.add_subparsers(dest='action',required=True)
 origin=commands.add_parser('approve-origin')
 for name in ['manufacturer','origin','approval-id']:origin.add_argument('--'+name,required=True)
 origin.add_argument('--revision',type=int,required=True);origin.add_argument('--inactive',action='store_true')
 item=commands.add_parser('import')
 for name in ['body','url','manufacturer','article']:item.add_argument('--'+name,required=True)
 for name in ['observed-at','valid-until']:item.add_argument('--'+name,type=float,required=True)
 for name in ['inspect','activate','revoke']:
  child=commands.add_parser(name);child.add_argument('--reference',required=True)
  if name=='activate':child.add_argument('--review',required=True);child.add_argument('--evidence',required=True)
  if name=='revoke':child.add_argument('--revision',type=int,required=True)
 args=vars(parser.parse_args());action=args.pop('action')
 if action=='approve-origin':args['active']=not args.pop('inactive');result=approve_origin(**args)
 elif action=='import':args['body']=_bounded(args['body'],MAX_BYTES);result=import_reference(**args)
 else:
  ref=json.loads(_bounded(args.pop('reference'),4096))
  if action=='inspect':result=review_bindings(ref)
  elif action=='activate':result=activate_reference(ref,json.loads(_bounded(args['review'],1024*1024)),_bounded(args['evidence'],1024*1024))
  else:result=revoke_reference(ref,revision=args['revision'])
 print(json.dumps(result,ensure_ascii=False))
if __name__=='__main__':main()
