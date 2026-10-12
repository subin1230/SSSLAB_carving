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
import sys
import time

import numpy as np

from zip_pair_candidates import fingerprint, load_blocks
from zip_pair_payload import file_hash

HIDDEN_LAYER = 'leaky_re_lu_2'
HIDDEN_DIM = 256


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


def verify_reference(block_path, embedding_path, predict):
    raw = load_blocks(block_path)
    expected = np.load(embedding_path, mmap_mode='r', allow_pickle=False)
    if len(raw) == 0 or expected.shape != (len(raw), HIDDEN_DIM) or expected.dtype != np.float32:
        raise ValueError('invalid reference embedding shape/dtype')
    rows = np.unique(np.linspace(0, len(raw)-1, min(16, len(raw)), dtype=np.int64))
    batch = np.asarray(raw[rows])
    actual = extract_embeddings(batch, predict, len(rows))
    reference = np.asarray(expected[rows])
    if not np.isfinite(reference).all():
        raise ValueError('nonfinite saved reference embeddings')
    difference = float(np.max(np.abs(actual-reference)))
    if not np.allclose(actual, reference, atol=1e-4, rtol=1e-4):
        raise ValueError(f'reference embeddings do not reproduce (max absolute difference={difference}); check encoder, layer, preprocessing and TensorFlow environment')
    return dict(raw_path=str(block_path), embedding_path=str(embedding_path), rows=rows.tolist(),
                matched=True, atol=1e-4, rtol=1e-4, max_absolute_difference=difference,
                raw_sample_sha256=hashlib.sha256(batch.tobytes()).hexdigest(),
                embedding_sample_sha256=hashlib.sha256(reference.tobytes()).hexdigest())


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


def score(args):
    output = Path(args.output)
    if output.exists():
        raise FileExistsError('output exists; choose a new output directory')
    if args.batch_size < 1 or args.threads < 1:
        raise ValueError('batch size and threads must be positive')
    x = load_blocks(args.blocks)
    if len(x) < 2:
        raise ValueError('requires at least two blocks')
    if importlib.util.find_spec('tensorflow') is None:
        raise SystemExit('현재 Python에 TensorFlow가 없음. 먼저 python3 score_samefile_pilot.py environment 결과를 확인하세요. 데이터/모델은 변경하지 않았습니다.')
    # CPU default for shared-server inference. Do not reserve someone else's GPU.
    os.environ['CUDA_VISIBLE_DEVICES'] = '-1'
    os.environ.setdefault('TF_CPP_MIN_LOG_LEVEL', '2')
    import tensorflow as tf
    tf.config.threading.set_intra_op_parallelism_threads(args.threads)
    tf.config.threading.set_inter_op_parallelism_threads(1)
    tf.keras.utils.set_random_seed(42)
    encoder_hash, pair_hash = file_hash(args.encoder_model), file_hash(args.pair_model)
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
    print('[기존 임베딩 재현 검사]', flush=True)
    reference = verify_reference(args.reference_blocks, args.reference_embeddings, encoder.predict_on_batch)
    print(json.dumps(reference, indent=2), flush=True)
    print(f'[우리 블록 {len(x):,}개 임베딩 추출]', flush=True)
    embeddings = extract_embeddings(x, encoder.predict_on_batch, args.batch_size)
    provenance = dict(
        python=sys.executable, tensorflow=tf.__version__, numpy=np.__version__, device='CPU',
        intra_op_threads=args.threads, batch_size=args.batch_size,
        encoder_model=str(Path(args.encoder_model).resolve()), encoder_model_sha256=encoder_hash,
        pair_model=str(Path(args.pair_model).resolve()), pair_model_sha256=pair_hash,
        reference_verification=reference,
    )
    return write_scores(x, embeddings, head.predict_on_batch, output, args.batch_size, provenance)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest='command', required=True)
    sub.add_parser('environment')
    s = sub.add_parser('score')
    for name in ('blocks', 'encoder-model', 'pair-model', 'reference-blocks', 'reference-embeddings', 'output'):
        s.add_argument('--'+name, required=True)
    s.add_argument('--batch-size', type=int, default=1024)
    s.add_argument('--threads', type=int, default=2)
    a = p.parse_args()
    if a.command == 'environment':
        environment()
    else:
        score(a)


if __name__ == '__main__':
    main()
