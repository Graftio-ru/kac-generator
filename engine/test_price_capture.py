"""Offline fixtures are synthetic test data, never fresh supplier evidence."""
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock,patch

from engine.price_capture import (qualify_snapshot,capture_price,capture_review_reason,
                                  visible_prices,require_price_captures,DOM_SNAPSHOT)
from engine.test_purchase_scope import fixture_purchase_snapshot
from engine.models import PriceOffer,SpecPosition,PositionResult,Requisites
from engine.offer_selection import offer_is_comparable,select_top_offers


def fixture_visibility_attestation(evidence,path):
    """Explicit OFFLINE scene for unrelated policy tests; never real supplier proof."""
    from engine.test_capture_visibility import Page
    from engine.capture_visibility import attestation_path
    import atexit
    price=evidence['price_text']
    unit=price+'\n'+evidence['unit_text']
    basis=evidence['basis']
    if evidence['sale_unit']=='уп' and evidence['requested_unit']!='уп':
        basis=f"Упаковка {evidence['quantity']:g} {evidence['requested_unit']}"
    article='Артикул: '+evidence['article']
    snapshot=dict(url=evidence['url'],title=evidence['title'],
        text='\n'.join([evidence['title'],article,unit,basis]),
        heading=dict(x=20,y=200,width=600,height=30),
        prices=[dict(text=price,unit_context=unit,x=20,y=270,width=350,height=30)],
        fragments=[dict(text=article,x=20,y=240,width=500,height=25),
                   dict(text=unit,x=20,y=270,width=500,height=30),
                   dict(text=basis,x=20,y=310,width=500,height=30)])
    snapshot=fixture_purchase_snapshot(snapshot)
    proof,reason=capture_price(Page(snapshot),path,url=evidence['url'],
        listed=evidence['listed_price'],normalized=evidence['normalized_price'],
        sale_unit=evidence['sale_unit'],requested_unit=evidence['requested_unit'],
        title=evidence['title'],article=evidence['article'],
        expected_mark=evidence['article'],expected_name=evidence['title'])
    if not proof:raise AssertionError('Offline producer fixture refused: '+reason)
    evidence.clear();evidence.update(proof)
    atexit.register(lambda:attestation_path(path).unlink(missing_ok=True))
    return evidence


def fixture_capture(offer, *, expected_mark=None, expected_name=None):
    """Coherent synthetic current proof; pixels are test data, never supplier evidence."""
    d=offer if isinstance(offer,dict) else offer.model_dump()
    from engine.search.agent import _sale_unit
    from engine.search.product_identity import rendered_identity, card_host
    from engine.config import CACHE_DIR
    import atexit,base64,hashlib,time
    prior=d.get('identity_evidence') or {}
    mark=expected_mark or prior.get('expected_mark') or d.get('page_article') or 'FIXTURE1'
    name=expected_name or prior.get('expected_name') or 'Item'
    title=d.get('product_title') or name+' '+mark
    article=d.get('page_article') or prior.get('main_article') or mark
    sale=_sale_unit(d.get('price_unit') or 'шт')
    requested=_sale_unit(d.get('normalized_price_unit') or sale)
    listed=d.get('listed_price_with_vat')
    normalized=d['price_with_vat']
    quantity=listed/(normalized or 1)
    basis=f'Упаковка {quantity:g} {requested}' if sale=='уп' and sale!=requested else f'цена за 1 {sale}'
    stamp=time.time()-1
    pixels=base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/l9sAAAAASUVORK5CYII=')
    paths=[]
    for suffix in ('price','identity'):
        with tempfile.NamedTemporaryFile(dir=CACHE_DIR,prefix='test_capture_'+suffix+'_',suffix='.png',delete=False) as f:
            f.write(pixels);paths.append(Path(f.name))
        atexit.register(lambda path=paths[-1]:path.unlink(missing_ok=True))
    shot,identity_shot=paths
    identity=rendered_identity(d['url'],d['url'],title,article,mark,name,card_host(d['url']))
    if identity:
        identity.update(observed_at=stamp+.5,identity_screenshot=identity_shot.name,identity_sha256=hashlib.sha256(pixels).hexdigest())
    evidence=dict(version=1,provenance='stable_visible_main_card',url=d['url'],title=title,article=article,
        listed_price=listed,normalized_price=normalized,sale_unit=sale,requested_unit=requested,
        quantity=quantity,basis=basis,unit_text=f'{listed:g} ₽ / {sale}',price_text=f'{listed:g} ₽',
        screenshot=shot.name,sha256=hashlib.sha256(pixels).hexdigest(),captured_at=stamp)
    fixture_visibility_attestation(evidence,shot)
    if identity:identity['observed_at']=evidence['captured_at']+.001
    values=dict(price_capture_evidence=evidence,identity_evidence=identity,screenshot_product=str(shot),
                normalized_price_unit=d.get('normalized_price_unit') or requested)
    if isinstance(offer,dict):offer.update(values)
    else:
        for key,value in values.items():setattr(offer,key,value)
    return offer


def fixture_capture_result(page,path,**fields):
    """Parameter-correct synthetic capture used only to isolate unrelated policies."""
    snapshot=page.evaluate(DOM_SNAPSHOT) if hasattr(page,'evaluate') else None
    if isinstance(snapshot,dict):
        arguments={k:fields[k] for k in ('url','listed','normalized','sale_unit','requested_unit','title','article','expected_mark','expected_name') if k in fields}
        evidence,reason=qualify_snapshot(snapshot,**arguments)
        if reason:return {},reason
        import base64,hashlib,time,atexit
        pixels=base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/l9sAAAAASUVORK5CYII=')
        Path(path).write_bytes(pixels)
        evidence.update(provenance='stable_visible_main_card',screenshot=Path(path).name,
                        sha256=hashlib.sha256(pixels).hexdigest(),captured_at=time.time())
        fixture_visibility_attestation(evidence,path)
        atexit.register(lambda:Path(path).unlink(missing_ok=True))
        return evidence,''
    offer=fixture_capture(dict(url=fields['url'],product_title=fields['title'],page_article=fields.get('article',''),
        price_with_vat=fields['normalized'],listed_price_with_vat=fields['listed'],price_unit=fields['sale_unit'],
        normalized_price_unit=fields['requested_unit']),expected_mark=fields.get('expected_mark') or 'FIXTURE1',
        expected_name=fields.get('expected_name') or 'Item')
    Path(path).write_bytes(Path(offer['screenshot_product']).read_bytes())
    offer['price_capture_evidence']['screenshot']=Path(path).name
    fixture_visibility_attestation(offer['price_capture_evidence'],path)
    import atexit
    atexit.register(lambda:Path(path).unlink(missing_ok=True))
    return offer['price_capture_evidence'],''


class PriceCaptureTests(unittest.TestCase):
    def snapshot(self,text='Item ABC123\nАртикул: ABC123\n120 ₽ / уп\nУпаковка 12 м'):
        return fixture_purchase_snapshot(dict(url='https://seller.example/product/ABC123',title='Item ABC123',text=text,
            prices=[dict(text='120 ₽ / уп',x=0,y=80,width=150,height=30)],
            fragments=[dict(text='Упаковка 12 м',x=0,y=120,width=200,height=25),dict(text='Артикул: ABC123',x=0,y=45,width=180,height=25)],
            heading=dict(x=0,y=0,width=250,height=30)))

    def fields(self,**changes):
        return dict(dict(url='https://seller.example/product/ABC123',listed=120,normalized=10,
            sale_unit='упаковка',requested_unit='м',title='Item ABC123',article='ABC123',
            expected_mark='ABC123',expected_name='Item'),**changes)

    def test_explicit_pack12m_derivation_is_not_piece_or_kit(self):
        e,r=qualify_snapshot(self.snapshot(),**self.fields())
        self.assertEqual('',r);self.assertEqual(12,e['quantity']);self.assertEqual(10,e['normalized_price'])
        for changes in (dict(requested_unit='шт'),dict(requested_unit='компл'),dict(normalized=120),dict(normalized=11)):
            self.assertTrue(qualify_snapshot(self.snapshot(),**self.fields(**changes))[1])

    def test_only_price_currency_not_articles_quantities_or_metadata(self):
        self.assertEqual([20790.96],visible_prices('Артикул 3526410, 12 м, 20 790,96 ₽'))
        for snap in (dict(self.snapshot(),prices=[]),dict(self.snapshot(),text='Item ABC123\nАртикул: ABC123\nУпаковка 12 м'),
                     dict(self.snapshot(),text='Item ABC123\nРекомендуем\n120 ₽ / уп\nУпаковка 12 м')):
            self.assertTrue(qualify_snapshot(snap,**self.fields())[1])

    def test_wrong_card_or_heading_or_article_rejected(self):
        for changes in (dict(url='https://other.example/product/ABC123'),dict(title='Other'),
                        dict(text='Item ABC123\nАртикул: DEF456\n120 ₽ / уп\nУпаковка 12 м')):
            self.assertTrue(qualify_snapshot(dict(self.snapshot(),**changes),**self.fields())[1])

    def test_price_and_unit_must_share_purchase_block(self):
        snap=self.snapshot('Item ABC123\nАртикул: ABC123\n120 ₽ / шт\n240 ₽ / уп\nУпаковка 12 м')
        snap['prices'][0]['text']='120 ₽ / шт'
        self.assertTrue(qualify_snapshot(snap,**self.fields())[1])
        snap['prices'][0].update(text='120 ₽',unit_context='120 ₽\n240 ₽ / уп')
        self.assertTrue(qualify_snapshot(snap,**self.fields())[1])

    def test_literal_price_per_metre_is_not_an_inferred_pack_total(self):
        from engine.price_capture import visible_quote_fields
        snap=self.snapshot('Item ABC123\nАртикул: ABC123\n1732.58 ₽ / м\nУпаковка 12 м')
        snap['prices'][0]['text']='1732.58 ₽ / м'
        snap=fixture_purchase_snapshot(snap)
        fields=visible_quote_fields(snap,'м',url=snap['url'],title=snap['title'],expected_mark='ABC123')
        self.assertEqual(1732.58,fields['listed_price_with_vat']);self.assertEqual('м',fields['price_unit'])
        self.assertEqual(1,fields['package_quantity'])

    def test_order_multiple_cannot_establish_pack_basis(self):
        for text in ('Минимальный заказ 12 м','Кратность заказа 12 м','Упаковка 12 м\nУпаковка 24 м'):
            self.assertTrue(qualify_snapshot(self.snapshot('Item ABC123\nАртикул: ABC123\n120 ₽ / уп\n'+text),**self.fields())[1])

    def test_unit_must_label_price_not_stock_count_or_title(self):
        snap=self.snapshot('Item ABC123 (Упаковка 12 м)\nАртикул: ABC123\n120 ₽\nДоступно 12 упак')
        snap['prices'][0]['text']='120 ₽'
        self.assertTrue(qualify_snapshot(snap,**self.fields())[1])

    def test_explicit_lot_and_piece_length(self):
        snap=self.snapshot('Item ABC123\nАртикул: ABC123\n120 ₽ за 3 м')
        snap['prices'][0]['text']='120 ₽ за 3 м'
        snap=fixture_purchase_snapshot(snap)
        e,r=qualify_snapshot(snap,**self.fields(sale_unit='3 м',normalized=40))
        self.assertEqual('',r);self.assertEqual(3,e['quantity'])
        snap.update(title='Item ABC123 L3000',text='Item ABC123 L3000\nАртикул: ABC123\n120 ₽ / шт')
        snap['prices'][0]['text']='120 ₽ / шт'
        snap=fixture_purchase_snapshot(snap)
        self.assertEqual('',qualify_snapshot(snap,**self.fields(title=snap['title'],sale_unit='шт',normalized=40))[1])

    def test_stable_capture_real_file_and_postshot_change(self):
        from engine.test_capture_visibility import Page
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp)/'capture.png';snapshot=self.snapshot()
            # Fixture h1 must fit below the synthetic fixed header.
            snapshot['heading']['y']=200
            for f in snapshot['fragments']:f['y']+=200
            snapshot['prices'][0]['y']+=200
            page=Page(snapshot)
            e,r=capture_price(page,p,**self.fields())
            self.assertEqual('',r);self.assertEqual('capture.png',e['screenshot']);self.assertEqual(64,len(e['sha256']))
            class Changed(Page):
                def screenshot(self,**kw):
                    super().screenshot(**kw);self.snapshot['prices'][0]['text']='240 ₽ / уп'
            self.assertTrue(capture_price(Changed(snapshot),p,**self.fields())[1])

    def test_same_price_moved_outside_captured_frame_is_review(self):
        from engine.test_capture_visibility import Page
        snapshot=self.snapshot();snapshot['heading']['y']=200
        for f in snapshot['fragments']:f['y']+=200
        snapshot['prices'][0]['y']+=200
        class Moved(Page):
            def screenshot(self,**kw):
                super().screenshot(**kw);self.snapshot['prices'][0]['y']=1200
        with tempfile.TemporaryDirectory() as tmp:
            evidence,reason=capture_price(Moved(snapshot),Path(tmp)/'capture.png',**self.fields())
            self.assertFalse(evidence);self.assertIn('расположение',reason)

    def test_static_document_actual_url_not_requested_or_blank_metadata(self):
        from engine.price_capture import _snapshot
        page=MagicMock();page._kac_document_url=self.fields()['url']
        page.evaluate.return_value={**self.snapshot(),'url':'about:blank'}
        self.assertEqual(self.fields()['url'],_snapshot(page)['url'])
        page.evaluate.return_value={**self.snapshot(),'url':'https://other.example/card'}
        self.assertEqual({},_snapshot(page))

    def test_unreadable_capture_and_nonfinite_price_refused(self):
        page=MagicMock();page.evaluate.side_effect=ValueError('unavailable')
        self.assertTrue(capture_price(page,Path('unused.png'),**self.fields())[1])
        for n in (True,float('nan'),float('inf'),0,-1):
            self.assertTrue(qualify_snapshot(self.snapshot(),**self.fields(listed=n))[1])

    def test_malformed_evidence_is_review_without_exception(self):
        o=fixture_capture(PriceOffer(price_with_vat=100,url='https://seller.example/product',price_unit='шт',listed_price_with_vat=100))
        for key,value in [('captured_at','bad'),('captured_at',True),('listed_price','bad'),('quantity',False),('sha256',123)]:
            e={**o.price_capture_evidence,key:value}
            self.assertTrue(capture_review_reason(o.model_copy(update={'price_capture_evidence':e})))
        self.assertTrue(capture_review_reason(o.model_copy(update={'price_capture_evidence':['bad']})))

    def test_legacy_captured_price_requires_review_manual_price_kept(self):
        o=PriceOffer(price_with_vat=100,url='https://seller.example/product',price_unit='шт',listed_price_with_vat=100)
        self.assertTrue(capture_review_reason(o));self.assertFalse(offer_is_comparable(o))
        self.assertTrue(offer_is_comparable(o.model_copy(update={'listed_price_with_vat':None})))
        self.assertTrue(offer_is_comparable(o.model_copy(update={'from_letter':True})))

    def test_evidence_survives_api_draft_and_changes_never_green(self):
        from engine.app import _offer_to_dict,app
        from engine.schemas import PositionIn
        from fastapi.testclient import TestClient
        o=fixture_capture(PriceOffer(price_with_vat=100,url='https://seller.example/product',price_unit='шт',listed_price_with_vat=100))
        restored=PositionIn(number=1,name='Item',offers=[_offer_to_dict(o)]).to_result().offers[0]
        self.assertFalse(capture_review_reason(restored));self.assertEqual(o.price_capture_evidence,restored.price_capture_evidence)
        for changes in (dict(price_with_vat=200),dict(url='https://seller.example/other'),dict(price_unit='уп'),
                        dict(screenshot_product='other.png'),dict(price_capture_evidence={}),dict(price_capture_review_reason='Unreadable')):
            altered=o.model_copy(update=changes)
            self.assertTrue(capture_review_reason(altered));self.assertEqual([],select_top_offers([altered]))
            payload=dict(object_name='Test',skip_search=True,use_tsn=False,positions=[dict(number=1,name='Item',offers=[_offer_to_dict(altered)])])
            with patch('engine.app.run_pipeline_async') as pipeline:
                self.assertEqual(422,TestClient(app).post('/build',json=payload).status_code);pipeline.assert_not_called()

    def test_report_retains_review_price_identity_url_and_unit(self):
        from engine.builders.research_report_xlsx import build_research_report
        from engine.schemas import ResearchReportRequest
        from openpyxl import load_workbook
        from io import BytesIO
        o=PriceOffer(price_with_vat=10,url='https://seller.example/product/ABC123',price_unit='уп',listed_price_with_vat=120,
            price_capture_review_reason='Цена на снимке не читается',identity_evidence={'fixture':'retained'})
        from engine.app import _offer_to_dict
        request=ResearchReportRequest(object_name='Test',positions=[dict(number=1,name='Item',unit='м',offers=[_offer_to_dict(o)])])
        wb=load_workbook(BytesIO(build_research_report(request)))
        self.assertEqual(120,wb['Предложения']['M2'].value)
        self.assertIn('не читается',wb['Предложения']['O2'].value)
        self.assertEqual('Нет',wb['Предложения']['Q2'].value)
        self.assertNotEqual('00C6EFCE',wb['Предложения']['G2'].fill.fgColor.rgb)
        self.assertTrue(any('не читается' in str(row[11]) for row in wb['Позиции × сайты'].iter_rows(values_only=True)))


def fixture_card_page(page, url, *, title='Item FIXTURE1', article='FIXTURE1',
                      price=100, sale_unit='шт'):
    """Literal synthetic DOM + separately written PNGs; real identity validators run."""
    from engine.search.agent import _sale_unit
    import base64
    token=_sale_unit(sale_unit)
    text=f'{title}\nАртикул: {article}\n{price:g} ₽ / {token}'
    snapshot=dict(url=url,title=title,text=text,
        prices=[dict(text=f'{price:g} ₽ / {token}',unit_context=f'{price:g} ₽ / {token}',x=0,y=80,width=200,height=30)],
        heading=dict(x=0,y=0,width=300,height=30),viewport=dict(width=800,height=600))
    page.url=url;page._kac_document_url=url;page._kac_response_status=200
    page.title.return_value=title;page.inner_text.return_value=text
    page.locator.return_value.first.inner_text.return_value=title
    page.evaluate.return_value=snapshot
    def screenshot(*a,**kw):
        Path(kw['path']).write_bytes(base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/l9sAAAAASUVORK5CYII='))
    page.screenshot.side_effect=screenshot
    return snapshot
