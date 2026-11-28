# qorqut-system

The one package that touches the operating system: private files, locks, processes, local channels, programs and
places.

**Rule 1.** Feature code makes no system-specific call. It calls this package.

The package depends on no other first-party package. On Linux and Mac it holds today's calls, moved rather than
rewritten. A first Windows version of each operation is added before feature files are converted to use it, so that
an operation's shape is known to fit Windows before many files depend on it.