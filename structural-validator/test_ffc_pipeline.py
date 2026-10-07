import csv
import json
from pathlib import Path
import tempfile
import unittest
try:
    import numpy as np
except ImportError:
    np = None


@unittest.skipIf(np is None, 'pipeline tests require numpy')
class PipelineModeTests(unittest.TestCase):
    def test_selection_and_prediction_are_kept_separate(self):
        from ffc_pipeline import collect
        from demo import examples
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            data = examples()[0][2].ljust(512, b'\0')
            x = np.array([list(data)] * 3, dtype=np.uint8)
            np.savez(root/'data.npz', x=x, y=np.array([16,59,16], dtype=np.uint8))
            metas = [dict(row=i, block_id=f'tess:{i}', source_file_id=f'file{i}', ground_truth_type=t, label=l)
                     for i,(t,l) in enumerate([('png',16),('json',59),('png',16)])]
            preds = [dict(block_id=m['block_id'], source_file_id=m['source_file_id'], ground_truth_type=m['ground_truth_type'], predicted_type=p)
                     for m,p in zip(metas,['jpg','png','png'])]
            for name, rows in [('meta.csv',metas),('pred.csv',preds)]:
                with (root/name).open('w',newline='') as f:
                    w=csv.DictWriter(f,fieldnames=list(rows[0]));w.writeheader();w.writerows(rows)
            def run(mode):
                return collect(root/'data.npz',root/'meta.csv',root/'pred.csv',root/mode,0,'v2',mode)
            for mode, expected_ids in [('prediction',{'tess:1','tess:2'}),('ground-truth',{'tess:0','tess:2'})]:
                s=run(mode)
                self.assertEqual(s['inspected_counts']['ffc_correct'],1)
                self.assertEqual(s['inspected_counts']['ffc_wrong'],1)
                self.assertEqual(s['uses_ground_truth_for_validation'],mode=='ground-truth')
                records=[json.loads(l) for l in (root/mode/'evidence.jsonl').read_text().splitlines()]
                self.assertEqual({r['block_id'] for r in records},expected_ids)
                self.assertTrue(all(r['validation_format']=='png' for r in records))
                if mode=='ground-truth':
                    self.assertEqual(records[0]['predicted_type'],'jpg')
                    self.assertFalse(records[0]['ffc_correct'])
                    self.assertIn(('CHUNK_CRC','VALID'),[(r['rule'],r['status']) for r in records[0]['results']])
            v3 = collect(root/'data.npz',root/'meta.csv',root/'pred.csv',root/'v3',0,'v3','ground-truth')
            self.assertEqual(v3['validator_version'], 'v3')
            self.assertIn('evidence_counts', v3)
            self.assertEqual(v3['inspected_counts']['boundary_supported_invalid_blocks'], 0)
            # ID mismatch must still stop diagnostic mode before output creation.
            preds[0]['block_id']='wrong'
            with (root/'pred.csv').open('w',newline='') as f:
                w=csv.DictWriter(f,fieldnames=list(preds[0]));w.writeheader();w.writerows(preds)
            with self.assertRaises(ValueError):
                collect(root/'data.npz',root/'meta.csv',root/'pred.csv',root/'bad',0,'v2','ground-truth')
            self.assertFalse((root/'bad').exists())
