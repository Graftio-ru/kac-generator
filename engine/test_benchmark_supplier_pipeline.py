import json
import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from engine.benchmark_supplier_pipeline import main


class BenchmarkReportTests(unittest.TestCase):
    def test_multiple_snapshots_retain_sources_and_replace_same_seller(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            raw = json.dumps({'cases': [{'number':23}]}).encode()
            (root/'controls.json').write_bytes(raw)
            digest = hashlib.sha256(raw).hexdigest()
            for name, candidates in [('first', [{'inn':'1','price_with_vat':100,'url':'https://a.ru/item'}]),
                                     ('later', [{'inn':'1','price_with_vat':200,'url':'https://a.ru/item'},
                                                {'inn':'2','price_with_vat':150,'url':'https://b.ru/item'}])]:
                (root/f'{name}.json').write_text(json.dumps({'control_sha256':digest,'stage':'end-to-end',
                                                           'rows':[{'number':23,'candidates':candidates}]}))
            argv = ['benchmark', '--controls',str(root/'controls.json'), '--report',str(root/'out.json'),
                    '--stage','end-to-end','--resume-from',str(root/'first.json'),
                    '--resume-from',str(root/'later.json')]
            with patch('sys.argv',argv), patch('engine.benchmark_supplier_pipeline.run_case',
                    return_value={'number':23,'status':'measured','seconds':0}) as run:
                main()
            merged = run.call_args.args[3]['_resume_rows'][23]['candidates']
            self.assertEqual({o['inn']:o['price_with_vat'] for o in merged}, {'1':200,'2':150})

    def invoke(self, root, numbers='', failure=None):
        controls = root / 'controls.json'
        controls.write_text(json.dumps({'cases': [{'number': 23}]}))
        report = root / 'report.json'
        argv = ['benchmark', '--controls', str(controls), '--report', str(report),
                '--stage', 'discovery', '--numbers', numbers]
        with patch('sys.argv', argv), patch(
                'engine.benchmark_supplier_pipeline.run_case', side_effect=failure,
                return_value={'number': 23, 'status': 'measured', 'seconds': 0}):
            main()
        return report

    def test_unknown_selection_does_not_create_success_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with self.assertRaises(SystemExit) as caught:
                self.invoke(root, numbers='999')
            self.assertEqual(caught.exception.code, 2)
            self.assertFalse((root / 'report.json').exists())

    def test_interruption_is_saved_as_terminal_status(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with self.assertRaises(KeyboardInterrupt):
                self.invoke(root, failure=KeyboardInterrupt())
            report = json.loads((root / 'report.json').read_text())
            self.assertEqual(report['status'], 'interrupted')

    def test_previous_report_is_preserved(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.invoke(root)
            before = (root / 'report.json').read_bytes()
            with self.assertRaises(SystemExit):
                self.invoke(root)
            self.assertEqual((root / 'report.json').read_bytes(), before)
