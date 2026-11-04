# mailarchive (spike)

Incrementally mirrors a read-only IMAP mailbox into an [m2dir]-format mail
store on disk. A standalone Go module — it never links into the live Cloudreve
server, so it can't affect it. First backend is IMAP (tested against Gmail);
JMAP (`rockorager/go-jmap`) is the planned second behind the same `MailSource`
interface.

## Why m2dir

Emails land as plain files, one per message, so the on-disk tree *is* the
backup (restore is `cp`). Layout follows the spec: a `.m2store` marker at the
root, one folder per remote mailbox, each a `.m2dir` with a `.meta/` sidecar
for flags. Filenames are `YYYY-MM-DD_hh:mm_<from>,<checksum>.<nonce>`, where the
checksum is content-derived so re-runs are idempotent.

## Run

Gmail needs an [app password] (account with 2FA), not your normal password.

```sh
export IMAP_USER="you@gmail.com"
export IMAP_PASS="xxxx xxxx xxxx xxxx"   # app password

go run . -store ./mail -mailbox INBOX
# everything Gmail has, across all labels:
go run . -store ./mail -mailbox "[Gmail]/All Mail"
```

State (per-mailbox UIDVALIDITY + last UID) persists in `./mail/.sync-state.json`;
re-run to pull only what's new.

## Scope of the spike

- **Read-only.** Selects with EXAMINE and fetches `BODY.PEEK[]`, so the remote
  `\Seen` state is never touched.
- **New messages only.** Gmail has no QRESYNC, so this is UID-incremental;
  detecting flag changes or deletions on already-archived mail is future work
  (CONDSTORE where available, else periodic reconcile).
- **One pass per invocation.** Scheduling (cron / Cloudreve's queue) is left to
  the caller.

[m2dir]: https://man.sr.ht/~bitfehler/m2dir/
[app password]: https://support.google.com/accounts/answer/185833
