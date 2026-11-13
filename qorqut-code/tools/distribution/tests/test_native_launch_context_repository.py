"""An installation initialized before the Context store existed gets it created at launch; an existing store is left alone.

    python3.12 -m unittest discover -s tools/distribution/tests -v
"""
from pathlib import Path
import json
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import native_launch  # noqa: E402


class ContextRepositoryLaunchTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.instance = self.root / 'instance'
        (self.instance / 'config').mkdir(parents=True)
        self.record = {'state': 'initialized', 'instance_root': str(self.instance),
                       'components': {'app': '/prepared/app', 'context_engine': '/prepared/context'}}
        (self.instance / 'config/initialization-receipt.json').write_text(json.dumps(self.record))

    def initialize(self):
        with mock.patch.object(native_launch, 'run') as run:
            record = native_launch.initialize(self.root, {})
        return record, run

    def test_missing_store_is_created_by_the_explicit_command(self):
        record, run = self.initialize()
        self.assertEqual(record, self.record)
        run.assert_called_once()
        command = list(map(str, run.call_args.args[0]))
        self.assertEqual(command[:5], [str(self.root / 'runtime/venv/bin/python'), '-I', '-B', '-m', 'qorqut.bootstrap.context_repository'])
        self.assertIn(str(self.instance), command)
        self.assertIn('context_engine=/prepared/context', command)

    def test_existing_store_is_left_alone(self):
        (self.instance / 'operations/context-graph').mkdir(parents=True)
        _, run = self.initialize()
        run.assert_not_called()


if __name__ == '__main__':
    unittest.main()
