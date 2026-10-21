"""Offline checks for geometry-aware OCR serialization."""
from __future__ import annotations

import unittest
from unittest.mock import patch

from engine.parsers.spec_pdf import (
    _is_graftio_equipment,
    _raw_positions,
    _merge_vision_annotations,
    _vision_annotations_with_narrow_columns,
    _structure_positions,
    _reconcile_layout_cells,
    _vision_layout_text,
    parse_spec,
)
from engine.llm import ModelOutputInvalid


class VisionLayoutTests(unittest.TestCase):
    def test_numbered_row_with_unreadable_name_is_retained_and_bad_quantity_not_guessed(self):
        with patch('engine.parsers.spec_pdf._raw_positions',return_value=[
            {'number':12,'name':None,'unit':'шт','qty':'нечитаемо'}]):
            row=parse_spec('input.png')[0]
        self.assertEqual(12,row.number)
        self.assertEqual('',row.name)
        self.assertEqual(0,row.qty)
        with patch('engine.parsers.spec_pdf._raw_positions',return_value=[
            {'number':42,'name':None,'unit':None,'type_mark':None,'qty':None},
            {'name':'Раздел оборудования','unit':None,'qty':None}]):
            rows=parse_spec('input.png')
        self.assertEqual([42],[p.number for p in rows])
        from engine.source_review import recognition_fields
        self.assertEqual({'name','unit','qty'},set(recognition_fields(rows[0])))
    def test_colored_overprint_creates_field_visibility_issue_without_changing_code(self):
        from PIL import Image, ImageDraw
        from engine.parsers.spec_pdf import _source_layout_text, _reconcile_layout_cells
        image=Image.new('RGB',(1000,1000),'white')
        annotations=[('Позиция',1,[.08,.92,.03,.01]),('Единица',1,[.76,.92,.03,.01]),
                     ('Кол-во',1,[.805,.92,.03,.01]),('1',1,[.095,.70,.01,.01])]
        ImageDraw.Draw(image).line((550,295,620,295),fill='blue',width=2)
        text=_source_layout_text(image,annotations)
        row=_reconcile_layout_cells([{'number':1,'type_mark':'1234','qty':2}],text)[0]
        self.assertIn('type_mark',row['review_fields'])
        self.assertEqual('1234',row['type_mark'])
        clear=_source_layout_text(Image.new('RGB',(1000,1000),'white'),annotations)
        self.assertNotIn('source_warning',clear)
    def test_independent_type_readings_flag_glyph_and_prefix_disagreement(self):
        from engine.parsers.spec_pdf import _reconcile_layout_cells
        text=('y=0.92 | x=0.08:Позиция | x=0.76:Единица | x=0.805:Кол-во\n'
              'y=0.70 | x=0.095:1 | x=0.45:MODEL-D048 | x=0.46:MODEL-0048 | x=0.817:1\n'
              'y=0.67 | x=0.095:2 | x=0.45:ABC123 | x=0.817:2\n'
              'y=0.64 | x=0.095:3 | x=0.45:ABC 123 | x=0.46:ABC123 | x=0.817:3')
        result=_reconcile_layout_cells([{'number':1,'type_mark':'MODEL-0048','qty':1},
                                       {'number':2,'type_mark':'1ABC123','qty':2},
                                       {'number':3,'type_mark':'ABC123','qty':3}],text)
        self.assertIn('Разные чтения марки',result[0]['review_issue'])
        self.assertEqual('MODEL-0048',result[0]['type_mark'])
        self.assertIn('не совпадает',result[1]['review_issue'])
        self.assertFalse(result[2]['review_issue'])
    def test_layout_cells_recover_missing_qty_without_crossing_neighbor_rows(self):
        header = "y=0.92 | x=0.08:Позиция | x=0.76:Единица | x=0.805:Кол-во\n"
        text = header + "\n".join([
            "y=0.70 | x=0.095:29 | x=0.772:М | x=0.817:14",
            "y=0.67 | x=0.095:30 | x=0.772:М | x=0.817:2",
            "y=0.64 | x=0.095:31 | x=0.772:М",
        ])
        raw = [{"number": 29, "qty": None, "unit": ""},
               {"number": 30, "qty": 14, "unit": "м"},
               {"number": 31, "qty": 1, "unit": "м"}]
        result = _reconcile_layout_cells(raw, text)
        self.assertEqual([14, 2, 0], [p["qty"] for p in result])
        self.assertEqual("м", result[0]["unit"])
        self.assertIn("не прочитано", result[2]["review_issue"])

    def test_layout_conflicts_are_not_resolved_by_guessing(self):
        text = "y=0.92 | x=0.08:Позиция | x=0.76:Единица | x=0.805:Кол-во\n" + "\n".join([
            "y=0.70 | x=0.095:1 | x=0.817:14 | x=0.819:16",
            "y=0.67 | x=0.095:2 | x=0.817:2",
            "y=0.64 | x=0.095:3 | x=0.817:1",
        ])
        result = _reconcile_layout_cells([{"number": 1, "qty": 14}], text)
        self.assertEqual(0, result[0]["qty"])
        self.assertIn("Противоречивые", result[0]["review_issue"])
        self.assertEqual([{"number": 1, "qty": 14}], _reconcile_layout_cells(
            [{"number": 1, "qty": 14}], text.replace("x=0.805:Кол-во", "x=0.50:Кол-во")))
        zero_conflict = text.replace("x=0.817:14 | x=0.819:16", "x=0.817:0 | x=0.819:1")
        self.assertEqual(0, _reconcile_layout_cells([{"number": 1, "qty": 1}], zero_conflict)[0]["qty"])
        repeated = text + "\ny=0.60 | x=0.095:1 | x=0.817:14"
        row = _reconcile_layout_cells([{"number": 1, "qty": 14}], repeated)[0]
        self.assertEqual(0, row["qty"])
        self.assertIn("Несколько", row["review_issue"])
    def test_article_cell_recovery_and_conflicts_do_not_guess_characters(self):
        text = "y=0.92 | x=0.08:Позиция | x=0.76:Единица | x=0.805:Кол-во\n" + "\n".join([
            "y=0.70 | x=0.095:1 | x=0.58:SSC 90500K | x=0.817:14",
            "y=0.67 | x=0.095:2 | x=0.58:CM201001INOX | x=0.59:CM2010011N0X | x=0.817:2",
            "y=0.64 | x=0.095:3 | x=0.58:3526210 | x=0.817:1",
        ])
        raw = [{"number": 1, "type_mark": ""},
               {"number": 2, "type_mark": "CM201001INOX"},
               {"number": 3, "type_mark": "3526310"}]
        result = _reconcile_layout_cells(raw, text)
        self.assertEqual("SSC90500K", result[0]["type_mark"])
        self.assertEqual("CM201001INOX", result[1]["type_mark"])
        self.assertIn("Противоречивые варианты артикула", result[1]["review_issue"])
        self.assertEqual("3526310", result[2]["type_mark"])
        self.assertIn("отличается", result[2]["review_issue"])
        for mark in ["13526210", "3526210ZL"]:
            row = _reconcile_layout_cells([{"number": 3, "type_mark": mark}], text)[0]
            self.assertIn("отличается", row["review_issue"])
        row = _reconcile_layout_cells([{"number": 3, "type_mark": "DKC (3526210)"}], text)[0]
        self.assertNotIn("отличается", row["review_issue"])

    def test_null_fields_remain_missing_instead_of_literal_none(self):
        with patch("engine.parsers.spec_pdf._raw_positions", return_value=[{
            "number": 1, "name": "Кабель", "type_mark": None,
            "unit": None, "qty": 10, "maker": None, "review_issue": None}]):
            position = parse_spec("sample.png")[0]
        self.assertEqual("", position.unit)
        self.assertEqual("", position.type_mark)
        self.assertEqual("", position.review_issue)

    def test_narrow_unit_pass_recovers_a_missing_unit(self) -> None:
        from PIL import Image

        calls = iter([
            [("21", 1.0, [0.095, 0.85, 0.02, 0.01])],
            [],
            [("м", 1.0, [0.5, 0.85, 0.1, 0.01])],
            [],
            [],
            [],
            [],
            [],
            [],
            [],
        ])

        class FakeOCR:
            def __init__(self, *_args, **_kwargs):
                pass

            def recognize(self):
                return next(calls)

        annotations = _vision_annotations_with_narrow_columns(
            Image.new("RGB", (1000, 1000)), FakeOCR
        )
        self.assertEqual(["21", "м"], [item[0] for item in annotations])
        self.assertAlmostEqual(0.76, annotations[1][2][0], places=2)

    def test_graftio_equipment_is_not_searched_even_with_ocr_maker_errors(self) -> None:
        examples = [
            ("Зарядная станция Тип-2", "PEVC2201E", "Гратио"),
            ("Зарядная станция Тип-3", "PEV(3106(E)", ""),
            ("Щит распределительный", "ЩС-ЗУ", "Гфтио"),
            ("Вторичный распределительный узел (2 линии)", "ВРУ-395", ""),
            ("Монтажный комплект для установки зарядной станции Тип 2", "", ""),
        ]
        self.assertTrue(all(_is_graftio_equipment(*item) for item in examples))
        self.assertFalse(_is_graftio_equipment(
            "Перегородка SEP L3000", "36480", "DKC"
        ))
        self.assertFalse(_is_graftio_equipment(
            "Вторичный распределительный узел", "Другая марка", "Другой завод"
        ))

    def test_groups_same_row_and_orders_cells_by_x(self) -> None:
        annotations = [
            ("14", 1.0, [0.85, 0.70, 0.02, 0.02]),
            ("3526210", 1.0, [0.55, 0.70, 0.08, 0.02]),
            ("23", 1.0, [0.08, 0.70, 0.02, 0.02]),
            ("Лоток 100х50", 1.0, [0.15, 0.70, 0.25, 0.02]),
        ]

        result = _vision_layout_text(annotations)

        self.assertEqual(1, len(result.splitlines()))
        self.assertLess(result.index("23"), result.index("Лоток 100х50"))
        self.assertLess(result.index("Лоток 100х50"), result.index("3526210"))
        self.assertLess(result.index("3526210"), result.index("14"))

    def test_keeps_neighboring_rows_separate(self) -> None:
        annotations = [
            ("23", 1.0, [0.08, 0.70, 0.02, 0.02]),
            ("24", 1.0, [0.08, 0.675, 0.02, 0.02]),
        ]

        result = _vision_layout_text(annotations)

        self.assertEqual(2, len(result.splitlines()))

    def test_column_pass_fills_missing_cell_without_duplicating_existing_cell(self) -> None:
        base = [("23", 1.0, [0.08, 0.70, 0.02, 0.02])]
        additions = [
            ("23", 0.5, [0.081, 0.701, 0.02, 0.02]),
            ("14", 0.5, [0.81, 0.70, 0.02, 0.02]),
        ]

        merged = _merge_vision_annotations(base, additions)

        self.assertEqual(2, len(merged))
        self.assertEqual(["23", "14"], [item[0] for item in merged])

    def test_parser_ignores_non_object_llm_fragments(self) -> None:
        raw = [
            "случайный фрагмент",
            {"number": 1, "name": "Лоток", "type_mark": "3526210", "unit": "м", "qty": 2},
        ]
        with patch("engine.parsers.spec_pdf._raw_positions", return_value=raw):
            positions = parse_spec("fixture.pdf")

        self.assertEqual(1, len(positions))
        self.assertEqual("3526210", positions[0].type_mark)

    def test_missing_unit_stays_visible_for_manual_review(self) -> None:
        raw = [{"number": 23, "name": "Лоток", "type_mark": "3526210", "unit": "", "qty": 14}]
        with patch("engine.parsers.spec_pdf._raw_positions", return_value=raw):
            positions = parse_spec("fixture.jpg")

        self.assertEqual("", positions[0].unit)


class StructuredResponseTests(unittest.TestCase):
    def test_deepseek_image_spec_sends_pixels_and_independent_ocr(self) -> None:
        raw = [{"number": 1, "name": "Лоток", "unit": "м", "qty": 14}]
        with patch("engine.parsers.spec_pdf.ocr_available", return_value=True), \
             patch("engine.parsers.spec_pdf.ocr_image", return_value="позиция 1"), \
             patch("engine.parsers.spec_pdf.settings.llm_provider", "deepseek"), \
             patch("engine.parsers.spec_pdf._visual_positions", return_value=raw) as structure:
            self.assertEqual(raw, _raw_positions("page-37.jpg"))
        structure.assert_called_once_with("позиция 1", ['page-37.jpg'])

    def test_deepseek_image_spec_works_without_local_ocr(self) -> None:
        with patch("engine.parsers.spec_pdf.ocr_available", return_value=True), \
             patch("engine.parsers.spec_pdf.ocr_image", return_value=""), \
             patch("engine.parsers.spec_pdf.settings.llm_provider", "deepseek"), \
             patch("engine.parsers.spec_pdf._visual_positions", return_value=[]) as structure:
            self.assertEqual([], _raw_positions("page-37.png"))
        structure.assert_called_once_with('', ['page-37.png'])

    def test_overlapping_views_are_temporary_and_keep_original_context(self):
        from PIL import Image
        from pathlib import Path
        from tempfile import TemporaryDirectory
        from engine.parsers.spec_pdf import _visual_positions
        saved=[]
        def structure(*,text,images):
            self.assertEqual('independent OCR',text)
            self.assertEqual(5,len(images))
            self.assertEqual(original,images[0])
            for path in images[1:]:
                with Image.open(path) as tile:
                    self.assertEqual((1100,880),tile.size)
                saved.append(path)
            return []
        with TemporaryDirectory() as directory:
            original=str(Path(directory)/'input.png')
            Image.new('RGB',(1000,800),'white').save(original)
            with patch('engine.parsers.spec_pdf._structure_positions',side_effect=structure):
                self.assertEqual([],_visual_positions('independent OCR',[original]))
        self.assertFalse(any(Path(p).exists() for p in saved))

    def test_visual_readings_are_preserved_and_ocr_conflicts_stay_pending(self):
        text = ('y=0.92 | x=0.08:Позиция | x=0.76:Единица | x=0.805:Кол-во\n'
                'y=0.70 | x=0.095:1 | x=0.45:MODEL-0048 | x=0.817:160 | x=0.818:460\n'
                'y=0.67 | x=0.095:2 | x=0.817:2\n'
                'y=0.64 | x=0.095:3 | x=0.817:3')
        raw=[{'number':1,'name':'Кабель','type_mark':'MODEL-D048','unit':'м','qty':460}]
        with patch('engine.parsers.spec_pdf.complete_json',return_value={'positions':raw}) as llm:
            rows=_structure_positions(text=text,images=['image.png'])
        self.assertNotIn('MODEL-0048',llm.call_args.args[0])
        self.assertEqual(460,rows[0]['qty'])
        self.assertEqual('MODEL-D048',rows[0]['type_mark'])
        self.assertEqual('vision',rows[0]['_source_reader'])
        self.assertIn('Противоречивые',rows[0]['review_issue'])
        self.assertIn('не совпадает',rows[0]['review_issue'])

    def test_visual_unit_conflict_and_omitted_mark_are_explicit(self):
        from engine.parsers.spec_pdf import _compare_visual_cells
        text=('y=0.92 | x=0.08:Позиция | x=0.76:Единица | x=0.805:Кол-во\n'
              'y=0.70 | x=0.095:1 | x=0.45:MODEL-X | x=0.76:м | x=0.817:1\n'
              'y=0.67 | x=0.095:2 | x=0.817:2\n'
              'y=0.64 | x=0.095:3 | x=0.817:3')
        row=_compare_visual_cells([{'number':1,'type_mark':'','unit':'шт','qty':1}],text)[0]
        self.assertEqual('шт',row['unit'])
        self.assertEqual('',row['type_mark'])
        self.assertIn('Единица',row['review_issue'])
        self.assertIn('Марка видна',row['review_issue'])

    def test_source_maker_keeps_graftio_excluded_when_visual_maker_is_missing(self):
        from engine.parsers.spec_pdf import _compare_visual_cells
        text=('y=0.92 | x=0.08:Позиция | x=0.76:Единица | x=0.805:Кол-во\n'
              'y=0.70 | x=0.095:1 | x=0.66:Графтио | x=0.76:шт | x=0.817:1\n'
              'y=0.67 | x=0.095:2 | x=0.817:2\n'
              'y=0.64 | x=0.095:3 | x=0.817:3')
        raw=_compare_visual_cells([{'number':1,'name':'Щит','unit':'шт','qty':1}],text)
        with patch('engine.parsers.spec_pdf._raw_positions',return_value=raw):
            self.assertTrue(parse_spec('input.png')[0].is_unique)

    def test_accepts_deepseek_object_wrapper(self) -> None:
        raw = [{"number": 1, "name": "Кабель", "unit": "м", "qty": 10}]
        with patch("engine.parsers.spec_pdf.complete_json", return_value={"positions": raw}):
            self.assertEqual(raw, _structure_positions(text="spec"))

    def test_keeps_legacy_root_array_compatibility(self) -> None:
        raw = [{"number": 1, "name": "Кабель", "unit": "м", "qty": 10}]
        with patch("engine.parsers.spec_pdf.complete_json", return_value=raw):
            self.assertEqual(raw, _structure_positions(text="spec"))

    def test_rejects_unknown_object_instead_of_returning_empty_positions(self) -> None:
        with patch("engine.parsers.spec_pdf.complete_json", return_value={"type": "json_object"}):
            with self.assertRaisesRegex(ModelOutputInvalid, "invalid_rows"):
                _structure_positions(text="spec")


if __name__ == "__main__":
    unittest.main()
