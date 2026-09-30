#!/usr/bin/env python3
"""Synthetic design sizing, not the Event Broker/Orders production serializer.
Run: python3 docs/reviews/fixtures/orders-lifecycle-capacity.py
The real serialized envelope remains subject to the normative byte limit.
"""
import json
from uuid import UUID


def uid(n):
    return str(UUID(int=n))


def size(value):
    return len(json.dumps(value, separators=(',', ':'), ensure_ascii=False).encode('utf-8'))


def event(reference_bytes):
    return {
        'id': uid(1), 'type': 'OrderCompleted', 'version': 1,
        'source': 'bss-orders-lifecycle', 'time': '2026-09-29T12:00:00.000000Z',
        'envelopeSizingReserve': 'x' * 2048,
        'data': {
            'orderId': uid(2), 'orderVersion': 999999999,
            'category': 'new_sale', 'state': 'completed',
            'resourceTenantId': uid(3), 'sellerTenantId': uid(4),
            'payerTenantId': uid(5), 'contractRef': uid(6),
            'externalReference': 'x' * reference_bytes,
            'lines': [{'lineId': uid(100+i), 'subscriptionId': uid(500+i),
                       'externalReference': 'x' * reference_bytes} for i in range(200)],
        },
    }


if __name__ == '__main__':
    for ref_bytes in (64, 256):
        payload = event(ref_bytes)
        count = size(payload)
        fits = count <= 64 * 1024
        assert fits == (ref_bytes == 64)
        assert len({line['lineId'] for line in payload['data']['lines']}) == 200
        print(f'200-line completion, {ref_bytes}-byte references: {count} bytes; '
              f'{"fits" if fits else "must refuse before commit"}')
    for descriptor_bytes in (4096, 8192):
        version = {'orderId': uid(2), 'version': 2,
                   'items': [{'itemId': uid(100+i), 'descriptor': 'x' * descriptor_bytes}
                             for i in range(200)]}
        count = size(version)
        fits = count <= 1024 * 1024
        assert fits == (descriptor_bytes == 4096)
        print(f'200-item illustrative version, {descriptor_bytes}-byte descriptors: '
              f'{count} bytes; {"fits" if fits else "must refuse before commit"}')
    # D-159: chain slots nested under items, as resolve answers them; 1,000 bound slots per LINE
    # (Pricing's MAX_PINS is per request, and a request is one revision). The synthetic version
    # below is one line; the second case is one item whose matrix alone exceeds the cap.
    def binding(n):
        return {'price_id': uid(9000+n), 'dim_used': None, 'pinned_from': None,
                'price': {'unit': '0.10'}, 'min_fee': '5.00', 'eligibility': 'all',
                'effective_from': '2026-01-01', 'effective_to': None,
                'temporary_until': None, 'ends_on': None, 'keep_for_bound': False}
    def slots(per_item):
        return [{'dim_value': None if c == 0 else f'r{c}', 'uncovered': False,
                 'binding': binding(c)} for c in range(per_item)]
    for items, per_item in ((200, 5), (1, 1001)):
        version = {'orderId': uid(2), 'version': 2,
                   'items': [{'itemId': uid(100+i), 'chains': slots(per_item)}
                             for i in range(items)]}
        total_slots = sum(len(item['chains']) for item in version['items'])
        count = size(version)
        within_cap = total_slots <= 1000
        assert within_cap == (per_item == 5)
        assert count <= 1024 * 1024
        print(f'one line, {items} item(s) x {per_item} bound chain slots = {total_slots} slots, '
              f'{count} bytes; {"fits" if within_cap else "must refuse: one item exceeds the per-line slot cap"}')
