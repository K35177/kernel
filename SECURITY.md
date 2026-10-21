# Security policy

## Reporting a vulnerability

Please use GitHub's private vulnerability reporting for this repository. Do
not include bot tokens, Telegram init data, user identifiers, message content,
database files, or production logs in a public issue.

If private reporting is not available, open a public issue containing only a
request for a private contact channel. Do not disclose exploit details there.

## Supported version

Security fixes are applied to the latest revision of the default branch. This
portfolio repository does not provide long-term support for older revisions.

## Repository boundaries

The repository intentionally contains no production credentials, Telegram
user data, SQLite databases, deployment snapshots, or server inventory. The
files under `infra/` are examples and require an independent review before use
on a real host.
