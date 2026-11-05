"""Install the CRM Domain contract double while the real ``qorqut_operations.crm`` package is absent from this source group.

The host composes that Domain package. Until the reviewed Domain commit is integrated, the double (see
``crm_domain_double``) stands in for it so the host's own behavior can be tested; with the real package importable this is
a no-op. It must run before any ``bazarlyk`` module is imported, which is why it lives in the root conftest."""

import crm_domain_double

crm_domain_double.install()
