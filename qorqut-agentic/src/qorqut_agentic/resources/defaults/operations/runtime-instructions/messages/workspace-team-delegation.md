Delegate, worker, outsource or helper means a permanent global worker by default: give it one
atomic assignment (its Task when there is one), keep its current work accurate with agent_work_update,
have the worker verify its result and generally trust its completion report.
A legacy delegated assignment applies only when the owner or Task explicitly selects one: then read
$bazarlyk-role-operations-delegation and create with delegated_assignments_create only when compatible
dispatch is available.
