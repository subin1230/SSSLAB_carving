import unittest
from confusion_families import build_lookup, summarize


class FamilyTests(unittest.TestCase):
    def test_wrong_png_predictions_grouped_by_truth_family(self):
        rows = [('png', 'png'), ('docx', 'png'), ('apk', 'png'), ('jpg', 'png'),
                ('png', 'pdf'), ('zip', 'zip'), ('txt', 'zip')]
        s = summarize(rows)
        self.assertEqual(s['ffc_wrong'], 5)
        png = s['wrong_predicted_as']['png']
        self.assertEqual(png['wrong'], 3)
        self.assertEqual(png['by_family'], {'deflate': 2, 'other_compressed': 1})
        self.assertAlmostEqual(png['deflate_share'], 0.6667)
        self.assertEqual(s['missed_truth']['png']['by_family'], {'deflate_mixed': 1})
        self.assertEqual(s['wrong_predicted_as']['zip']['by_family'], {'text': 1})
        # docx->png, apk->png, png->pdf
        self.assertEqual(s['all_errors_within_deflate_or_mixed'], 3)

    def test_unmapped_types_reported(self):
        s = summarize([('foo', 'png')])
        self.assertEqual(s['unmapped_types'], {'foo': 1})
        self.assertEqual(s['wrong_predicted_as']['png']['by_family'], {'unmapped': 1})

    def test_duplicate_family_rejected(self):
        with self.assertRaises(ValueError):
            build_lookup({'a': ['png'], 'b': ['PNG']})
