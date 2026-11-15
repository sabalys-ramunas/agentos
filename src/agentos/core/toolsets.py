"""The toolsets table: the node's tools in named groups (agentos.mcp.toolsets serves them).

It sits in core because node.toml's `mcp_toolsets` is checked against these names when the config
loads (agentos.core.config), `agentos connect` reads them without the MCP server, and the server
decides with them what one connection is shown. How a tool lands in a toolset, and what a
connection is shown, is in agentos.mcp.toolsets.
"""

from __future__ import annotations

from dataclasses import dataclass

CORE = "core"
FULL = "full"
OTHER = "other"
FULL_ALIASES = frozenset({FULL, "all"})


@dataclass(frozen=True)
class Toolset:
    name: str
    summary: str  # one short line an agent reads in tools_more's description
    tools: tuple[str, ...] = ()
    tags: tuple[str, ...] = ()  # a tool no toolset names lands in the first toolset sharing a tag
    hint: str = ""  # a few words for tools_more's description, when the name alone does not say it


TOOLSETS: tuple[Toolset, ...] = (
    # core: what the howto recipes and the server instructions need for the everyday jobs (a post
    # with an image, a vault login, a hand-off to the human, waiting for an approval or a run),
    # so an agent does those without loading anything. approval_confirm rather than approval_status:
    # an approval_pending result names it, and approval_wait already returns the status. Everything else is one tools_more away.
    Toolset(CORE, "the everyday lane loop: find and open a lane, look, act, wait, upload, log in, ask a person", (
        "whoami", "howto", "lanes_list", "lane_open", "lane_close", "lane_claim", "lane_release",
        "lane_view", "lane_wait_control", "browser_navigate", "browser_snapshot", "browser_act",
        "browser_wait", "browser_screenshot", "browser_tabs", "browser_dialog", "browser_dialog_answer",
        "browser_upload", "files_put", "login", "approval_wait", "approval_confirm", "human_ask",
        "memory_context", "run_status", "environment_open", "environment", "environment_close",
        # an environment's terminal: what nearly every chat with one runs, so it needs no tools_more
        # (about 830 tokens here, against 3,450 for the whole terminals toolset in such a chat)
        "lane_exec", "lane_job_start", "lane_job_read", "lane_job_cancel",
        # its files: coding is what an environment is for, and without these an agent writes files
        # through shell quoting (heredocs, sed), which breaks on the first quote in the code
        "lane_file_read", "lane_file_write", "lane_file_edit",
    )),
    Toolset("files", "more for a browser lane: its files (inbox and downloads), switching tabs, scrolling a feed", (
        "files_get", "files_list", "files_delete", "browser_tab", "browser_scroll",
    ), ("files",), "files, tabs, scrolling"),
    Toolset("devtools", "why a page broke: its console messages and page errors, its requests and which "
            "failed, and one response's body", (
        "browser_console", "browser_network",
    ), ("devtools",)),
    Toolset("control", "sharing lanes: who has one, asking a person for it, the queue, a safety check, a freeze", (
        "lane_control", "lane_request_control", "lane_queue", "lane_check", "lane_freeze",
    ), ("control",), "who has a lane, asking for it, queue, freeze"),
    Toolset("vault", "stored logins: what the vault holds, filling one login field, asking for a log in", (
        "vault_list", "vault_fill", "lane_login_request",
    ), ("vault",), "stored logins"),
    Toolset("flows", "saved flows and their runs: list, read, run, record, save, cancel, Ask me first", (
        "flows_list", "flow_get", "flow_run", "flow_save", "flow_record_start", "flow_record_stop", "run_cancel",
        "ask_first_on",
    ), ("flows",), "saved flows and runs"),
    Toolset("workflows", "routines (workflows): a trigger such as a schedule, a webhook or an email, then "
            "steps; save, propose, run, retry, and the leads and opt-outs their mail steps use", (
        "workflow_catalog", "workflow_list", "workflow_get", "workflow_save", "workflow_propose",
        "workflow_run", "workflow_runs", "workflow_retry", "leads_propose", "send_suppress",
    ), ("workflows",), "routines on a schedule or a trigger"),
    Toolset("connectors", "installed connector operations, one tool per site operation (like "
            "instagram_post_photo), and their health", (
        "connectors_list", "connector_status",
    ), ("connectors",), "site operations"),
    Toolset("site_apis", "a site's data as typed endpoints: find what a page repeats, try a draft, ask a "
            "person to turn it on, list them", (
        "site_api_discover", "site_api_try", "site_api_propose", "site_apis_list",
    ), ("site_apis",), "a page's data as an API"),
    Toolset("mail", "mailboxes: find yours, list, read, mark, send, and asking a person to connect one", (
        "mail_mailboxes", "mail_list", "mail_read", "mail_mark", "mail_send", "mail_connect",
    ), ("mail",)),
    Toolset("memory", "workspace memory: search, read, write and correct (forget erases, and needs an "
            "irreversible grant and the owner)", (
        "memory_search", "memory_get", "memory_write", "memory_forget",
    ), ("memory",)),
    Toolset("profiles", "who the work is for: a person's or business's bios and voice, a product's brand and "
            "rules (profile_use)", (
        "profile_list", "profile_get", "profile_use", "profile_propose", "profile_save", "profile_update",
        "profile_attach", "profile_detach", "profile_sync_status", "profile_mark_synced", "profile_photo_put",
        "profile_learn_start", "profile_learn_finish", "profile_learn_brand",
    ), ("profiles",)),
    Toolset("studio", "is a page all right: ui_check (layout, contrast, targets, names, health, design system), "
            "ui_diff (what a change did), system_check, baselines, a finding or an image on request", (
        "ui_check", "ui_diff", "system_check", "ui_baseline", "finding_get", "ui_image", "studio_status",
    ), ("studio",), "check a page you build"),
    Toolset("workspaces", "workspaces: list, plan and apply changes, lane readiness", (
        "workspace_list", "workspace_queue", "workspace_plan", "workspace_apply", "lane_set_readiness",
    ), ("workspaces",)),
    Toolset("worker", "the node's workers: hand a browser job to the cheap worker on the node (lane_do), and "
            "which workers are on", (
        "lane_do", "plugins_list",
    ), ("plugins",), "hand a browser job to the node"),
    Toolset("admin", "your grant, an approval's status without waiting (or all yours still waiting), and the call trail", (
        "grant_info", "approval_status", "traces_query",
    ), ("policy", "approvals", "traces"), "grant, approvals, call trail"),
    Toolset("terminals", "terminal and service lanes: shells, commands, background jobs, synced files, installs, "
            "images, disposable cloud VMs", (
        "env_create", "env_lanes_list", "env_remove", "lane_install", "lane_upload_check",
        "lane_upload", "lane_jobs_list", "shell_open",
        "shell_read", "shell_write", "shell_close", "service_start", "service_stop", "service_status",
        "service_logs", "image_build", "images_list", "vm_create", "vm_destroy", "vm_extend", "vm_clone",
        "vms_list", "environment_upload",
    ), ("env",), "shells, commands, services"),
    Toolset("brains", "agent tasks on the node's brains: run, follow, resume, cancel; the git remotes they reach", (
        "brains_list", "brain_run", "brain_status", "brain_tail", "brain_tasks", "brain_resume",
        "brain_cancel", "brain_use", "git_remotes",
    ), ("brains", "git"), "agent tasks on the node"),
    Toolset("automation", "events, triggers and the agenda "
            "(planned one-off actions, their approval, runs and proof)", (
        "events_list", "triggers_list", "trigger_propose",
        "agenda_list", "agenda_get", "agenda_draft", "agenda_update", "agenda_propose", "agenda_cancel",
    ), ("events", "agenda"), "events, triggers, agenda"),
)
NAMES: tuple[str, ...] = tuple(t.name for t in TOOLSETS)
