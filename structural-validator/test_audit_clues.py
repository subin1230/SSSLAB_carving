import unittest
try:
    import numpy
except ImportError:
    numpy = None


@unittest.skipIf(numpy is None,'requires numpy')
class ClueTests(unittest.TestCase):
    def test_categories_are_not_violation_labels(self):
        from audit_clues import describe
        from validator_v4 import validate_block
        from test_validator_v2 import chunk
        cases=[(b'nothing','png','no_recognized_candidate'),
               (b'PK\x03\x04xx','zip','fixed_fields_incomplete'),
               (b'<ICCProfile>sRGB IEC6196','png','declared_extent_outside_block'),
               (chunk(b'IEND',b''),'png','complete_but_unanchored'),
               (chunk(b'pHYs',b'\0'*9),'png','boundary_supported')]
        for data,fmt,expected in cases:
            category, candidates=describe(data,fmt,validate_block(data,fmt))
            self.assertEqual(category,expected)

    def test_full_audit_counts_and_examples(self):
        from audit_clues import audit
        from validator_v4 import validate_block
        import tempfile,json
        from pathlib import Path
        import numpy as np
        with tempfile.TemporaryDirectory() as tmp:
            run=Path(tmp)/'run';run.mkdir()
            data=b'no structure'.ljust(512,b'\0')
            np.savez(run/'data.npz',x=np.array([list(data)],dtype=np.uint8),y=np.array([16],dtype=np.uint8))
            (run/'summary.json').write_text(json.dumps(dict(validator_version='v4',inputs={'npz':str(run/'data.npz')},total_rows=1,block_size=512,inspected_counts={'inspected':1})))
            record=dict(row=0,block_id='tess:0',metadata={'label':'16'},validation_format='png',ground_truth_type='png',predicted_type='png',results=validate_block(data,'png'))
            (run/'evidence.jsonl').write_text(json.dumps(record)+'\n')
            output=Path(tmp)/'audit'
            result=audit(run,output)
            self.assertEqual(result['block_categories'],{'png | no_recognized_candidate':1})
            self.assertEqual(len((output/'examples.jsonl').read_text().splitlines()),1)
            self.assertTrue((output/'report.md').exists())
