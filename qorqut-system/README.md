# qorqut-system

The one package that touches the operating system: private files, locks, processes, local channels, programs and
places.

**Rule 1.** Feature code makes no system-specific call. It calls this package.

## What is here

| Module | Operations |
|---|---|
| `qorqut_system.private_files` | open a private folder (create or not, following a final link only when the caller asks), and a folder inside one; make a folder private (with its parents when asked); inside it read a file with a size limit, create a new file, create one and stream it, replace a file, open for append, make sure one exists, open a file as a stream the caller keeps (also when it is already there), open a file with the caller's own flags, remove, stat, list, make and remove a folder, rename a name (also one that refuses an existing target), link a name (also following a link at its source); "is private and mine"; the permission bits, the owner, and the user and group of a stat; set private; the user's own name; sync a folder; make and read a symbolic link, also inside an opened folder |
| `qorqut_system.locks` | open a lock file and check it, or lock a file the caller opened; exclusively or shared, waiting or failing at once |
| `qorqut_system.atomic` | replace one small file atomically |
| `qorqut_system.publication` | publish a fully written file under a new name, exclusively |
| `qorqut_system.retained` | address an already-open file where the system allows it |
| `qorqut_system.identity` | compare a saved file identity with the file on disk |
| `qorqut_system.processes` | start in the background; identity that survives a reused process id; command line, name, program, folder, parent, owner and process group of a process; the files it holds open and whether it holds a write lock on one; list processes; stop one identified process; one process asked to stop or killed by its id; stop a process with everything it started, gently then by force, also when the process itself has gone; a group asked to stop or killed by its id; ignore a hangup; watch a child's exit without reaping it; boot and machine identity |

A folder opened once is passed on as a `Folder` object, and a lock file as a `LockFile`, so no descriptor number
crosses a function boundary and the Windows version can hold a path instead.

Each operation has a Unix version, shared by Linux and Mac, and a first Windows version. The Unix versions hold
today's calls, moved rather than rewritten. The Windows versions are a beginning: they are not shipped, not hardened
and not run by the checks yet. There the channel is a named pipe that only its owner can open and carries the same
control token, and a launcher keeps its name and its text, because the package ships the shell that runs it.

Not verified on Windows: the launcher with Codex, because no Codex model turn has run on that system, and a launcher
typed into Claude Code's PowerShell tool, which a session on Windows turns off.

The server's old helpers keep their names for the files that still import them and pass on to this package.

The package depends on no other first-party package. `qorqut_system.errors` holds the errors the layer raises;
feature code translates them to its own error type so callers keep the type they saw before a move.
