"""Validate request bodies the dashboard sent against the real Operations descriptors (stub collaborators only).

    GLOBAL_REQUESTS_OUT=/tmp/g.json node tests/test_global_tasks_browser.js   # records [{op, body}]
    PYTHONPATH=<qorqut-operations>:<jsonschema> python3 tests/validate_global_requests.py <filtered.json>
Operations that are not Task/Action/tag descriptors (private conversations, section G reads) must be filtered out first."""
import json, sys
from unittest.mock import MagicMock
from jsonschema import Draft202012Validator
from qorqut_operations.tasks.operations import TaskOperations
from qorqut_operations.actions.operations import ActionOperations
from qorqut_operations.tags import TagOperations
t=object.__new__(TaskOperations); t.application=MagicMock(); t.items=MagicMock(); t.ports=MagicMock(); t.clock=None
t.note_ref_types=["task"]; t.overview=MagicMock(); t.ports.focus.schema={"display_name":{"type":"string"}}; t.ports.references.schema={"type":"array"}; t.ports.references.types=("task",)
a=object.__new__(ActionOperations); a.application=MagicMock(); a.clock=None
a.references=MagicMock(); a.references.schema={"type":"array"}
ds={}
for o in (t,a,TagOperations()):
    for d in o.descriptors():
        if hasattr(d,'input_schema') and isinstance(getattr(d,'name',None),str): ds[d.name]=d
bad=0
for c in json.load(open(sys.argv[1])):
    d=ds.get(c['op'])
    if not d: print('NO DESCRIPTOR',c['op']); bad+=1; continue
    body={k:v for k,v in c['body'].items() if k not in('idempotency_key','expected_revision')}
    errs=[e.message for e in Draft202012Validator(dict(d.input_schema)).iter_errors(body)]
    print('OK ' if not errs else 'BAD',c['op'],sorted(body),errs[:2]); bad+=bool(errs)
sys.exit(1 if bad else 0)
