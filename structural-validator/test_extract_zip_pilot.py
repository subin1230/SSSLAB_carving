import unittest
try:
    import numpy as np
except ImportError:
    np=None

@unittest.skipIf(np is None,'requires numpy')
class GroupTests(unittest.TestCase):
    def test_valid_and_broken_groups(self):
        from extract_zip_pilot import verify_group
        g=np.arange(32);codes=np.zeros(32,dtype=int);ix=np.arange(32);types=np.full(32,5);reps=np.zeros(32)
        verify_group(g,codes,ix,types,reps,5)
        for which in ('codes','ix','types','reps'):
            arrays=[v.copy() for v in (codes,ix,types,reps)]
            arrays[('codes','ix','types','reps').index(which)][16]+=1
            with self.assertRaises(ValueError):verify_group(g,*arrays,5)
        with self.assertRaises(ValueError):verify_group(np.full(32,0),codes,ix,types,reps,5)
