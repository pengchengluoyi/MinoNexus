"""Offline legal-image calibration using the existing assert_visual verifier.

No device calls, recovery or fabricated labels. Outputs are evaluation artifacts,
not a replacement business configuration store.
"""
from __future__ import annotations

import argparse
import base64
import json
import mimetypes
import os
from collections import Counter, defaultdict
from pathlib import Path


def validate_manifest(manifest, base):
    rows = manifest.get('samples') or []
    if manifest.get('schema') != 'mino.vision_calibration.v1':
        raise ValueError('invalid_manifest_schema')
    identities = set()
    for row in rows:
        if row.get('id') in identities or not row.get('id'):
            raise ValueError('duplicate_sample_id')
        identities.add(row['id'])
        if row.get('platform') not in {'web', 'android', 'ios', 'pc'}:
            raise ValueError('invalid_platform')
        if not row.get('expected') or not row.get('source') or row.get('legal_use_confirmed') is not True:
            raise ValueError('missing_expected_or_legal_source')
        labels = row.get('human_labels') or []
        if not labels or any(x not in {'pass', 'fail', 'insufficient'} for x in labels):
            raise ValueError('human_labels_required')
        image = (base / row['image']).resolve(strict=True)
        if not image.is_relative_to(base.resolve()):
            raise ValueError('image_outside_dataset')
        if not image.is_file():
            raise ValueError('image_missing')
    return rows


def report(rows):
    strata = defaultdict(Counter)
    disagreements = []
    for row in rows:
        labels = set(row['human_labels'])
        if len(labels) != 1:
            disagreements.append(row['id'])
            truth = 'disagreement'
        else:
            truth = next(iter(labels))
        prediction = row.get('prediction', 'not_run')
        key = f"{row['platform']}:{row.get('stratum', 'unclassified')}"
        strata[key][f'{truth}->{prediction}'] += 1
        strata[key]['samples'] += 1
        strata[key]['false_pass'] += int(truth in {'fail', 'insufficient'} and prediction == 'pass')
        strata[key]['false_fail'] += int(truth == 'pass' and prediction == 'fail')
        strata[key]['unavailable'] += int(prediction in {'insufficient', 'not_run'})
        strata[key]['decidable'] += int(prediction in {'pass', 'fail'})
    return {'schema': 'mino.vision_calibration_report.v1', 'sample_count': len(rows),
            'strata': {key: dict(value) for key, value in strata.items()}, 'disagreements': disagreements,
            'coverage_denominator': len(rows), 'samples': rows,
            'interpretation': 'Observed counts only; small samples do not establish general accuracy.'}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('manifest', type=Path)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--infer', action='store_true')
    parser.add_argument('--data-dir', type=Path)
    parser.add_argument('--user-id')
    parser.add_argument('--provider-id')
    args = parser.parse_args()
    if args.output.exists():
        raise ValueError('output_exists_use_new_path')
    source = args.manifest.resolve(strict=True)
    manifest = json.loads(source.read_text())
    rows = validate_manifest(manifest, source.parent)
    if args.infer:
        if not args.data_dir or not args.user_id:
            raise ValueError('explicit_execution_user_and_data_dir_required')
        os.environ['MINO_NEXUS_DATA_DIR'] = str(args.data_dir.resolve(strict=True))
        from mino_nexus.core.execution_user import execution_user
        from mino_nexus.ai.planner import assert_visual
        from mino_nexus.ai.dispatch_log import bind, reset, get_call
        with execution_user(args.user_id):
            for row in rows:
                image = (source.parent / row['image']).resolve(strict=True)
                token = bind(trigger='offline_calibration', source='v5_vision_calibration', job='assert-vision')
                try:
                    result = assert_visual(expectation=row['expected'], image_base64=base64.b64encode(image.read_bytes()).decode(),
                        image_mime=mimetypes.guess_type(image.name)[0] or 'image/png', provider_id=args.provider_id)
                finally:
                    reset(token)
                row['prediction'] = 'insufficient' if result.parse_warnings or result.error_kind else 'pass' if result.passed else 'fail'
                row['verifier_build'] = 'assert-vision-existing-v1'
                row['verifier_output'] = {'confidence': result.confidence, 'warnings': result.parse_warnings}
                # Keep actual model/prompt versions when supplied by the verifier.
                raw = result.raw_llm or {}
                row['model_evidence'] = {'meta': raw.get('meta', {}), 'prompt_version': raw.get('prompt_version'), 'prompt_sha': raw.get('prompt_sha')}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report(rows), ensure_ascii=False, indent=2))
    print(json.dumps({'sample_count': len(rows), 'output': str(args.output)}, ensure_ascii=False))


if __name__ == '__main__':
    main()
