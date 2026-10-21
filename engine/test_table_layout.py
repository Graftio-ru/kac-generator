"""Offline geometry controls are independent of product readings."""
import tempfile
import unittest
from pathlib import Path
from hashlib import sha256
from PIL import Image, ImageDraw
from unittest.mock import patch
from engine.table_layout import analyze_table, export_crops


class TableLayoutTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def grid(self, name='grid.png', xs=(40, 90, 220, 350), ys=(35, 65, 95, 125, 155), size=(420, 230)):
        image = Image.new('RGB', size, 'white'); draw = ImageDraw.Draw(image)
        for x in xs: draw.line((x, ys[0], x, ys[-1]), fill='black')
        for y in ys: draw.line((xs[0], y, xs[-1], y), fill='black')
        path = self.root / name; image.save(path); return path

    def test_changed_columns_are_geometry_not_fixed_field_names(self):
        for xs in ((40,90,220,350),(25,70,120,180,260,370)):
            layout = analyze_table(self.grid(xs=xs))
            self.assertEqual('geometry_detected', layout['status'])
            self.assertEqual(list(xs), layout['x_rules'])
            self.assertEqual(4, len(layout['rows']))
            self.assertEqual(4*(len(xs)-1), len(layout['cells']))
            self.assertEqual('unresolved', layout['field_mapping'])
            self.assertTrue(all(c['state']=='unread' for c in layout['cells']))

    def test_stable_ids_and_original_bytes_unchanged(self):
        path=self.grid(); before=path.read_bytes()
        a=analyze_table(path); b=analyze_table(path)
        self.assertEqual(a,b)
        records=export_crops(path,a,self.root/'crops',padding=3)
        self.assertEqual(before,path.read_bytes())
        self.assertEqual(sha256(before).hexdigest(),a['source_hash'])
        self.assertEqual(len(records),len(list((self.root/'crops').glob('*.png'))))
        self.assertEqual([1,0,37,0,1,32],records[0]['crop_to_original'])
        with Image.open(self.root/'crops'/records[0]['crop_file']) as crop:
            self.assertEqual(tuple(records[0]['crop_size']),crop.size)

    def test_explicit_rotation_maps_crop_corners_back_to_source(self):
        path=self.grid(); original=Image.open(path).convert('RGB')
        for rotation in (0,90,180,270):
            layout=analyze_table(path,rotation=rotation)
            self.assertEqual('geometry_detected',layout['status'])
            records=export_crops(path,layout,self.root/f'crops{rotation}',padding=0)
            record=records[0]; a,b,c,d,e,f=record['crop_to_original']
            w,h=record['crop_size']
            mapped=[(a*x+b*y+c,d*x+e*y+f) for x in (0,w) for y in (0,h)]
            bounds=[min(x for x,y in mapped),min(y for x,y in mapped),
                    max(x for x,y in mapped),max(y for x,y in mapped)]
            self.assertEqual(record['crop_original_bounds'],bounds)
            self.assertTrue(0<=bounds[0]<bounds[2]<=original.width)
            self.assertTrue(0<=bounds[1]<bounds[3]<=original.height)

    def test_missing_divider_is_not_a_certified_field_boundary(self):
        path=self.grid()
        with Image.open(path) as source: image=source.convert('RGB')
        ImageDraw.Draw(image).line((90,67,90,93),fill='white',width=3); image.save(path)
        result=analyze_table(path)
        merged=[c for c in result['cells'] if c['column_span']>1]
        self.assertEqual(1,len(merged))
        self.assertEqual('merged_or_missing_divider',merged[0]['geometry_issue'])
        self.assertEqual('unread',merged[0]['state'])

    def test_borderless_layout_is_explicitly_unsupported(self):
        path=self.root/'blank.png'; Image.new('RGB',(420,230),'white').save(path)
        result=analyze_table(path)
        self.assertEqual('unsupported',result['status'])
        self.assertEqual('ruled_table_not_found',result['code'])
        with self.assertRaises(ValueError): export_crops(path,result,self.root/'bad')

    def test_additional_table_region_is_retained_as_unclassified(self):
        path=self.grid(size=(420,430))
        with Image.open(path) as source: image=source.convert('RGB')
        draw=ImageDraw.Draw(image)
        for x in (40,90,220,350):draw.line((x,250,x,340),fill='black')
        for y in (250,280,310,340):draw.line((40,y,350,y),fill='black')
        image.save(path); result=analyze_table(path)
        self.assertIn('additional_ruled_regions_require_mapping',result['issues'])
        self.assertEqual(1,len(result['additional_regions']))

    def test_side_by_side_tables_are_not_silently_dropped(self):
        path=self.root/'side-by-side.png'
        image=Image.new('RGB',(500,200),'white'); draw=ImageDraw.Draw(image)
        for xs in ((20,70,140,220),(270,320,390,470)):
            for x in xs: draw.line((x,30,x,150),fill='black')
            for y in (30,60,90,120,150): draw.line((xs[0],y,xs[-1],y),fill='black')
        image.save(path); result=analyze_table(path)
        self.assertIn('additional_ruled_regions_require_mapping',result['issues'])
        self.assertEqual(1,len(result['additional_regions']))
        self.assertGreater(result['additional_regions'][0]['bounds'][0],result['table_bounds'][2])

    def test_changed_source_cannot_reuse_layout(self):
        path=self.grid(); layout=analyze_table(path)
        path.write_bytes(b'changed')
        with self.assertRaisesRegex(ValueError,'source changed'):
            export_crops(path,layout,self.root/'crops')

    def test_analysis_uses_the_hashed_snapshot_even_if_path_changes(self):
        path=self.grid(); snapshot=path.read_bytes()
        def changed():
            Image.new('RGB',(420,230),'red').save(path)
            return snapshot
        with patch.object(Path,'read_bytes',side_effect=changed) as read:
            result=analyze_table(path)
        self.assertEqual(1,read.call_count)
        self.assertEqual('geometry_detected',result['status'])
        self.assertEqual(sha256(snapshot).hexdigest(),result['source_hash'])

    def test_crop_export_uses_same_snapshot_as_its_hash_guard(self):
        path=self.grid(); layout=analyze_table(path); snapshot=path.read_bytes()
        def changed():
            Image.new('RGB',(420,230),'red').save(path)
            return snapshot
        with patch.object(Path,'read_bytes',side_effect=changed) as read:
            records=export_crops(path,layout,self.root/'frozen',padding=0)
        self.assertEqual(1,read.call_count)
        with Image.open(self.root/'frozen'/records[0]['crop_file']) as crop:
            self.assertEqual((0,0,0),crop.getpixel((0,0)))

    def test_export_does_not_inherit_exif_rotation(self):
        path=self.grid()
        with Image.open(path) as source: image=source.convert('RGB')
        exif=Image.Exif(); exif[274]=6; image.save(path,exif=exif)
        layout=analyze_table(path)
        self.assertEqual(6,layout['source_exif_orientation'])
        records=export_crops(path,layout,self.root/'exif')
        with Image.open(self.root/'exif'/records[0]['crop_file']) as crop:
            self.assertNotIn(274,crop.getexif())

    def test_scaled_grid_preserves_cell_count(self):
        path=self.grid()
        for scale in (.6,1.5,2):
            with Image.open(path) as source:
                scaled=source.resize((round(source.width*scale),round(source.height*scale)))
                target=self.root/f'scale{scale}.png';scaled.save(target)
            layout=analyze_table(target)
            self.assertEqual('geometry_detected',layout['status'])
            self.assertEqual(12,len(layout['cells']))

if __name__=='__main__':unittest.main()
