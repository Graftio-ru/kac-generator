"""Offline estimate from recorded usage and explicitly supplied per-million USD rates.

This does not query an account or claim the actual amount deducted by a provider.
"""
import argparse
import json
from decimal import Decimal
from pathlib import Path


def estimate_report(report, rates):
    job = report.get('job')
    if job is not None:
        progress = job.get('progress_rows', {})
        metrics = [(row or progress.get(str(index), {})).get('metrics', {})
                   for index, row in enumerate(job.get('rows', []))]
    else:
        metrics = [report.get('metrics', {})]
    usage = {}
    for metric in metrics:
        for model, values in metric.get('llm_usage', {}).items():
            item = usage.setdefault(model, {key: 0 for key in (
                'responses', 'input_tokens', 'cache_hit_tokens', 'output_tokens')})
            for key in item:
                value = int(values.get(key, 0))
                if value < 0:
                    raise ValueError('Recorded usage cannot be negative')
                item[key] += value
    costs = {}
    total = Decimal(0)
    unknown = []
    for model, item in usage.items():
        if item['cache_hit_tokens'] > item['input_tokens']:
            raise ValueError('Cache hits cannot exceed total input tokens')
        if model not in rates:
            unknown.append(model)
            continue
        rate = {key: Decimal(str(rates[model][key])) for key in (
            'input_cache_miss', 'input_cache_hit', 'output')}
        if any(not value.is_finite() or value < 0 for value in rate.values()):
            raise ValueError('Rates must be finite nonnegative USD per million tokens')
        cost = ((item['input_tokens']-item['cache_hit_tokens'])*rate['input_cache_miss']
                + item['cache_hit_tokens']*rate['input_cache_hit']
                + item['output_tokens']*rate['output']) / Decimal(1_000_000)
        costs[model] = str(cost)
        total += cost
    return {'usage': usage, 'model_estimates_usd': costs,
            'estimated_usd': str(total) if usage and not unknown else None,
            'unpriced_models': unknown,
            'notice': 'Estimate of recorded responses only; not provider billing. '
                      'Missing usage, unrecorded calls and account activity are not included.'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--report', type=Path, required=True)
    parser.add_argument('--rates', type=Path, required=True,
                        help='JSON map provider:model -> input_cache_miss,input_cache_hit,output; USD/1M')
    args = parser.parse_args()
    print(json.dumps(estimate_report(json.loads(args.report.read_text()),
                                     json.loads(args.rates.read_text())), ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
