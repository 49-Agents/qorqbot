#!/usr/bin/env python3
"""Offline comparison on disposable receipts; never opens an instance database."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sqlite3
import sys
import tempfile

from measure_toolcall_latency import measure
from qorqut_operations.storage.core_schema import CORE_SCHEMA
from qorqut_operations.storage.queue_indexes import RECOVER_PENDING, SCHEMA

PREVIOUS_QUERY = """SELECT id FROM apply_receipts WHERE signoff_id IS NULL AND
    (state IN ('prepared','applying') OR (state='failed' AND json_extract(error_json, '$.retryable')=1
    AND EXISTS (SELECT 1 FROM resource_claims WHERE apply_id=apply_receipts.id))) ORDER BY created_at,id LIMIT ?"""


def evaluate(connection, query, rounds):
    def read():
        return connection.execute(query, (50,)).fetchall()
    result = measure(read, rounds)
    steps = []
    connection.set_progress_handler(lambda: steps.append(1) or 0, 1)
    try:
        read()
    finally:
        connection.set_progress_handler(None, 0)
    result['work_per_call'] = {'sqlite_vm_steps': len(steps)}
    result['query_plan'] = [row[3] for row in connection.execute('EXPLAIN QUERY PLAN ' + query, (50,))]
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--rounds', type=int, default=11)
    parser.add_argument('--receipts', type=int, default=5000)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    if not 3 <= args.rounds <= 100 or not 100 <= args.receipts <= 10000:
        parser.error('Use 3–100 rounds and 100–10000 receipts')
    with tempfile.TemporaryDirectory(prefix='queue-latency-') as directory:
        connection = sqlite3.connect(Path(directory) / 'synthetic.sqlite3')
        try:
            for statement in CORE_SCHEMA:
                if statement.startswith(('CREATE TABLE apply_receipts', 'CREATE TABLE resource_claims')):
                    connection.execute(statement)
            connection.execute('CREATE TABLE signoffs(id TEXT PRIMARY KEY,state TEXT,updated_sequence INTEGER UNIQUE)')
            # 16 KiB plans are synthetic; the current production history is much larger.
            connection.executemany('INSERT INTO apply_receipts(id,principal_id,target_namespace,operation_name,'
                'payload_hash,change_set,state,created_at,updated_at) '
                "VALUES(?,'agent','global','synthetic','hash',?,'applied','same','same')",
                ((str(index), b'x' * 16384) for index in range(args.receipts)))
            connection.commit()
            result = {'fixture': 'synthetic settled receipts, empty pending queue; warm OS cache',
                      'receipts': args.receipts, 'plan_bytes': 16384,
                      'before': evaluate(connection, PREVIOUS_QUERY, args.rounds)}
            for statement in SCHEMA:
                connection.execute(statement)
            connection.commit()
            result['after'] = evaluate(connection, RECOVER_PENDING, args.rounds)
            assert result['before']['result_sha256'] == result['after']['result_sha256']
        finally:
            connection.close()
    text = json.dumps(result, indent=2, sort_keys=True) + '\n'
    if args.output:
        args.output.write_text(text)
    sys.stdout.write(text)


if __name__ == '__main__':
    main()
