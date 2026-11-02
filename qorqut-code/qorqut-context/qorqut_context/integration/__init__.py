"""Context's integration with the Operations platform: the queue, filing, graph-edit and usage storage and operations, the
connected backend (driver, loop, service, recovery) and the feature declarations a host splices into its composition.

This package is the only part of ``qorqut_context`` that imports ``qorqut_operations`` (the ``integration`` extra). The base
Context engine never imports it, and this initializer imports nothing: import the submodule you need
(``qorqut_context.integration.queue``, ``.registration``, ``.service``, ...)."""
