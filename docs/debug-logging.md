# Browser debug logging

The browser installs [`debugLog.ts`](../src/utils/debugLog.ts) before analytics
and React mount in [`main.tsx`](../src/main.tsx). It records console output,
uncaught errors, and global `fetch` calls, including each physical API retry.
Network rows include the method, URL, HTTP status, time to response headers,
and echoed request ID. An HTTP 200 stays a network success even if the caller
cannot parse its JSON; rejected requests record network, timeout, or abort
metadata without a response body.

## Capture modes

Open the overlay with Ctrl+Shift+D, the bottom-left corner hotspot, or
`?debug=1`. Its **Network capture** selector applies to future requests:

| Choice | Request bodies | Response bodies |
| --- | --- | --- |
| Metadata | Omitted | Omitted |
| Responses (default) | Omitted | Redacted and bounded |
| Requests + responses | Redacted and bounded | Redacted and bounded |

Capture runs while the overlay is closed. Each request snapshots its mode at
start: changing modes does not alter in-flight requests or capture old responses
retroactively. Closing/reopening the overlay and **Clear** preserve the mode.
Changing mode leaves captured history intact until Clear or buffer eviction.

Response content attaches asynchronously to the original network row. It is
visible in the overlay and included in text searches and **Copy**, which copies
only the currently filtered entries. The caller receives the original response
without waiting for capture; logging reads a clone. Requests made with a
`Request` object still have response capture, but request-body extraction reads
only `init.body`, never the `Request` stream.

## Preferences and migration

Initialization uses the first recognized source in this order:

1. `debugcapture=metadata|responses|bodies` in the URL.
2. Legacy URL `debugbody=0` (Metadata) or `debugbody=1` (Requests + responses).
3. `gr.debugCaptureMode` in local storage, using the same three mode values.
4. Legacy local storage `gr.debugBody=1` (Requests + responses).
5. Responses.

Invalid values fall through. Explicit URL choices and migrated legacy choices
are persisted under `gr.debugCaptureMode`; the legacy key is removed only after
the new write succeeds. Every selector choice is persisted explicitly, including
Metadata, so opting out survives reload. Remove an explicit capture parameter
from the URL if you want a later selector choice to govern the next reload.
`debug=1` affects visibility only.

The old off setter removed `gr.debugBody`, so an old opt-out cannot be
distinguished from a browser that was never configured. Both adopt Responses
unless an explicit Metadata preference is set. Unavailable storage does not
break fetch: a URL or runtime choice still applies in memory, otherwise the
default applies. Persistence requires working local storage.

## Bounds, persistence, and redaction

The store keeps 500 entries in memory and mirrors the newest 200 to
`gr.debugLog` in local storage. Successful requests and aborts use a 500 ms write
debounce; other failures persist immediately, including another immediate write
when an HTTP error's redacted response arrives. Page exit flushes pending logs.
Clear removes stored history; later activity can create new entries. Delayed
body capture cannot restore cleared or evicted rows.

Response reads are capped at 8,000 bytes; a known larger Content-Length skips
cloning entirely. Incomplete content at the cap gets a safe note instead of a
raw prefix. Displayed bodies are limited to 2,000 characters plus a truncation
marker, after redaction. Unreadable bodies get a note; responses that cannot be
cloned keep metadata only. Non-string request bodies use a type placeholder.

Body redaction hides sensitive JSON keys and assignment-shaped secrets, scrubs
JWT/Bearer/email patterns, and suppresses unparseable text with sensitive-key
markers. Headers are not captured apart from the echoed request ID. This is
best-effort redaction: usernames and session IDs remain useful debugging
identifiers, and arbitrary payloads can still contain personal data. Console
arguments, errors, and URL text do not use body redaction. Review copied logs
before sharing them. Release gating and broader console redaction remain
separate concerns documented in the source.

Behavior is covered by [`debugLog.test.ts`](../src/utils/debugLog.test.ts) and
[`DebugOverlay.test.tsx`](../src/components/DebugOverlay.test.tsx).
