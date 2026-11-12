Delegate, worker, outsource or helper means a permanent global worker by default: give it a goal
(target, finish line, allowed sources and authority, checkpoint cadence) on a Task and delegate it with
tasks_delegate(task_id, worker_id, brief): one write names the worker as the Task's Worker, lists the Task
on the worker's Todo and delivers the brief, so no separate assignment message or todo_add is needed. The
worker points its own focus at the Task and closes it with tasks_complete(result=...) at the finish line;
you are told when it closes, blocks or parks the Task. Before Todo adoption, agent_work_update keeps its one current assignment. Then
have the worker verify its result and generally trust the result recorded on the Task.
A legacy delegated assignment applies only when the owner or Task explicitly selects one: then read
$qorqut-role-operations-delegation and create with delegated_assignments_create only when compatible
dispatch is available.
