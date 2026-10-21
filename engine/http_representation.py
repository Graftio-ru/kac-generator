"""Pure bounded HTTP content decoding; original controls remain caller-owned."""
from dataclasses import dataclass
import zlib

VERSION='bounded_content_v1'
BLOCK=64*1024

class RepresentationError(ValueError):
 def __init__(self,reason,metadata):
  self.reason=reason;self.metadata=dict(metadata,outcome='unsupported_or_invalid',reason=reason);super().__init__(reason)

class RepresentationLimit(ValueError):
 def __init__(self,reason,metadata):
  self.reason=reason;self.metadata=dict(metadata,outcome='size_limit',reason=reason);super().__init__(reason)

@dataclass(frozen=True)
class DecodedBody:
 content:bytes
 metadata:dict

def encoding_kind(value):
 normalized=value.strip().lower() if isinstance(value,str) else None
 return 'identity' if normalized in {'','identity'} else normalized if normalized in {'gzip','deflate'} else 'unsupported'

def decode_chunks(encoding,chunks,max_bytes,check):
 """Bound both wire and decoded bytes without flush or unbounded decompression."""
 if type(max_bytes) is not int or max_bytes<=0:raise ValueError('Positive integer body bound required')
 check()
 kind=encoding_kind(encoding);meta={'version':VERSION,'encoding_kind':kind,'raw_bytes':0,'decoded_bytes':0,'members':0}
 if kind=='unsupported':raise RepresentationError('unsupported_encoding',meta)
 decoder=zlib.decompressobj(31 if kind=='gzip' else 15) if kind!='identity' else None
 output=[];iterator=iter(chunks)
 def append(data):
  meta['decoded_bytes']+=len(data)
  if meta['decoded_bytes']>max_bytes:raise RepresentationLimit('decoded_bytes_limit',meta)
  if data:output.append(data)
 while True:
  check()
  try:chunk=next(iterator)
  except StopIteration:break
  check()
  if type(chunk) is not bytes:raise RepresentationError('invalid_chunk',meta)
  meta['raw_bytes']+=len(chunk)
  if meta['raw_bytes']>max_bytes:raise RepresentationLimit('raw_bytes_limit',meta)
  for offset in range(0,len(chunk),BLOCK):
   check();pending=chunk[offset:offset+BLOCK]
   if kind=='identity':append(pending);check();continue
   if kind=='deflate' and decoder.eof:raise RepresentationError('trailing_data',meta)
   while True:
    check()
    if decoder is None:decoder=zlib.decompressobj(31)
    limit=min(BLOCK,max_bytes-meta['decoded_bytes']+1)
    previous=pending
    try:decoded=decoder.decompress(pending,limit)
    except zlib.error:
     check()
     raise RepresentationError('trailing_data' if meta['members'] else 'malformed_stream',meta) from None
    check();append(decoded)
    if decoder.eof:
     meta['members']+=1;pending=decoder.unused_data
     if kind=='deflate':
      if pending:raise RepresentationError('trailing_data',meta)
      break
     decoder=None
     if not pending:break
     continue
    pending=decoder.unconsumed_tail
    if pending and pending==previous and not decoded:raise RepresentationError('malformed_stream',meta)
    if not pending and not decoded:break
    # Empty-input drains are bounded calls as well; never use flush().
 check()
 if kind=='gzip' and (decoder is not None or not meta['members']):raise RepresentationError('trailing_data' if meta['members'] else 'truncated_stream',meta)
 if kind=='deflate' and not decoder.eof:raise RepresentationError('truncated_stream',meta)
 return DecodedBody(b''.join(output),dict(meta,outcome='decoded',reason='complete'))
