"""Standard Claude API prices, checked 2026-11-19.
https://platform.claude.com/docs/en/about-claude/pricing

Rates are integer nanodollars per token, in input/output/cache-read/5m-write order.
"""
import re

TOKEN_FIELDS = ('input_tokens', 'output_tokens', 'cache_read_input_tokens', 'cache_creation_input_tokens')
RATES = {
    'fable-5-2': (8000, 40000, 200, 10000),
    'fable-5-1': (10000, 50000, 250, 12500),
    'fable-5': (10000, 50000, 1000, 12500),
    'opus-5-5': (4000, 20000, 200, 5000),
    'opus-5': (5000, 25000, 500, 6250),
    'opus-4-1': (15000, 75000, 1500, 18750),
    'opus-4': (15000, 75000, 1500, 18750),
    'sonnet-5-5': (2000, 10000, 100, 2500),
    'sonnet-5': (2000, 10000, 200, 2500),
    'sonnet-4-6': (3000, 15000, 300, 3750),
    'sonnet-4-5': (3000, 15000, 300, 3750),
    'sonnet-4': (3000, 15000, 300, 3750),
    'haiku-5-5': (100, 500, 10, 125),
    'haiku-4-5': (1000, 5000, 100, 1250),
}
for minor in (5, 6, 7, 8):
    RATES[f'opus-4-{minor}'] = RATES['opus-5']
for version in ('5', '5-1', '5-2'):
    RATES[f'mythos-{version}'] = RATES[f'fable-{version}']


def estimate(model, tokens, *, one_hour_writes=0, historical=False):
    name = re.sub(r'-\d{8}$', '', model.removeprefix('claude-').removesuffix('[1m]'))
    rates = RATES.get(name) if model.startswith('claude-') else None
    if rates is None:
        return 0, sum(tokens)
    if name == 'haiku-5-5' and not historical and tokens[0] + sum(tokens[2:]) > 100_000:
        rates = (500, 2500, 50, 625)
    total = sum(count * rate for count, rate in zip(tokens, rates))
    return total + min(tokens[3], one_hour_writes) * (2 * rates[0] - rates[3]), 0

def usage_price(model, usage):
    cache = usage.get('cache_creation') or {}
    hour = usage.get('one_hour_cache_writes', cache.get('ephemeral_1h_input_tokens', 0))
    return estimate(model, [max(0, usage.get(k, 0)) for k in TOKEN_FIELDS],
                    one_hour_writes=max(0, hour))
