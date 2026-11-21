"""The agenda: one timeline per workspace of planned actions of any kind (runtime service "agenda").

A post is one kind of planned action. So is an invoice reminder sent on the 1st, a domain
renewed before it lapses, a form submitted inside office hours, a report exported on Friday,
or one agent task woken on a given day. Each entry names something openajar can already run
(a saved flow, a connector operation, or a trigger that wakes a flow, a brain or an agent),
when it should run, and goes through the node's own approvals before it may.

    service.build(runtime)       -> AgendaService (registers "agenda"; the runner's scheduler ticks it)
    tools.tool_specs(runtime)    -> agenda_list, agenda_get, agenda_draft, agenda_update, agenda_propose,
                                    agenda_cancel (toolset "agenda"; approving is not an agent tool)
    cli.app                      -> `openajar agenda list|show|approve|cancel|...`
    api.routes                   -> /api/agenda*, mounted by openajar.api.mounts

See docs/AGENDA.md.
"""
