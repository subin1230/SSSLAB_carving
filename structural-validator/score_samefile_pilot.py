"""Frozen FiFTy + V7 MLP inference on shuffled blocks, without truth or ZIP edges.

V7 pair input: concat(abs(h1-h2), h1*h2), float32. The main MLP outputs
logits, so its scalar output must be passed through sigmoid exactly once.
"""
import argparse
import csv
import gzip
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import time

import numpy as np

from zip_pair_candidates import fingerprint, load_blocks
from zip_pair_payload import file_hash

HIDDEN_LAYER = 'leaky_re_lu_2'
HIDDEN_DIM = 256
REFERENCE_ATOL = 1e-4
REFERENCE_RTOL = 1e-4


def pair_features(h1, h2):
    a, b = np.asarray(h1, dtype=np.float32), np.asarray(h2, dtype=np.float32)
    if a.ndim != 2 or a.shape != b.shape or a.shape[1] != HIDDEN_DIM:
        raise ValueError('requires matching (N,256) embeddings')
    with np.errstate(over='raise', invalid='raise'):
        result = np.concatenate((np.abs(a-b), a*b), axis=1)
    if not np.isfinite(result).all():
        raise ValueError('nonfinite pair features')
    return result


def sigmoid(logits):
    z = np.asarray(logits, dtype=np.float64)
    if not np.isfinite(z).all():
        raise ValueError('nonfinite model logits')
    result = np.empty_like(z)
    positive = z >= 0
    result[positive] = 1 / (1 + np.exp(-z[positive]))
    ez = np.exp(z[~positive])
    result[~positive] = ez / (1 + ez)
    return result


def pair_batches(count, batch_size):
    if count < 2 or batch_size < 1:
        raise ValueError('requires at least two blocks and a positive batch size')
    batch = []
    for i in range(count-1):
        for j in range(i+1, count):
            batch.append((i, j))
            if len(batch) == batch_size:
                yield np.asarray(batch, dtype=np.int64)
                batch = []
    if batch:
        yield np.asarray(batch, dtype=np.int64)


def extract_embeddings(x, predict, batch_size):
    if batch_size < 1:
        raise ValueError('batch size must be positive')
    out = np.empty((len(x), HIDDEN_DIM), dtype=np.float32)
    for start in range(0, len(x), batch_size):
        end = min(start+batch_size, len(x))
        # Match Narin's raw 0..255 byte input. No division by 255 or normalization.
        h = np.asarray(predict(np.asarray(x[start:end])), dtype=np.float32)
        if h.shape != (end-start, HIDDEN_DIM) or not np.isfinite(h).all():
            raise ValueError('unexpected/nonfinite encoder output')
        out[start:end] = h
    return out


def reference_sample(block_path, embedding_path):
    raw = load_blocks(block_path)
    expected = np.load(embedding_path, mmap_mode='r', allow_pickle=False)
    if len(raw) == 0 or expected.shape != (len(raw), HIDDEN_DIM) or expected.dtype != np.float32:
        raise ValueError('invalid reference embedding shape/dtype')
    rows = np.unique(np.linspace(0, len(raw)-1, min(16, len(raw)), dtype=np.int64))
    batch = np.asarray(raw[rows])
    reference = np.asarray(expected[rows])
    if not np.isfinite(reference).all():
        raise ValueError('nonfinite saved reference embeddings')
    metadata = dict(raw_path=str(block_path), embedding_path=str(embedding_path), rows=rows.tolist(),
                    raw_shape=list(raw.shape), embedding_shape=list(expected.shape),
                    raw_sample_sha256=hashlib.sha256(batch.tobytes()).hexdigest(),
                    embedding_sample_sha256=hashlib.sha256(reference.tobytes()).hexdigest())
    return batch, reference, metadata


def reference_batch_context(block_path, rows, predict, batch_size):
    """Recreate each selected row's original contiguous inference batch."""
    raw = load_blocks(block_path)
    rows = np.asarray(rows)
    if (batch_size < 1 or rows.ndim != 1 or not len(rows)
            or rows.dtype.kind not in 'iu' or (rows < 0).any() or (rows >= len(raw)).any()):
        raise ValueError('invalid reference batch size or row indices')
    result = np.empty((len(rows), HIDDEN_DIM), dtype=np.float32)
    starts = (rows // batch_size)*batch_size
    windows = []
    for start in np.unique(starts):
        start = int(start)
        end = min(start+batch_size, len(raw))
        batch = np.asarray(raw[start:end])
        h = extract_embeddings(batch, predict, len(batch))
        selected = np.flatnonzero(starts == start)
        result[selected] = h[rows[selected]-start]
        windows.append(dict(start=start, end_exclusive=end, rows=end-start,
                            raw_sha256=hashlib.sha256(batch.tobytes()).hexdigest()))
        print(f'원래 배치 재현: {start:,}:{end:,} ({len(windows)}개 배치 완료)', flush=True)
    return result, dict(batch_size=batch_size, windows=windows,
                        processed_rows=sum(w['rows'] for w in windows), compared_rows=len(rows),
                        note='Original raw order and final short batch preserved; no padding or sample duplication. Only selected rows are compared.')


def verify_reference(block_path, embedding_path, predict, reference_batch_size=0):
    batch, reference, metadata = reference_sample(block_path, embedding_path)
    if reference_batch_size < 0:
        raise ValueError('reference batch size must be nonnegative')
    if reference_batch_size:
        actual, context = reference_batch_context(block_path, metadata['rows'], predict, reference_batch_size)
        metadata.update(inference_mode='original_batch_context', batch_context=context)
    else:
        actual = extract_embeddings(batch, predict, len(batch))
        metadata.update(inference_mode='sample_batch', batch_size=len(batch))
    difference = float(np.max(np.abs(actual-reference)))
    if not np.allclose(actual, reference, atol=REFERENCE_ATOL, rtol=REFERENCE_RTOL):
        raise ValueError(f'reference embeddings do not reproduce (max absolute difference={difference}); check encoder, layer, preprocessing and TensorFlow environment. Run diagnose_samefile_reference.py for details; scoring was not started.')
    return dict(metadata, matched=True, atol=REFERENCE_ATOL, rtol=REFERENCE_RTOL,
                max_absolute_difference=difference)


def write_scores(x, embeddings, predict, output, batch_size, provenance):
    """Truth-free scoring; model calls are injected so batching can be tested."""
    output = Path(output)
    if len(x) < 2 or batch_size < 1 or embeddings.shape != (len(x), HIDDEN_DIM):
        raise ValueError('invalid scoring dimensions/batch size')
    if embeddings.dtype != np.float32 or not np.isfinite(embeddings).all():
        raise ValueError('requires finite float32 embeddings')
    output.mkdir(parents=True, exist_ok=False)
    state = output/'run_state.json'
    state.write_text(json.dumps(dict(status='running'))+'\n')
    start = time.monotonic()
    total, written = len(x)*(len(x)-1)//2, 0
    try:
        np.save(output/'embeddings.npy', embeddings)
        score_path = output/'samefile_scores.csv.gz'
        with gzip.open(score_path, 'wt', newline='', encoding='utf-8', compresslevel=3) as f:
            w = csv.writer(f)
            w.writerow(['sample_id_1', 'sample_id_2', 'same_file_logit', 'same_file_score'])
            for batch_number, ids in enumerate(pair_batches(len(x), batch_size)):
                features = pair_features(embeddings[ids[:, 0]], embeddings[ids[:, 1]])
                logits = np.asarray(predict(features))
                if logits.shape != (len(ids), 1):
                    raise ValueError('expected scalar logit per pair, shape (N,1)')
                logits = logits[:, 0]
                scores = sigmoid(logits)
                w.writerows((int(i), int(j), format(float(z), '.9g'), format(float(s), '.9g'))
                            for (i, j), z, s in zip(ids, logits, scores))
                written += len(ids)
                if batch_number % 100 == 0 or written == total:
                    print(f'Same-File 점수: {written:,}/{total:,}쌍', flush=True)
        if written != total:
            raise ValueError('incomplete pair enumeration')
        manifest = dict(
            provenance=provenance, blocks=len(x), block_size=512, blocks_sha256=fingerprint(x),
            pair_count=written, pair_selection='all unordered distinct pairs; sample_id_1 < sample_id_2',
            hidden_layer=HIDDEN_LAYER, hidden_dimension=HIDDEN_DIM,
            pair_feature='concat(abs(h1-h2), h1*h2), float32, no feature scaling',
            model_output='logit', score_transform='sigmoid exactly once',
            truth_used=False, zip_evidence_used=False, threshold_applied=False,
            zero_blocks_retained=int(np.count_nonzero(~np.any(x, axis=1))),
            seconds=time.monotonic()-start, embeddings_sha256=file_hash(output/'embeddings.npy'),
            scores_sha256=file_hash(score_path),
            note='Frozen inference, no training. Row index is the pilot sample_id. All pairs are scored without true format/UUID routing, including cross-format pairs outside V7 same-type negative sampling. Scores are not calibrated probabilities or grouping results. Source overlap is audited separately; no block is dropped based on truth.',
        )
        (output/'score_manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2)+'\n')
        state.write_text(json.dumps(dict(status='complete', pairs=written))+'\n')
    except Exception as e:
        state.write_text(json.dumps(dict(status='failed', pairs_written=written, error=str(e)))+'\n')
        raise
    print('점수 저장:', score_path)
    return manifest


def environment():
    info = dict(python=sys.executable, python_version=sys.version.split()[0], numpy=np.__version__,
                tensorflow_available=importlib.util.find_spec('tensorflow') is not None)
    print(json.dumps(info, indent=2))
    print('\n[기존 Python 환경 경로 — 자동 실행하거나 설치하지 않음]')
    for root in (Path('/home/narin/venvs'), Path('/home/subin/venvs')):
        for path in sorted(root.glob('*/bin/python')):
            print(path)


def add_runtime_arguments(parser):
    parser.add_argument('--threads', type=int, default=2)
    parser.add_argument('--device', choices=('cpu', 'gpu'), default='cpu')
    parser.add_argument('--gpu-index', type=int, help='nvidia-smi index; required for GPU')
    parser.add_argument('--gpu-memory-mb', type=int, default=2048,
                        help='TensorFlow GPU allocator cap; CUDA overhead can be additional')
    parser.add_argument('--tf32', choices=('default', 'on', 'off'), default='default')


def prepare_device_environment(args, environ, query_gpu=None):
    """Select an explicit GPU by UUID before importing TF; never pick a busy GPU silently."""
    if args.threads < 1:
        raise ValueError('threads must be positive')
    if args.device == 'cpu':
        if args.gpu_index is not None or args.tf32 != 'default':
            raise ValueError('GPU index/TF32 options require --device gpu')
        environ['CUDA_VISIBLE_DEVICES'] = '-1'
        return None
    if args.gpu_index is None or args.gpu_index < 0 or args.gpu_memory_mb < 256:
        raise ValueError('GPU requires a nonnegative --gpu-index and memory cap >= 256 MiB')
    if 'CUDA_VISIBLE_DEVICES' in environ:
        raise ValueError('CUDA_VISIBLE_DEVICES is already set. Refusing to overwrite an existing GPU allocation; inspect the assigned devices before choosing an index.')
    if query_gpu is None:
        def query_gpu(index):
            return subprocess.run(
                ['nvidia-smi', '--id='+str(index), '--query-gpu=uuid,name,memory.free',
                 '--format=csv,noheader,nounits'], check=True, capture_output=True,
                text=True, timeout=10).stdout
    records = list(csv.reader(query_gpu(args.gpu_index).strip().splitlines()))
    if len(records) != 1 or len(records[0]) != 3:
        raise ValueError('cannot identify exactly one GPU from nvidia-smi')
    uuid, name, free_text = [v.strip() for v in records[0]]
    if not uuid.startswith('GPU-'):
        raise ValueError('expected a physical GPU UUID; MIG is not supported here')
    free_mb = int(free_text)
    if free_mb < args.gpu_memory_mb+1024:
        raise ValueError(f'GPU {args.gpu_index} has only {free_mb} MiB free; require cap plus 1024 MiB headroom. No GPU was selected.')
    # UUID avoids index reordering between CUDA and nvidia-smi.
    environ['CUDA_VISIBLE_DEVICES'] = uuid
    return dict(nvidia_smi_index=args.gpu_index, uuid=uuid, name=name,
                free_memory_mib_before_load=free_mb, tensorflow_memory_limit_mib=args.gpu_memory_mb,
                note='Free memory is a snapshot, not a reservation. Other jobs remain running.')


class DevicePredictor:
    def __init__(self, tf, model, device):
        self.tf, self.model, self.device = tf, model, device

    def predict_on_batch(self, batch):
        with self.tf.device(self.device):
            return self.model.predict_on_batch(batch)


def load_models(args):
    """Shared inference setup. CPU default; explicitly selected GPU has a memory cap."""
    if importlib.util.find_spec('tensorflow') is None:
        raise SystemExit('현재 Python에 TensorFlow가 없음. 먼저 python3 score_samefile_pilot.py environment 결과를 확인하세요. 데이터/모델은 변경하지 않았습니다.')
    if 'tensorflow' in sys.modules:
        raise RuntimeError('start a fresh process: device setup must precede TensorFlow import')
    gpu = prepare_device_environment(args, os.environ)
    os.environ.setdefault('TF_CPP_MIN_LOG_LEVEL', '2')
    import tensorflow as tf
    tf.config.threading.set_intra_op_parallelism_threads(args.threads)
    tf.config.threading.set_inter_op_parallelism_threads(1)
    device = '/GPU:0' if gpu else '/CPU:0'
    if gpu:
        devices = tf.config.list_physical_devices('GPU')
        if len(devices) != 1:
            raise RuntimeError('selected GPU is not available to TensorFlow; CPU fallback is not allowed')
        tf.config.set_logical_device_configuration(
            devices[0], [tf.config.LogicalDeviceConfiguration(memory_limit=args.gpu_memory_mb)])
        if args.tf32 != 'default':
            tf.config.experimental.enable_tensor_float_32_execution(args.tf32 == 'on')
        gpu['tensorflow_device_details'] = tf.config.experimental.get_device_details(devices[0])
    tf.keras.utils.set_random_seed(42)
    encoder_hash, pair_hash = file_hash(args.encoder_model), file_hash(args.pair_model)
    with tf.device(device):
        fifty = tf.keras.models.load_model(args.encoder_model, compile=False)
        if tuple(fifty.input_shape) != (None, 512):
            raise ValueError(f'unexpected FiFTy input shape: {fifty.input_shape}')
        encoder = tf.keras.Model(inputs=fifty.input, outputs=fifty.get_layer(HIDDEN_LAYER).output)
        if tuple(encoder.output_shape) != (None, HIDDEN_DIM):
            raise ValueError('unexpected hidden dimension')
        head = tf.keras.models.load_model(args.pair_model, compile=False)
    if tuple(head.input_shape) != (None, 512) or tuple(head.output_shape) != (None, 1):
        raise ValueError('unexpected Same-File MLP shape')
    activation = tf.keras.activations.serialize(head.layers[-1].activation)
    if activation != 'linear':
        raise ValueError('expected V7 main MLP linear logit output; refusing a second sigmoid')
    if gpu and any('GPU:0' not in v.device for v in list(encoder.variables)+list(head.variables)):
        raise RuntimeError('model variables were not placed on the selected GPU')
    provenance = dict(
        python=sys.executable, python_version=sys.version.split()[0],
        tensorflow=tf.__version__, keras=getattr(tf.keras, '__version__', None),
        numpy=np.__version__, device='GPU' if gpu else 'CPU', intra_op_threads=args.threads,
        requested_placement=device, gpu=gpu, tf32_requested=args.tf32,
        tf32_enabled=bool(tf.config.experimental.tensor_float_32_execution_enabled()),
        onednn_environment=os.environ.get('TF_ENABLE_ONEDNN_OPTS', 'unset'),
        tensorflow_build=tf.sysconfig.get_build_info(),
        visible_devices=[d.name for d in tf.config.get_visible_devices()],
        encoder_model=str(Path(args.encoder_model).resolve()), encoder_model_sha256=encoder_hash,
        pair_model=str(Path(args.pair_model).resolve()), pair_model_sha256=pair_hash,
        encoder_variable_devices=sorted({v.device for v in encoder.variables}),
        pair_variable_devices=sorted({v.device for v in head.variables}),
    )
    return DevicePredictor(tf, encoder, device), DevicePredictor(tf, head, device), provenance


def score(args):
    output = Path(args.output)
    if output.exists():
        raise FileExistsError('output exists; choose a new output directory')
    if args.batch_size < 1 or args.reference_batch_size < 0:
        raise ValueError('batch size must be positive and reference batch size nonnegative')
    x = load_blocks(args.blocks)
    if len(x) < 2:
        raise ValueError('requires at least two blocks')
    encoder, head, provenance = load_models(args)
    print('[기존 임베딩 재현 검사]', flush=True)
    reference = verify_reference(args.reference_blocks, args.reference_embeddings,
                                 encoder.predict_on_batch, args.reference_batch_size)
    print(json.dumps(reference, indent=2), flush=True)
    print(f'[우리 블록 {len(x):,}개 임베딩 추출]', flush=True)
    embeddings = extract_embeddings(x, encoder.predict_on_batch, args.batch_size)
    provenance.update(batch_size=args.batch_size, reference_verification=reference)
    return write_scores(x, embeddings, head.predict_on_batch, output, args.batch_size, provenance)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest='command', required=True)
    sub.add_parser('environment')
    s = sub.add_parser('score')
    for name in ('blocks', 'encoder-model', 'pair-model', 'reference-blocks', 'reference-embeddings', 'output'):
        s.add_argument('--'+name, required=True)
    s.add_argument('--batch-size', type=int, default=1024)
    s.add_argument('--reference-batch-size', type=int, default=0,
                   help='0: sample batch; otherwise verify in original contiguous inference batches')
    add_runtime_arguments(s)
    a = p.parse_args()
    if a.command == 'environment':
        environment()
    else:
        score(a)


if __name__ == '__main__':
    main()
