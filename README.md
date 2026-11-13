# proton-bridge-mcp

A small MCP server (stdio) that lets an AI agent read and organize Proton Mail
through a local [Proton Mail Bridge](https://proton.me/mail/bridge), save
drafts, and send a draft only after a person has reviewed it and approved it.

It is one Python file using only the standard library, plus three shell
scripts to run Bridge, watch disk use, and roll everything back.

## What it does

Eight tools, per mail profile:

| Tool | What it does |
|---|---|
| `list_folders` | Lists folders and labels (labels show under `Labels/`). |
| `search` | Searches one folder; returns headers only (uid, date, from, to, cc, subject), newest first, at most 50. |
| `read_message` | Reads one message as text. Attachments are listed by name, never opened. |
| `move_message` | Moves messages to a folder, Archive, Spam or Trash. |
| `set_label` | Adds a label (an IMAP copy into `Labels/<name>`). |
| `save_draft` | Saves a plain-text draft to Drafts. Never sends. |
| `prepare_send` | Step 1 of a send: checks one draft and returns the full review text and its hash. Sends nothing. |
| `send_draft` | Step 2: sends that exact reviewed draft once, after a person approved it. |

## What it deliberately cannot do

- **No permanent delete.** There is no EXPUNGE, no `\Deleted` flag, no STORE,
  no CLOSE and no folder delete anywhere in the code (the tests check the
  source for this). Trash is as far as mail can go.
- **No moving into or out of** Drafts, Sent, Outbox, Scheduled or All Mail
  (the folder blocklist).
- **No attachments** on sends, and **no HTML**: a send is one plain-text part.
- **No Bcc**, no hidden or direction-changing Unicode characters, no
  non-ASCII (lookalike) addresses, at most 20 recipients.
- **No send without a fresh `prepare_send` review of that exact draft** (the
  tool cannot tell whether a person read it); no send from outside Drafts; no
  send of a draft that changed after its review.
- **No network reach**: it talks only to Bridge on `127.0.0.1` (IMAP 1143 by
  default, set with `PROTON_MCP_IMAP_PORT`; SMTP 1025 fixed), with STARTTLS pinned to Bridge's own certificate.
- **No content in logs**: logs hold times, tool names, counts, results and
  message uids only. Never subjects, addresses, bodies or passwords.

## Safety design

**Split mode.** Run one server process per mail profile
(`proton_mcp.py --profile a`, `--profile b`). Each process logs in as one
address only, keeps its logs, errors and pending reviews in its own folder,
and refuses to run as, or draft or send From, any other profile's address (or
any domain listed in its `refuse_domains`). If two addresses live on one
Proton account, turn on Bridge's split address mode so each address gets its
own IMAP login and password.

**Folder blocklist.** `move_message` refuses Drafts, Sent, Outbox, Scheduled,
All Mail and any other "All ..." folder, as source or target. It needs the
server's IMAP MOVE; there is no copy-and-delete fallback.

**Disk guard.** Bridge keeps a local cache (gluon) and does not check free
space. `disk_guard.sh` runs once before Bridge starts and then every 60 seconds:
if the cache passes 5 GB or free space on `/` drops under 60 GB, it stops
Bridge. If it cannot measure, it treats that as a breach (fails closed).
After the first sync, `touch run/synced`; from then on an empty or missing
cache also counts as a breach. The limits are constants in the script; edit
them for your machine.

**Encrypted password store.** The Bridge mailbox password is read from
[`pass`](https://www.passwordstore.org/) (GPG-encrypted) at run time, each
time it is needed. It is never written to disk, logged or put in the config.

**Review, then approve, then send once.** `prepare_send` rebuilds the draft
from its parsed fields (From, To, Cc, Subject, reply headers, body, and the
profile's signature if one is set), hashes those fields, and returns the
review text with the hash. It writes a private review record (mode 600) that
is valid for 30 minutes. Show that review to the person who must approve the
send. Then `send_draft` needs the draft uid, the full hash and `tap_ref`, the
id of that person's approval (for example a message or button id in your
chat app). Before sending it, under a per-profile lock and a global lock:

- refuses if sending is off for the profile (`send_enabled`), the daily cap
  (10 attempts per profile per day) is reached, the draft was already sent,
  or the approval id was used before;
- claims and deletes the review record (one review, one attempt);
- refuses if the review is older than 30 minutes or the hash differs;
- reads the draft again and refuses if anything changed since the review.

**The tap ledger.** Every send attempt that ends as sent or as an error (not
a refusal) writes `<approval id> <profile> sent|error` to one shared ledger,
`<state_dir>/shared/TAPS-USED`, outside every profile's own folder. An
approval id in the ledger can never send again, from any profile. The ledger
folder must be mode 700 and the file mode 600, owned by you and not links;
otherwise, or if it is missing, every send is refused. The send path never
creates it: run `proton_mcp.py --init-ledger` once at setup.

**Rollback.** `rollback.sh stop` stops Bridge and the guard.
`rollback.sh full --confirm "<date time note>"` also removes Bridge, its
config, cache and data, the `proton-bridge` pass entries, Bridge's own pass
entries (the path in `run/bridge-pass-path`), the GPG key you made for it (by
exact fingerprint, from `run/gpg-key-fpr`) and Proton's debsig policy files,
and tells you to sign the Bridge session out in Proton's web settings. If
either `run/` file is missing or does not look right, that step is skipped
with a warning and you remove it by hand. It removes `~/.password-store` only
if no entries are left in it. Setup step 1 and 2 create both `run/` files.
Run it with no arguments to see the steps first.

## Setup

You need: Linux, a paid Proton Mail plan (Bridge is not available on the free
plan), Proton Mail Bridge 3.x, Python 3.9 or newer, `pass` and `gpg`, and
`tmux` for the scripts. The scripts use GNU `du`, `df` and `ss`.

Run these from the repo folder (the scripts keep their own files in `run/`).

1. **Make a GPG key and a pass store.** On Linux, Bridge keeps its own keys in
   `pass`, and this server reads the Bridge password from it, so set this up
   before you sign in to Bridge:

   ```sh
   gpg --quick-generate-key "proton-bridge-mcp local" default default never
   FPR=$(gpg --list-secret-keys --with-colons "proton-bridge-mcp local" | awk -F: '/^fpr/{print $10; exit}')
   pass init "$FPR"
   mkdir -p run && echo "$FPR" > run/gpg-key-fpr   # rollback.sh uses this exact fingerprint
   ```

   A passphrase on the key protects the store at rest; Bridge and the server
   then need `gpg-agent` to have it unlocked.
2. **Install Bridge** from Proton's site, verify the package signature, and
   sign in once with `./start.sh cli` (type `login`, follow the prompts,
   then `info` to see the IMAP/SMTP username and the Bridge password, then
   `exit`). Then `./start.sh start`. Run `pass ls` and write the folder
   Bridge created (a relative path such as `docker-credential-helpers/<id>`)
   to `run/bridge-pass-path`; rollback removes only that path.
3. **Store the Bridge password in pass** (not your Proton password):
   `pass insert proton-bridge/mailbox`. For a second profile with its own
   Bridge password, use a second entry and set `PROTON_MCP_PASS_ENTRY`.
4. **Write the profiles file** at `~/.config/proton-bridge-mcp/profiles.json`
   (or point `PROTON_MCP_CONFIG` at it), based on `profiles.example.json`, and
   `chmod 600` it. The server refuses a file that is a link, is not mode 600,
   or is not yours.

   | Key | Meaning |
   |---|---|
   | `state_dir` | Logs, errors, reviews and the ledger. Must be an absolute path when any profile has `send_enabled`; otherwise it defaults to `$XDG_STATE_HOME/proton-bridge-mcp`. |
   | `timezone` | IANA zone for log times and the daily-cap day. Default `UTC`. |
   | `profiles.<name>.username` | The Bridge login address for this profile. |
   | `from_name` | Optional. If set, every send is From exactly `"from_name" <username>`. One plain line: no quotes, `<`, `>`, backslash, control or hidden characters. |
   | `signature_file` | Optional. If set, the file's text is added to every send and covered by the review hash; sends stay off while it is missing or empty. |
   | `hide_login_in_review` | Default `true`: the review shows a placeholder instead of the login address. |
   | `send_enabled` | Default `false`. Must be `true` for `prepare_send` and `send_draft` to work. |
   | `refuse_domains` | Optional list of domains this profile must never use as From. |

   Every server for one mailbox account must use the same profiles file, so
   they all share one state folder and one tap ledger.

5. **Create the ledger** once: `python3 proton_mcp.py --init-ledger`.
6. **Add one MCP server per profile** to your client. Example (paths are
   placeholders):

   ```json
   {
     "mcpServers": {
       "proton-a": {
         "command": "python3",
         "args": ["/path/to/proton-bridge-mcp/proton_mcp.py", "--profile", "a"],
         "env": {"PROTON_MCP_CONFIG": "/path/to/profiles.json"}
       },
       "proton-b": {
         "command": "python3",
         "args": ["/path/to/proton-bridge-mcp/proton_mcp.py", "--profile", "b"],
         "env": {"PROTON_MCP_CONFIG": "/path/to/profiles.json",
                 "PROTON_MCP_PASS_ENTRY": "proton-bridge/mailbox-b"}
       }
     }
   }
   ```

Other environment settings: `PROTON_MCP_IMAP_PORT` (default 1143; `start.sh`
reads the same variable to check that Bridge is listening),
`PROTON_MCP_CERT` (default `~/.config/protonmail/bridge-v3/cert.pem`),
`PROTON_MCP_LOG_DIR` / `PROTON_MCP_ERR_DIR` (must stay inside the profile's
own folder), `PROTON_MCP_USERNAME` (optional; refused unless it equals the
configured address). The host, the SMTP port, the cap, the review time limit,
the folder blocklist and the Trash rule are code constants on purpose.

**Tests:** `python3 -m unittest discover tests`. They use local fake IMAP and
SMTP servers and stub `tmux`, `du`, `df`, `pass` and `gpg`; they never touch a
real Bridge, a real mailbox or a real password store.

## Honest limits

- **Paid plan.** Bridge needs a paid Proton plan, and Bridge must be running
  and signed in for any tool to work.
- **The approval id is not proof.** `tap_ref` is a convention your MCP client
  must enforce. It is not authentication of a human. `send_draft` records the
  approval id the calling agent gives it and refuses reuse (and the bare word
  `none`), but it cannot check that a person really approved. An agent that
  can call `send_draft` could invent a new id. The review hash, the 30-minute
  single-use review, the ledger, the daily cap and the content-free SEND-LOG
  limit and record the damage; the real gate is your MCP client's own
  confirmation step and who you let call the tool.
- **Same-user trust.** Any process running as your OS user can start
  proton_mcp.py and call `send_draft` itself, or send mail straight through
  Bridge's SMTP port with the Bridge password from `pass`, skipping every
  guard here. Split mode limits mistakes and keeps logs apart; it is not a
  wall against code running as the same OS user, which can also read the
  profiles file, the state folder and (with your GPG agent unlocked) the
  password store, or point `PROTON_MCP_CONFIG` somewhere else.
- **Restart to load changes.** Stdio servers are started by the MCP client.
  After you change the code or the profiles file, start a fresh client session
  (or restart the servers) before the change takes effect.
- **Bridge sync lag.** The first sync can take a long time on a large
  mailbox, and new mail, moves and sent copies can take a moment to show up.
  Bridge's first sync also hands out UIDs out of date order, so `search`
  sorts by arrival date, not UID.
- **An error after the claim is ambiguous.** If `send_draft` returns an
  error after it started the SMTP step, the mail may or may not have gone out.
  Check Sent before trying again; the approval id is used up either way.
- **The draft stays in Drafts** after a send. Move it to Trash in the Proton
  app; this tool cannot move mail out of Drafts.
- **Plain text only.** HTML mail is shown with tags stripped, which is rough.
  Bodies are cut at 100,000 characters when read.
- **Search** returns at most 50 results and accepts only one non-ASCII value
  per search.
- **Linux only** as written: the scripts assume the Bridge `.deb` layout,
  `tmux`, and GNU tools. The disk-guard limits suit a large disk; change them
  for yours.
- **Trash is not forever.** Mail moved to Trash follows your Proton Trash
  settings.
- **Bridge's crash reports.** `start.sh` runs Bridge in its normal mode,
  which sends crash reports to Proton. Check Bridge's own settings for crash
  reports and telemetry and set them the way you want; this project does not
  change them.
- **One Bridge per machine.** Bridge allows one running instance; `start.sh
  cli` stops the background one before sign-in.

## License

MIT. See `LICENSE`. This project uses only the Python standard library and
talks to Proton Mail Bridge, `pass` and `gpg` as separate programs; it
does not include or link their code.
