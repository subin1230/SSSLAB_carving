import unittest
from evaluate_android_chunks import detect
from test_validator_v2 import chunk


class AndroidChunkTests(unittest.TestCase):
    def test_fixed_layouts(self):
        for kind, size in [(b'npOl',24),(b'npLb',16)]:
            self.assertTrue(detect(chunk(kind,b'\0'*size))[0]['layout_matches'])

    def test_nptc_count_size_relation(self):
        payload = bytes([0,2,2,9])+b'\0'*(32+4*13-4)
        self.assertTrue(detect(chunk(b'npTc',payload))[0]['layout_matches'])
        self.assertFalse(detect(chunk(b'npTc',payload[:-4]))[0]['layout_matches'])

    def test_corruption_truncation_and_text(self):
        data=chunk(b'npOl',b'\0'*24)
        self.assertEqual(detect(data[:-1]),[])
        damaged=bytearray(data);damaged[8]^=1
        self.assertEqual(detect(damaged),[])
        self.assertEqual(detect(b'plain npTc npOl npLb text'),[])

    def test_multiple_chunks_not_assumed_multiple_blocks(self):
        self.assertEqual(len(detect(chunk(b'npOl',b'\0'*24)+chunk(b'npLb',b'\0'*16))),2)

    def test_hypothetical_recovery_and_harm_are_separate(self):
        import csv,json,tempfile
        from pathlib import Path
        import numpy as np
        from evaluate_android_chunks import evaluate
        with tempfile.TemporaryDirectory() as temp:
            run=Path(temp)/'run';run.mkdir()
            data=chunk(b'npOl',b'\0'*24).ljust(512,b'\0')
            np.savez(run/'data.npz',x=np.array([list(data)]*2,dtype=np.uint8),y=np.array([0,1]))
            metas=[dict(row=i,block_id=str(i),source_file_id='same',label=i,ground_truth_type=t) for i,t in enumerate(['apk','png'])]
            blocks=[dict(row=i,block_id=str(i),ground_truth_type=m['ground_truth_type'],predicted_type='png') for i,m in enumerate(metas)]
            for name,rows in [('meta.csv',metas),('blocks.csv',blocks)]:
                with (run/name).open('w',newline='',encoding='utf-8') as f:
                    w=csv.DictWriter(f,fieldnames=list(rows[0]));w.writeheader();w.writerows(rows)
            (run/'summary.json').write_text(json.dumps(dict(selection_mode='all-formats',limit=0,total_rows=2,block_size=512,inputs={'npz':str(run/'data.npz'),'metadata':str(run/'meta.csv')})),encoding='utf-8')
            result=evaluate(run,Path(temp)/'out')
            self.assertEqual(result['counts']['ffc_wrong_apk_recovered_if_overridden'],1)
            self.assertEqual(result['counts']['ffc_correct_non_apk_harmed_if_overridden'],1)
            self.assertEqual(result['counts']['accuracy_delta_if_overridden'],0)
            self.assertEqual(result['candidate_precision'],0.5)
