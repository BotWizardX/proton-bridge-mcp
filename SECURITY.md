# Security

## Reporting a problem

Please report security problems privately, using the repository's private
vulnerability reporting ("Report a vulnerability" on the Security tab), not a
public issue. Include what you found, how to reproduce it, and which version
or commit you used. Please do not include real mail, addresses or passwords.

## What counts as a security problem here

Anything that lets the server:

- send mail without a matching, fresh `prepare_send` review, or send the same
  review or approval id twice;
- send anything other than the reviewed plain text (other recipients, Bcc,
  attachments, a different From, a changed body);
- delete mail permanently, or move mail into or out of Drafts, Sent, Outbox,
  Scheduled or All Mail;
- log message content, addresses, subjects or passwords;
- reach Bridge anywhere other than 127.0.0.1, or skip STARTTLS outside the
  unit tests;
- let one profile run as, or send From, another profile's address.

## Known limits (not bugs)

See "Honest limits" in README.md. In short:

- `tap_ref` is a convention your MCP client must enforce. It is not
  authentication of a human. The approval id is cited by the calling agent
  and is not cryptographic proof that a human approved.
- Any process running as your OS user can start proton_mcp.py and call
  `send_draft` itself, or send mail straight through Bridge's SMTP port with
  the Bridge password from `pass`, skipping every guard here. Such a process
  can also read the state folder and the password store.
- The MCP client decides who may call the tools.
