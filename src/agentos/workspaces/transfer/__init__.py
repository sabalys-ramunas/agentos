"""Workspace transfer: one workspace into one encrypted file, and back in on another node.

    export    `agentos workspace export` and its API: lanes, the workspace record, vault
              entries, flows, schedules, grant definitions and memory, in the age format
    import_   `agentos workspace import`: verify, plan, then apply as one unit (rolled back on
              failure); lanes come in cold and existing lanes are never replaced
"""
