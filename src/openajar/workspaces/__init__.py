"""Workspaces, templates, bulk lane creation and the login queue (runtime service "workspaces").

    service.build(runtime)     -> WorkspaceService (registers "workspaces")
    tools.tool_specs(runtime)  -> workspace_list, workspace_queue, workspace_plan, workspace_apply,
                                  browser_set_readiness
    cli.app                    -> `openajar workspace ...`
    api.routes(runtime)        -> /api/workspaces*, /api/templates*, /api/lanes/{ws}/{name}/readiness
"""
