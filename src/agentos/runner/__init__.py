"""Durable runs, flows, proof of done, schedules and triggers (runtime services "runner"
and "flows").

    service.build(runtime)       -> RunnerService (registers "runner" and "flows")
    tools.tool_specs(runtime)    -> static tools + one typed flow_<name> tool per saved flow
    cli.app / cli.apps           -> `agentos runs|flows|schedules|watches ...`
    api.routes / public_routes   -> /api/runs*, /api/flows*, /api/schedules*, /api/hooks/{token}
    driver.FlowDriver            -> what the node implements on top of human input
"""
