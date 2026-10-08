import io
import json
from pathlib import Path
import tempfile
import unittest
import zipfile
import numpy as np
from analyze_supported_structures import inspect, analyze
from validator_v5 import validate_block
from test_validator_v2 import chunk


class StructureAnalysisTests(unittest.TestCase):
    def test_png_keyword_and_crc(self):
        data = chunk(b'tEXt', b'Software\0example')
        item = inspect(data, 'png', validate_block(data, 'png'))[0]
        self.assertEqual(item['keyword'], 'Software')
        self.assertEqual(item['text_preview'], 'example')
        self.assertTrue(item['crc_matches'])
        truncated = inspect(data[:-1], 'png', validate_block(data[:-1], 'png'))[0]
        self.assertFalse(truncated['complete'])
        self.assertNotIn('keyword', truncated)

    def test_zip_filename_extraction(self):
        out = io.BytesIO()
        with zipfile.ZipFile(out, 'w') as z:
            z.writestr('META-INF/MANIFEST.MF', b'hello')
        data = out.getvalue()
        items = inspect(data, 'zip', validate_block(data, 'zip'))
        self.assertTrue(any(i.get('filename') == 'META-INF/MANIFEST.MF' and i['supported'] for i in items))

    def test_label_is_stratification_not_classification(self):
        with tempfile.TemporaryDirectory() as temp:
            run = Path(temp)/'run'; run.mkdir()
            data = chunk(b'tEXt', b'Software\0example').ljust(512, b'\0')
            np.savez(run/'data.npz', x=np.array([list(data)], dtype=np.uint8), y=np.array([1]))
            summary = dict(selection_mode='all-formats', total_rows=1, block_size=512,
                           inputs={'npz': str(run/'data.npz')}, inspected_counts={'candidate_blocks':1, 'supported_blocks':1})
            (run/'summary.json').write_text(json.dumps(summary), encoding='utf-8')
            record = dict(row=0, block_id='tess:0', ground_truth_type='apk', predicted_type='apk',
                          metadata={'label':'1'}, results={'png':validate_block(data,'png'), 'zip':validate_block(data,'zip')})
            (run/'evidence.jsonl').write_text(json.dumps(record)+'\n', encoding='utf-8')
            result = analyze(run, Path(temp)/'out')
            self.assertEqual(result['supported_blocks'], 1)
            self.assertEqual(result['per_structure_and_truth']['png | truth=apk']['source_label_matches_structure'], 0)
            self.assertEqual(result['metadata_keywords']['png | truth=apk | Software'], 1)
