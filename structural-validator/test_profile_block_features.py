import unittest
try:
    import numpy as np
except ImportError:
    np=None


@unittest.skipIf(np is None,'requires numpy')
class FeatureTests(unittest.TestCase):
    def test_features(self):
        from profile_block_features import features
        x=features(np.array([[0]*512,list(range(256))*2],dtype=np.uint8))
        self.assertAlmostEqual(x[0,0],0)
        self.assertEqual(x[0,2],1)
        self.assertEqual(x[0,4],1)
        self.assertAlmostEqual(x[1,0],8)

    def test_stream_sample_matches_rows(self):
        from profile_block_features import sample_split,features
        import tempfile,csv
        from pathlib import Path
        with tempfile.TemporaryDirectory() as t:
            root=Path(t);x=np.random.default_rng(5).integers(0,256,(40,512),dtype=np.uint8);y=np.array([0,1]*20,dtype=np.uint8)
            np.savez_compressed(root/'train.npz',x=x,y=y)
            with (root/'train_meta.csv').open('w',newline='') as f:
                w=csv.writer(f);w.writerow(['row','label','ground_truth_type','block_id','source_file_id'])
                for i,v in enumerate(y):w.writerow([i,v,['png','zip'][v],f'b{i}',f'f{i}'])
            xx,types,meta=sample_split(root,'train',10,42)
            self.assertEqual(len(xx),20)
            np.testing.assert_allclose(xx,features(x[[int(m['row']) for m in meta]]))
            self.assertEqual(sum(types=='png'),10)
