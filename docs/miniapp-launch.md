# Mini App launch and login diagnostics

A Reply Keyboard `KeyboardButton.web_app` can open the application without
providing signed initialization data. It is a different entry point from a
`MenuButtonWebApp`, an inline Web App button, or a configured Main Mini App.
Calling `/start` or `/menu` again does not change the launch type of a reply
keyboard button. See Telegram's [initialization data specification](https://core.telegram.org/bots/webapps#webappinitdata)
and [menu button documentation](https://core.telegram.org/bots/webapps#launching-mini-apps-from-the-menu-button).

For authenticated actions, configure a supported entry point with the same
HTTPS address as the application. Keep the reply keyboard if desired; its
page can explain how to open the authenticated entry point. Do not fabricate
signed data, trust `initDataUnsafe`, or use client-supplied user IDs for login.

The frontend exposes an expandable local diagnostic on launch/login failure.
It contains only a fixed category, request stage and endpoint, HTTP status when available,
and three presence booleans. It does not log, persist, or transmit diagnostics.
No raw initialization data, URL fragments, session tokens, account identifiers,
SDK objects, or backend response bodies are included.

| Category | Meaning |
| --- | --- |
| `sdk_unavailable` | The adapter could not find the official SDK object. |
| `init_data_missing` | The SDK is present but has no signed initialization string. |
| `auth_rejected` | The auth endpoint returned HTTP 401 for a nonempty proof. |
| `auth_payload_rejected` | The auth endpoint rejected the request shape with HTTP 422. |
| `session_expired` | A fresh Telegram launch is needed; an already exchanged proof is not replayed. |
| `http_error` | An HTTP response was received, but the request or response failed. |
| `invalid_json` | A successful HTTP response could not be decoded as JSON; its real HTTP status is retained. |
| `invalid_response` | The decoded JSON did not match the expected endpoint schema. |
| `request_timeout` | The client's existing deadline expired, including while reading the response body. |
| `transport_or_cors_error` | Fetch failed; DNS, TLS, offline, timeout and CORS cannot reliably be separated by frontend code. |
| `unexpected_error` | An unrecognized failure occurred; its message is not exposed. |

Presence of initialization data is not proof of validity. Only the backend
validates its signature, age and identity. A 401 alone cannot distinguish a bad
signature, expired data, or a mismatch between the launching bot and backend
token. A Telegram platform hint is not authentication or proof of launch type.

The endpoint is a fixed label: `telegram_auth`, `me`, `access_status`, or
`access_request`. No arbitrary URL is included. This distinguishes an auth
failure from either protected request in the parallel initialization flow.
Schema validation errors retain their endpoint and are not reported as real
HTTP 502 responses. The HTTP status is omitted when it was not retained.

Before deployment, test SDK absence, a normal browser, a keyboard launch without
data, a signed launch, rejected auth, opaque fetch failure, HTTP errors, and
session expiration using synthetic fixtures. Verify that missing data causes no
auth request and that diagnostics never include fixture secrets. Check the
rendered React UI, TypeScript and production build as well.

After separately approving any Telegram configuration change, verify a real
supported launch on a device. Close the old window and open the native Web App
menu or configured Main Mini App; a plain HTTPS link inside Telegram is not a
substitute. Successful authentication will create normal session records.
Do not use production initialization data in terminal commands or screenshots.

## URL comparison and diagnostic releases

Compare scheme, hostname, effective port, path, query and fragment separately.
Report only equality flags and categories; do not include parameter values,
parameter names, private paths, or hashes derived from private parameters.
Origin is determined by scheme, host and port. A path or trailing slash does
not change origin, but can change routing or trigger a redirect. Confirm the
server's behavior before treating two URLs as equivalent. Existing query or
fragment data should not be removed automatically during diagnosis.

With a root-relative `/api` base, changing a Mini App's path on the same origin
does not change its API endpoint origin. A URL mismatch alone does not prove
that Telegram initialization data or server-side authentication is broken.
Check any known private-chat menu separately with `getChatMenuButton`; a default
menu check does not enumerate all per-chat settings. Avoid exposing chat IDs.

Publish a diagnostic UI only after approval, as an independently verified static
release with a SHA-256 manifest. Keep application data, secrets and private
reports outside the artifact. Preserve the old release and its immutable asset
URLs, verify the new files, and switch the frontend pointer atomically. Record
the previous target in protected recovery metadata. Rollback should verify both
the current target and the preserved release, without replacing the database or
restarting application services.

Keep the initial preflight read-only and run it before transferring deployment
files. Record configuration fingerprints and service identities without exposing
environment values. A staging/upload step writes files even if it ends with a
read-only release check, so describe and authorize it separately. Before the
atomic switch, compare the recorded configuration and service guard again.
Verify the published HTML and immutable assets over HTTPS by hash; an expected
401 without a session is a useful negative check, not a failed login test.
Stop on drift or failed checks rather than retrying or rolling back blindly.

## Native fetch receiver

When storing a browser-native `fetch` in a client field, bind it to the global
object: `this.fetcher = fetcher.bind(globalThis)`. Calling an unbound native
function as `this.fetcher(...)` otherwise passes the client object as its
receiver. A browser can reject this with a TypeError before sending any HTTP
request, which looks like a transport failure if the original exception is
sanitized. Do not loosen CORS to work around a receiver error.

The regression tests include a receiver-checking fetch double and an opt-in real
Firefox check using the actual ApiClient, an isolated temporary browser profile,
a loopback server, and a synthetic proof. It checks that native fetch works,
an object receiver is rejected, and the fixed client reaches a readable auth401.
The harness does not load a personal browser profile, install dependencies,
disable browser sandboxing, or contact production/Telegram.

```bash
python -B frontend/tests/browser-fetch-receiver.py --expect http401
```

This optional test requires installed Firefox and the frontend's existing
esbuild dependency. See [Function.bind](https://developer.mozilla.org/en-US/docs/Web/JavaScript/Reference/Global_Objects/Function/bind)
for receiver binding semantics. Real device verification remains a separate
step; use synthetic requests and status/origin-match/exception-category metadata,
never initialization strings, session tokens or request/response body exports.
