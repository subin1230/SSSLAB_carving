import argparse
import unittest

from score_samefile_pilot import add_runtime_arguments, prepare_device_environment


class RuntimeSelectionTests(unittest.TestCase):
    def args(self, *argv):
        parser = argparse.ArgumentParser()
        add_runtime_arguments(parser)
        return parser.parse_args(argv)

    def test_default_cpu_does_not_query_or_reserve_gpu(self):
        env = {'CUDA_VISIBLE_DEVICES': 'GPU-existing'}
        def forbidden(_):
            self.fail('CPU inference must not query a GPU')
        self.assertIsNone(prepare_device_environment(self.args(), env, forbidden))
        self.assertEqual(env['CUDA_VISIBLE_DEVICES'], '-1')
        with self.assertRaises(ValueError):
            prepare_device_environment(self.args('--gpu-index', '2'), {})

    def test_explicit_gpu_is_resolved_to_uuid_with_bounded_memory(self):
        env, called = {}, []
        def query(index):
            called.append(index)
            return 'GPU-specified, NVIDIA RTX A5000, 8192\n'
        r = prepare_device_environment(self.args('--device', 'gpu', '--gpu-index', '2'), env, query)
        self.assertEqual(called, [2])
        self.assertEqual(env['CUDA_VISIBLE_DEVICES'], 'GPU-specified')
        self.assertEqual(r['tensorflow_memory_limit_mib'], 2048)
        self.assertEqual(r['nvidia_smi_index'], 2)

    def test_low_memory_stops_before_selecting_a_gpu(self):
        env = {}
        with self.assertRaisesRegex(ValueError, 'headroom'):
            prepare_device_environment(self.args('--device', 'gpu', '--gpu-index', '2'), env,
                                       lambda _: 'GPU-specified, A5000, 3000')
        self.assertNotIn('CUDA_VISIBLE_DEVICES', env)

    def test_existing_scheduler_visibility_is_not_overwritten(self):
        for value in ('', '-1', '0,1', 'GPU-allocated'):
            env = {'CUDA_VISIBLE_DEVICES': value}
            with self.assertRaisesRegex(ValueError, 'already set'):
                prepare_device_environment(self.args('--device', 'gpu', '--gpu-index', '2'), env)
            self.assertEqual(env['CUDA_VISIBLE_DEVICES'], value)

    def test_missing_selection_bad_cap_and_ambiguous_query_fail_closed(self):
        for argv in (('--device', 'gpu'), ('--device', 'gpu', '--gpu-index', '-1'),
                     ('--device', 'gpu', '--gpu-index', '0', '--gpu-memory-mb', '0')):
            with self.assertRaises(ValueError):
                prepare_device_environment(self.args(*argv), {})
        for response in ('', 'GPU-one, A, 9000\nGPU-two, A, 9000', 'MIG-123, A, 9000',
                         'GPU-one, A, N/A'):
            with self.assertRaises(ValueError):
                prepare_device_environment(self.args('--device', 'gpu', '--gpu-index', '0'), {},
                                           lambda _: response)


if __name__ == '__main__':
    unittest.main()
