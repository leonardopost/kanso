# Escalations

Append-only. kanso adds one line per escalation — misalignment, repeated certification
failure, promotability, live demotion, blocked deployment — and `kanso inbox` lists the
unread ones. Each entry is a single unchecked-checkbox line carrying an id, a timestamp,
the kind, its subject, a summary and the actions available.

`kanso inbox ack <id>` marks an entry read. Acknowledging is never an approval: live
capital moves only through `kanso promote <strategy> --live --as <your name>`.
