"""The events layer: a durable queue between triggers and what they wake (runtime service
"event_queue"; "events" is the panel's SSE bus).

    models.Event, Route, Delivery, DeliveryState, Controls   the shared shapes
    ports.EventsPort                                          emit and delivery_finished
    frame.render                                              the wake message
    store.EventStore                                          events.db (SQLite WAL)
    service.EventService                                      emit, routes CRUD, the dispatcher loop
    dispatcher.Dispatcher                                     the controls of design section 4.3.6
    targets                                                   flow, brain and agent targets
    sources                                                   runs, lane holds, approvals, owner messages
    tools.tool_specs                                          events_list, triggers_list, trigger_propose

See README.md and docs/design/always-on-agents.md section 4.3.
"""
