"""Bounded review-only page context. Never a quotation or identity proof."""
from hashlib import sha256
from pathlib import Path
import math,time,uuid,re,base64
from io import BytesIO
from PIL import Image
from .search.session import bounded_timeout
from .search.failure_policy import ResearchControlError
from .bounded_cdp import page_cdp

KEYS={'version','role','filename','sha256','url','captured_at','width','height','document_width','document_height','truncated'}
def capture_context(page,path,url):
    bounded_timeout(1)  # No browser work after an owned stop.
    path=Path(path);temporary=path.with_name(path.stem+'-'+uuid.uuid4().hex+'.tmp.png')
    from .search.product_identity import canonical_card_url
    phase_deadline=time.monotonic()+bounded_timeout(3)
    native_loading=False
    try:
        if canonical_card_url(page.url)!=canonical_card_url(url):raise ValueError('context_url_changed')
        with page_cdp(page,phase_deadline) as session:
            reply=session.send('Runtime.evaluate',{'expression':'({width:Math.max(document.documentElement.scrollWidth,document.body?.scrollWidth||0),height:Math.max(document.documentElement.scrollHeight,document.body?.scrollHeight||0),font_status:document.fonts?.status||"unknown",document_url:document.URL})','returnByValue':True,'timeout':max(1,int(bounded_timeout(max(0,getattr(session,'command_deadline',phase_deadline-.15)-time.monotonic()))*1000))})
            if reply.get('exceptionDetails'):raise ValueError('context_dimensions_timeout_or_error')
            dims=reply.get('result',{}).get('value')
            bounded_timeout(1)
            if type(dims) is not dict or any(type(dims.get(k)) not in (int,float) or not math.isfinite(dims[k]) or dims[k]<=0 for k in ('width','height')):raise ValueError('context_dimensions_invalid')
            if dims.get('document_url',page.url)!=page.url:raise ValueError('context_url_changed')
            width=min(1920,int(dims['width']));height=min(2400,int(dims['height']))
            clip={'x':0,'y':0,'width':width,'height':height}
            native_loading=dims.get('font_status')=='loading'
            if native_loading:
                reply=session.send('Page.captureScreenshot',{'format':'png','captureBeyondViewport':True,'clip':dict(clip,scale=1)})
                encoded=reply.get('data') if type(reply) is dict else None
                if type(encoded) is not str or len(encoded)>28*1024*1024:raise ValueError('context_native_payload_invalid')
                try:data=base64.b64decode(encoded,validate=True)
                except (ValueError,TypeError):raise ValueError('context_native_payload_invalid')
            else:data=session.screenshot(clip)
        bounded_timeout(1)
        if canonical_card_url(page.url)!=canonical_card_url(url):raise ValueError('context_url_changed')
        if type(data) is not bytes:raise ValueError('context_native_payload_invalid')
        if len(data)>20*1024*1024:raise ValueError('context_size_limit')
        with Image.open(BytesIO(data)) as image:
            if image.format!='PNG' or image.size!=(width,height):
                page._kac_context_dimensions={'requested':[width,height],'actual':list(image.size)}
                raise ValueError('context_image_invalid')
            image.verify()
        evidence={'version':1,'role':'review_context','filename':path.name,'sha256':sha256(data).hexdigest(),'url':url,'captured_at':time.time(),'width':width,'height':height,'document_width':dims['width'],'document_height':dims['height'],'truncated':dims['width']>width or dims['height']>height}
        bounded_timeout(1)
        if time.monotonic()>=phase_deadline:raise TimeoutError('context_capture_timeout')
        temporary.write_bytes(data)
        bounded_timeout(1)
        if time.monotonic()>=phase_deadline:raise TimeoutError('context_capture_timeout')
        temporary.replace(path)
        warnings=[]
        if native_loading:warnings.append('Снимок текущего кадра браузера; шрифты ещё загружаются')
        if evidence['truncated']:warnings.append('Контекст ограничен размером снимка; нижняя/правая часть страницы не приложена')
        return evidence,'; '.join(warnings)
    except ResearchControlError:raise
    except Exception as error:
        bounded_timeout(1)  # Global stop outranks local screenshot failure.
        reason=str(error) if type(error) is ValueError and str(error) in {'context_url_changed','context_dimensions_timeout_or_error','context_dimensions_invalid','context_image_invalid','context_size_limit','context_native_payload_invalid','context_sdk_bridge_unsupported','context_channel_cleanup_unacknowledged'} else type(error).__name__
        return {},'Контекст для проверки не снят: '+reason
    finally:
        temporary.unlink(missing_ok=True)

def context_valid(evidence,reference,url,data):
    try:
        if type(evidence) is not dict or set(evidence)!=KEYS or type(evidence['version']) is not int or evidence['version']!=1 or evidence['role']!='review_context':return False
        if type(reference) is not str or len(reference)>4096 or type(url) is not str or len(url)>4096:return False
        if type(evidence['filename']) is not str or len(evidence['filename'])>255 or type(evidence['sha256']) is not str or not re.fullmatch('[a-f0-9]{64}',evidence['sha256']):return False
        if evidence['filename']!=Path(reference).name or evidence['url']!=url or evidence['sha256']!=sha256(data).hexdigest():return False
        if type(evidence['captured_at']) not in (int,float) or not math.isfinite(evidence['captured_at']) or not 0<evidence['captured_at']<=time.time()+300:return False
        if type(evidence['truncated']) is not bool:return False
        for k,limit in (('width',1920),('height',2400)):
            if type(evidence[k]) is not int or not 0<evidence[k]<=limit:return False
            doc=evidence['document_'+k]
            if type(doc) not in (int,float) or not math.isfinite(doc) or doc<evidence[k]:return False
        if evidence['truncated']!=(evidence['document_width']>evidence['width'] or evidence['document_height']>evidence['height']):return False
        with Image.open(BytesIO(data)) as image:
            if image.format!='PNG' or image.size!=(evidence['width'],evidence['height']):return False
            image.verify()
        return True
    except (KeyError,TypeError,ValueError,OSError):return False
