# Security

## Reporting a vulnerability

Please report privately through GitHub:
**[Security → Report a vulnerability](https://github.com/davidboulay/lightspeed-xseries-mcp/security/advisories/new)**.

Don't open a public issue for anything exploitable. Expect a first response
within about a week. This is a single-maintainer project.

## What counts

The server's one promise is that it **never changes a Lightspeed account**.
Anything that breaks that promise is a vulnerability, for example:

- a tool, argument or path that makes the server send a request other than a
  `GET`, or a `POST` to `/inventory` or `/inventory_levels`;
- a way around the `api_get` path checks or its deny list (`/webhooks`,
  `/partner`, `/channel_requests`, `/security_events`);
- the token leaking into tool output, error messages or logs.

## What it doesn't protect

- **The token itself.** A Lightspeed personal token has no scopes. Anyone who
  holds it can do anything its user can, through the API directly. Keep it out
  of shared config, and revoke it in *Setup → Personal tokens* if it leaks.
- **Who can call the tools.** Over stdio, that's whoever runs the client. When
  hosted, it's whoever your auth proxy lets in. Read access includes customer
  contact details and cost prices.
