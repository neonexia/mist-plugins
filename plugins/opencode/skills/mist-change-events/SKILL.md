---
name: mist-change-events
description: React to changes in Mist - subscribe to a bucket folder and read new, changed and deleted objects from the change feed. Use when an agent must wait for another agent's output, watch an inbox folder, or trigger work when files arrive.
license: Apache-2.0
---

# Change events

A subscription watches a bucket folder you can read. Every write and delete there becomes an event in your feed:
bucket, key, action, size, etag, who did it, and a cursor. Events are metadata; read the object over S3.

Use it instead of polling `list-objects` in a loop.

## Subscribe

MCP:

```
create_subscription(bucket="my-bucket", prefix="inbox/", name="inbox-watch")
```

Python:

```python
sub = m.events.subscribe("my-bucket", "inbox/", name="inbox-watch")
print(sub["id"])
```

- Give a `name`: creating again with the same name returns the same subscription, so restarts are safe.
- Optional: `suffix=".json"` (only matching keys), `actions=["written"]` or `["deleted"]`, `include_self=true` (your own
  changes are skipped by default).
- You need read access to the whole folder (`403 not_readable` otherwise).
- Creating or deleting a subscription can take up to about 90 seconds. Wait for it.

## Read the feed (MCP)

```
feed(subscription_id="<id>", wait=20)
```

It returns as soon as events are ready, or after `wait` seconds (at most 25) with `events: []`. An empty batch is
normal: call again. The reply has `next_cursor`. Loop:

1. `feed(subscription_id, after=<next_cursor from the last call>, wait=20)`
2. Handle each event (for example, read `s3://<bucket>/<key>`).
3. Repeat with the new `next_cursor`.

Reading with `after=<cursor>` **acknowledges** everything before it. Without `after`, the feed resumes where you last
acknowledged, so after a restart you see again what you had not finished. `after="now"` skips the backlog;
`after="oldest"` starts at the oldest event Mist still keeps.

## Read the feed (Python)

```python
import os, mist

m = mist.Client(ca_bundle=os.environ.get("MIST_CA_BUNDLE"))
sub = m.events.subscribe("my-bucket", "inbox/", name="inbox-watch")
s3 = m.s3()
for ev in m.events.stream(sub["id"], on_expired="oldest"):
    if isinstance(ev, mist.Gap):           # the cursor aged out; re-list the folder to catch up
        continue
    if ev["action"] == "written":
        body = s3.get_object(Bucket=ev["bucket"], Key=ev["key"])["Body"].read()
        print("new", ev["key"], len(body), "bytes")
    else:
        print("deleted", ev["key"])
```

`stream()` long-polls in a loop, reconnects, acknowledges a batch only when you ask for the next event after it, and
skips ids it saw recently. One batch: `events, cursor = m.events.wait(sub["id"], wait=25)`.

## An event

```json
{"id": "...", "type": "object", "action": "written", "time": "2026-11-13T10:00:00Z",
 "bucket": "my-bucket", "key": "inbox/job-1.json",
 "object": {"size": 120, "etag": "...", "sequencer": "..."},
 "actor": {"iss": "https://idp.example.com", "id": "agent-7"},
 "cursor": "...", "subscription": "sub_..."}
```

## Rules for correct handlers

- **At least once.** An event can arrive twice. Make handling idempotent (deduplicate on `id`, or write results with a
  key derived from the input key).
- **In order per subscription**, not across subscriptions.
- Events are metadata only. The object may already be gone or changed when you read it: handle `NoSuchKey`.
- Keep the cursor, not a time.
- A subscription nobody reads for 7 days (default) is removed. Delete yours when done: `delete_subscription(id)`.

## Errors

| Error | Meaning | Do |
|---|---|---|
| `410 CursorExpired` | The cursor is older than what Mist keeps | Restart with `oldest_cursor` (or `now_cursor`) from the error, then re-list the folder |
| `403 SubscriptionSuspended` | You lost read access to the folder | It resumes by itself when access returns; ask the owner |
| `403 SubscriptionEventsOff` | The bucket's owner turned events off | Ask the owner (`m.buckets.set_events(bucket, True)`) |
| `403 not_readable` (create) | You cannot read the whole folder | Ask for a grant, or subscribe to a folder you can read |
| `409 SubscriptionLimit` | Too many subscriptions | Delete unused ones (`list_subscriptions`) |
| `503 SubscriptionNameBusy` / `ServiceUnavailable` | Busy or restarting | Wait a few seconds and repeat the same call |
