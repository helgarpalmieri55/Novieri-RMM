"""Business modules. Each registers into the kernel (job types, API routes)
via register_all() — the kernel never imports module internals directly."""

from fastapi import FastAPI


def register_all(app: FastAPI) -> None:
    from app.modules import (
        agent_distribution,
        agent_push,
        agent_update,
        ai_admin,
        ai_copilot,
        ai_portal,
        backup,
        bcp,
        budget,
        change_mgmt,
        deploy,
        documentation,
        email_intake,
        health,
        inventory,
        ipam,
        licenses,
        notification,
        patch,
        people,
        portal,
        provisioning,
        remote,
        reporting,
        security_logs,
        service_desk,
        site_server,
        sla,
        subscriptions,
    )

    agent_distribution.register(app)
    # Push install, cycle B1: the control surface only. Registers routes and
    # NO job types -- the module dispatches nothing, by design (spec §14), and
    # a `register_job_types()` here would break both kernel-gate partition
    # tests, which is the alarm working rather than a nuisance.
    agent_push.register(app)
    agent_update.register(app)
    inventory.register(app)
    # After `inventory`: IPAM registers a hook into inventory's post-ingest
    # extension point, so inventory's module must have been imported first.
    # The ordering is a readability choice, not a correctness one -- the hook
    # list is consulted at ingest time, not at registration time.
    ipam.register(app)
    security_logs.register(app)
    documentation.register(app)
    remote.register(app)
    patch.register(app)
    deploy.register(app)
    backup.register(app)
    provisioning.register(app)
    change_mgmt.register(app)
    bcp.register(app)
    licenses.register(app)
    subscriptions.register(app)
    budget.register(app)
    service_desk.register(app)
    notification.register(app)
    portal.register(app)
    email_intake.register(app)
    # After `portal`: the tier-1 resolver mounts under `/api/v1/portal/ai` and
    # its executor calls the portal's OWN routes through the loopback, so those
    # routes must exist on the app it is handed. A separate package for the same
    # reason the copilot is one (docs/modules/ai-automation.md §0.2): the portal
    # is a customer-facing surface, and it does not import `app.core.ai`.
    ai_portal.register(app)
    # Site servers: a promoted `Device` plus one registry row (§0.1). Registers
    # the `site.wake` job type here for the API process; the GATEWAY process
    # registers it separately in `register_module_job_types`, which is where
    # results complete.
    site_server.register(app)
    # After `service_desk`: the health module's alert->ticket action calls the
    # ticket core (`tickets.create_ticket`, `transitions.change_status`) and its
    # org settings hold a `ticket_queue`/`ticket_category` FK. As with `sla`
    # below, registration order does not affect route matching -- health mounts
    # under `/api/v1/health`, `/api/v1/devices/{id}/health` and
    # `/api/v1/approved-actions`, none of which the ticket core claims -- so this
    # follows the module dependency rather than creating one.
    health.register(app)
    # After `service_desk`: the technician copilot mounts under
    # `/api/v1/service-desk/tickets/{ticket_id}/ai` and reads `ticket.device_id`.
    # It is a SEPARATE PACKAGE rather than an addition to the ticket core
    # (docs/modules/ai-automation.md §0.3): making `service_desk` import
    # `app.core.ai` would put an AI dependency underneath SLA, email intake, the
    # portal and reporting. The dependency runs this way only.
    ai_copilot.register(app)
    # After `service_desk`: the SLA module's per-ticket route lives under
    # `/api/v1/tickets/{ticket_id}/sla`, and its admin routes read the ticket
    # core's queues. Registration order does not affect matching (the paths do
    # not overlap -- the ticket core mounts under `/api/v1/service-desk`), so
    # this follows the module dependency rather than creating one.
    sla.register(app)
    # People reads devices, seats, licences and tickets, and hooks into
    # inventory's post-ingest point; all of those are registered above.
    people.register(app)
    # After every module whose TABLES it reads (devices/agents are kernel;
    # health, patch, backup, service_desk and sla are above). Reporting imports
    # no module's CODE, only its models, so this ordering follows the data
    # dependency rather than creating one -- a reader looking for "what does
    # reporting read" finds it already registered above.
    #
    # `/api/v1/reports` IS NOT AN UNCLAIMED PREFIX, whatever spec §6 implies.
    # `security_logs` (registered near the top of this function) already mounts
    # `GET /api/v1/reports/access.csv`. Nothing collides today -- report paths
    # are one literal segment per definition key, and `registry.register()`
    # refuses a key containing `/` -- and `tests/test_reporting_runner.py`
    # asserts EXACTLY ONE route claims each report path, so a future definition
    # keyed `access.csv` is a red test rather than two routes at one path with
    # FastAPI silently serving whichever registered first.
    #
    # This call is what BUILDS the report routes: importing
    # `app.modules.reporting.api` registers every definition and runs
    # `verify_registry()`, so a definition whose SQL reads a table it did not
    # declare fails HERE, at application start, and the process does not come up.
    reporting.register(app)
    # Last, and it depends on every module above it -- not for registration
    # order (its routes live under `/api/v1/ai`, which nothing else claims) but
    # because the AI agent's tool registry names OTHER MODULES' `GET` routes and
    # resolves them against the live router at describe time. Registering it
    # after everything else keeps that resolution honest in the one process
    # where a missing route would otherwise be a tool with an empty description.
    ai_admin.register(app)
