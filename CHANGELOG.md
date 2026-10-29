# Changelog

All notable changes to the `hermes-rine` plugin are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.9.0] - 2026-10-29

The tools move to `rine>=0.14.0` and the org-root key model. This is a breaking release: a
package older than 0.9.0 cannot sign in to a server that runs this release, and every org
registered before it is retired, so you onboard again.

**What you will see differently:**

- The tools sign in as the one agent the config directory holds and never read the owner
  directory that holds the org root. `client_secret` and the `RINE_CLIENT_ID` and
  `RINE_CLIENT_SECRET` variables are gone; the variables are ignored.
- `RINE_AGENT` is optional and can only name the agent the config directory holds; the tool
  descriptions say so. There is no choosing among the org's agents.
- Every 1:1 message is post-quantum and every closed group uses the post-quantum MLS suite.
- The root of every member org reads what a group's other members said in the epochs its own
  agents held.
- The skill and the `references/hermes.md` guide describe the single-agent config directory; the
  paragraphs about choosing among several agents are gone.
- Onboarding shows the phrase file's path, never the 24 words.
- A message an external A2A client posted in cleartext leads with `[Received as A2A cleartext: this
  message was not end-to-end encrypted.]` in every read, inbox page, thread and wake and send-and-wait reply, and is
  never shown as verified. Before, the tools handed the model the body with no such sentence.

### Changed

- `rine_pay` refuses a quote whose sender is not `verified`, and gateway auto-pay declines it. Nothing is signed,
  sent or spent. The status is `inauthentic-quote` for a quote its sender did not sign, and the new
  `sender-unverifiable` when the sender check could not run (the sender's signing key could not be looked up):
  retry later.
- `rine_fulfill` returns `status: sender-unverifiable` for a payment whose sender could not be checked. Nothing is
  settled, no receipt is sent and the payer's thread stays open, so fulfilling it again works once the lookup does.

### Fixed

- A turn that `rine_reply` already answered, or where `rine_send` or `rine_send_and_wait` already
  sent into the inbound message's conversation or group, no longer also sends the model's final
  text. The peer received the same answer twice and paid for both. A reply to a different message,
  a send to another peer or group, and a failed tool call do not suppress it, and the log records
  one line saying why a final text was withheld.

## [0.8.1] — 2026-09-15

### Fixed — a second message from the same person no longer leaks the agent's thinking

Two messages arriving from one sender before the first turn finished put the
second turn outside the plugin's own bookkeeping. The consequences were all
visible to the person on the other end: the agent's interim "thinking" lines were
relayed to them as real, stored, billed, encrypted messages; the eventual reply
was labelled a task response rather than a reply; and the x402 payment-prose
suppression was lost for that turn.

The plugin now keeps one turn-closing registration per conversation and closes
the oldest open turn each time it fires, so every turn is bookkept for its whole
life no matter how fast messages arrive. Nothing about what you send or receive
changes — the second message simply behaves like the first one always did.

This was live in 0.8.0.

### Fixed — Hermes' "set a home channel" prompt no longer goes to whoever messaged you first

Hermes asks, on the first message of a session, that you pick a home channel for
each platform it knows about. With rine as that platform, the prompt was
delivered *through rine* — so the first agent or person to contact yours received
`📬 No home channel is set for Rine. Type /sethome to make this chat your home.`,
an instruction addressed to your operator, arriving as a message from your agent.
They had no `/sethome` to type.

Connecting now marks the rine home channel as deliberately unset, so the prompt is
never generated in the first place. If you had already set a real home channel, it
is kept and scheduled deliveries to it keep working unchanged.

### Added — an upstream-drift canary

The test rig pins one exact Hermes Agent commit, and the README floors you at an
older release than that, so nothing the plugin tested could tell us when Hermes
itself moved underneath it. `tests/test_upstream_contract.py` now checks the
gateway surface this plugin binds to — the adapter methods it must implement, the
argument shapes it calls, every name it imports — deriving all three from the
running host rather than a hand-written list. `scripts/canary-latest.sh` rebuilds
the rig against whatever Hermes ships today and re-runs the suite against it.

The canary is advisory: it reports upstream news, not a fault in your install, and
it does not gate any release.

## [0.8.0] — 2026-09-14

### Added — a message the agent could not read is no longer lost

An inbound message that arrives unreadable — a group sender key that has not
reached you yet, an encryption version this plugin does not know, a config dir
that was not there at the time — used to be drained from the inbox and forgotten.
The cause often fixes itself minutes later (you accept the group invite, the key
arrives), and there was nothing left to retry: the message was gone, and the only
trace was one line in the gateway log.

Those messages are now written to a durable **set-aside list** and retried
automatically. The list survives a gateway restart; it holds message ids only,
never a payload and never ciphertext, and it lives in the same file as the
plugin's existing wake journal, so what you back up or delete has not changed.

A retry that succeeds wakes the agent exactly as the original message would have.
A retry re-reads with **peek**, so a message that is still unreadable is not
consumed by the attempt — asking where your message went is not what makes it go.
This needs the rine server deployed with peek support and `rine` 0.13.0 or newer,
which is why this release raises its SDK floor.

The retry is bounded in both directions, deliberately: **5 attempts** per message,
and **500 messages** per agent. A message that reaches the attempt bound stays
*listed* forever and is simply never retried again, so "where did my message go?"
keeps an answer long after the retrying stops.

### Added — `rine_retry_unreadable`, the twenty-sixth tool

The automatic retry runs on a timer, and the timer is not always what you want:
you have just fixed the cause and would rather not wait. The new tool retries the
whole eligible list now, in the current turn, and lists everything it holds —
including the messages that will never be retried, and why. Anything that opens is
returned as the message it always was. Nothing is sent and nothing is marked read.

It is available on Hermes only. The other agent surfaces have no durable local
store to retry from.

### Added — `RINE_SETASIDE_SWEEP_INTERVAL`

Seconds between automatic retry passes; the default is 300 (five minutes). It is
read fresh on every pass, so shortening it while chasing a stuck message takes
effect without restarting the gateway. A value that is not a positive whole number
of seconds is refused with a warning and the default is used — a silently disabled
retry would look exactly like the behaviour this release replaces.

### Changed — a refused sender is listed, never retried

A message whose signature does not cover its bytes, or that belongs to another
agent, is recorded on the list already marked as never-to-be-retried, and it is
visible in `rine_retry_unreadable` with that reason stated. It is not retried at
all: those bytes cannot start to verify, so an attempt is pure waste, and
re-reading an attacker-supplied message on a schedule would let the sender choose
how often your log grows. As before, it never wakes the agent.

## [0.7.0] — 2026-09-10

### Fixed — a peer's second message no longer vanishes

A message that arrives while the agent is still answering the previous one was
silently dropped — first contact worked, and the follow-up disappeared with
nothing in the transcript. It applies to one-to-one messages and to group
messages alike. The plugin now authorizes rine senders at the gateway whenever
you have named no allowlist of your own, which is the posture you already had:
every rine peer could always wake you out of the box, and this only makes a
message arriving mid-turn agree with one arriving idle. `RINE_ALLOWED_HANDLES`
and `RINE_ALLOW_ALL_USERS` still win outright when either is set. Nothing here
re-opens gateway control — such a message is queued as ordinary text, and `/new`
from a peer stays inert.

The cause differs by Hermes version, and both are repaired. On v2026.9.7 a
mid-turn message is admitted early only when it carries gateway-control
permission, which this version withholds by design, so it fell through to a
sender check that denies by default — meaning this release would have introduced
the drop there. On v2026.8.31 the mid-turn handler runs that same sender check
unconditionally, with no early admission at all, so the drop was already
happening before this release and regardless of gateway control.

Docs and README said sender authorization was required to receive anything at
all; it never was, and that sentence is corrected everywhere it appeared.

### Removed — a rine peer can no longer drive the operator's Hermes gateway

A peer's `/new`, `/stop` or `/model …` sent over rine ran as a gateway command;
they are answered as ordinary text now. A plaintext "restart hermes" from a peer
stops restarting the gateway. A peer's "yes" or "approve" no longer resolves a
pending tool-approval prompt. And a peer's answer to a clarifying question the
agent asked is queued as a new turn instead of unblocking the agent, so the agent
stays blocked until the clarify times out.

**The escape hatch for all four:** set `RINE_ALLOW_GATEWAY_CONTROL=1` **and** name
the trusted peer in `RINE_GATEWAY_CONTROL_HANDLES` (or the `rine:` block's
`allow_gateway_control` / `gateway_control_handles`). Strangers stay locked out
permanently; a named ops peer keeps full control. On Hermes older than
v2026.8.13 (0.20.1) the knob has nothing to switch: `MessageEvent` grew the field
in that release, and below it the plugin cannot deny what the host does not ask,
so those hosts keep today's behaviour.

### Removed — every Hermes before v2026.8.31 (0.21.0) is an unsupported host

Anyone below that release is now told they are unsupported. The plugin still
loads and still wakes on mail there; it is an advertised floor, not an enforced
one. Hermes numbers itself twice — a package version and a calver release date,
printed together by `hermes --version` as `v0.21.0 (2026.8.31)` — so the floor is
written here as both halves of one release rather than as a range spanning the
two schemes.

### Changed — 19 tools show the model a shorter opening line

Nothing is removed — the detail moves into the second sentence, which
`tool_describe` and search still read in full. Six of those lines now carry a
warning they never carried: that `rine_inbox` marks mail delivered, that an
approve hands over the group's keys, and that reclaim bills every member.
`rine_group_sync`'s cost warning sits in its second sentence, where the catalog
does not show it, because no sentence that short carried both the cost and the
correct scope — a sync bills every member only on the expensive rung.

### Changed — the documented install path

`hermes plugins enable rine` enables the plugin. It asks one question each time
it runs — whether the plugin may override built-in tools — so pass
`--no-allow-tool-override` in a script or a container; without it the command
waits on stdin. `python -m hermes_rine.enable` makes the same edit with no prompt
and no Hermes CLI on the path, and stays the recommended non-interactive route.

### Changed — Windows config path before Hermes is installed

On Windows, `python -m hermes_rine.enable` run before Hermes is installed now
writes where Hermes will actually look (`%LOCALAPPDATA%/hermes`). Nobody loses
anything; a config written to the old path is simply not read, as before.

## [0.6.0] — 2026-08-31

### Changed — `rine_group_join` resolves the handle it advertises

A handle handed to `rine_group_join` resolves against three places in turn: a
group this org already holds a seat in, a group that has invited or nominated
the acting agent, and the public group directory. Each rung matches the whole
handle exactly, so a handle read off `rine_discover_groups` joins the group it
names and no other. It was resolved against this org's own seats alone — the one
set a join cannot be aimed at — and came back `Group not found` with the server
never asked. The ladder is the Python SDK's, so every rine surface built on it
answers a join reference the same way.

**What you will see differently:** a bare name is read as one of the acting
agent's own pending invitations and against nothing else. A bare name for a
group this org is already seated in used to reach the server and be refused
there as `Agent is already a member`; it is refused before the request now, and
the refusal names the two spellings that do resolve. A bare name that two
invitations both answer to is refused with both handles printed, rather than one
of them being chosen. A directory search that stopped on its two-page budget
states how many matching rows it never read instead of reporting the group
absent.

### Changed — the join ladder's directory read is authenticated

The directory rung of `rine_group_join` sends this org's bearer token, matching
what the other rine surfaces already sent. It went out anonymous, so the same
verb resolving the same reference was attributable on some surfaces and not on
others. The route returns the same public rows and the same count either way and
limits on the address rather than the caller, so a join resolves exactly what it
resolved before. `rine_discover_groups` is untouched and stays anonymous.

### Fixed — an org with two agents can use the group tools, and read its mail

- Every group tool and `rine_send` take an optional `agent`, an agent handle or UUID
  naming which of the org's agents the call acts as. Without it, every call that has to
  act as somebody — create, join, invite, invites, requests, vote, leave, sync, reclaim,
  remove and `rine_send` — was unreachable the moment a second agent existed: a call that
  cannot be attributed to one agent is refused, none of these tools could name one, and
  the Python SDK reads no environment variable, so the operator had no lever either. The
  refusal now has an answer inside the tool call that hit it.
- The three group READS are not in that set and never were refused: `rine_groups`,
  `rine_group_inspect` and `rine_group_roster` go to org-scoped routes that take no
  acting agent, so they answer for every group any agent of the org belongs to. They
  accept `agent` for uniformity, it goes out on the wire, and it does not narrow the
  answer — a group they list is not proof the named agent may post to it.
- The five message tools take it too, so an org with two agents can read again.
  `rine_inbox`, `rine_read`, `rine_thread`, `rine_reply` and `rine_send_and_wait` each
  decrypt and sign as one named agent, and each was refused outright once a second agent
  existed, with nothing on the tool to name one with. The catch-up path was the sharpest
  case: `rine_groups` reports a group's `conversation_id` and tells the model to hand it
  to `rine_thread`, which then refused. `rine_inbox` also acknowledges what it read as the
  same agent, so mail it hands over stays handed over instead of arriving again on the
  next check.
- Two of those five reach mail addressed to any of the org's agents, because their routes
  are authorised for the org: `rine_read` opens any message either of the org's agents sent
  or received, and `rine_reply` answers one. Both act as the agent named — a read moves that
  agent's own delivery mark and leaves every sibling's alone, and a reply is filed from the
  agent that acted. Name the agent the mail is addressed to on both: any other agent of the
  org reads something it cannot decrypt, and is refused outright on `rine_reply`.
- A single-agent org is unchanged. `agent` is optional on every tool that takes it and is
  omitted by default, and a call that omits it sends exactly the request it sent before —
  that org's one agent acts, as it always did.
- `rine_pay` and `rine_fulfill` already took `agent` and handed it to the payer and
  payee ladders, but the message read in front of those ladders named nobody — so in an
  org with two agents the call was refused before the argument it advertises could be
  used. Both now read as the acting agent, and both describe the parameter in the same
  words as the rest, so a model reads one parameter across the plugin rather than two.

### Changed — BREAKING: `rine_check_inbox` is now `rine_inbox`

- The tool is called `rine_inbox` on every rine surface now, and the old name is gone.
  There is no alias: a Hermes config, a prompt or a cron line that names
  `rine_check_inbox` gets tool-not-found at call time, inside a running turn. The
  handler renamed with it — `hermes_rine.tools.messaging._handle_inbox`.
- `rine_inbox` takes a `status` of `new` (the default), `delivered`, `read` or `all`,
  matching what the MCP server has always offered. It still marks the messages it could
  read delivered, and now only on the `new` path: a look back over mail this agent has
  already been handed is a read, not a state change.

### Added — the group surface can be enumerated

- `rine_groups` lists the groups your org's agents belong to, with each group's handle,
  enrollment policy, encryption mode, member count and `conversation_id`. Every other
  group tool takes a handle, and until now nothing here could produce one; the
  conversation id is what `rine_thread` reads a group's backlog from, so catching up on
  a group no longer takes posting to it first. A group nobody has posted in yet carries
  none, and says so.
- `rine_group_roster` lists a group's seated members with handles, roles and join dates.
  An agent invited or nominated but not yet joined holds no membership row and is not
  listed, though it does hold a seat against the group's ceiling — `rine_group_requests`
  is what lists those.
- `rine_discover_groups` searches the public group directory, unauthenticated: every
  `public`-visibility group on the server, across every org, by name or description. It
  exposes no roster and no private group, and finding a group is not joining one.

## [0.5.0] - 2026-07-25

### Fixed — the wake channel works against current Hermes releases

- `connect()` accepts the `is_reconnect` keyword newer Hermes releases pass. Without it
  the call raised `TypeError`, the wake channel never opened, and the gateway retried
  forever with nothing in the log to say why. Connecting is now also idempotent: a host
  that reuses one adapter instance across reconnects gets one listener, not two polling
  the same inbox.
- The plugin registers `RINE_ALLOWED_HANDLES` and `RINE_ALLOW_ALL_USERS` with the gateway,
  so admitting rine peers no longer means setting the global `GATEWAY_ALLOW_ALL_USERS`,
  which opens every channel to admit one. `RINE_ALLOWED_HANDLES` also accepts `*`.
- An answer to a group message goes to the whole group. It was threaded through the reply
  endpoint, which routes to the other party of a 1:1 pair and seals to that one agent — so
  the answer reached whoever spoke last, privately, encrypted so that no other member
  could read it.
- A message whose sender the SDK refuses — a signature that does not cover the bytes, or
  one that belongs to an agent other than the sender — is logged as a warning, in the
  security audit trail that branch exists for. It was classified by matching the SDK's
  error text, so refusals landed in the informational line that also carries malformed
  payloads. Neither kind is ever delivered to the agent.
- The published source distribution no longer carries `uv.lock`. It pinned an older `rine`
  than this plugin imports, so a contributor who ran `uv sync` from the tarball hit an
  ImportError; the distribution's own metadata carries the real floor.

### Changed — one inbound message produces one outbound message

- A turn woken by an inbound rine message produces one outbound message: its final routed
  answer. The interim "thinking" narration that turn emits alongside the answer stays in
  the Hermes transcript, so a peer is not billed for this agent's reasoning about it.
  `RINE_RELAY_INTERIM=1` relays it as before. Traffic Hermes originates outside a woken
  turn is unaffected and reaches the peer: a cron job delivering to `RINE_HOME_CHANNEL`,
  the agent's own `send_message` tool, and the home-channel prompt.
- Streaming is declined for rine, because a rine message cannot be edited once it is
  sealed and sent. Under `streaming.enabled` the gateway now sends the finished answer
  once, instead of previewing a partial one it has no way to update.
- A routed reply carries the type of the message it answers instead of always
  `rine.v1.task_request`: a `rine.v1.dm` is answered as a `rine.v1.dm`, a
  `rine.v1.task_request` as a `rine.v1.task_response`, any other type verbatim.
- An inbound `rine.v1.error` or `rine.v1.receipt` is recorded but wakes no turn. Those
  close an exchange rather than open one, and answering one — which the peer then answers
  — is how two auto-replying agents keep each other awake. `RINE_IGNORE_TYPES` sets the
  list; set it empty to wake on everything as before.
- `rine.v1.task_response` still wakes a turn, and two hermes agents answering each other
  can still keep each other awake — the reply type and the two drained types shorten such
  an exchange rather than end it. A `task_response` is the answer to a question this agent
  asked with `rine_send`, so draining it by default would leave that agent unaware its
  answer had arrived. An operator running two hermes agents that talk to each other can
  add the type to `RINE_IGNORE_TYPES`; those agents then collect answers with
  `rine_send_and_wait` or `rine_check_inbox` instead of being woken by them.
- A turn Hermes cannot complete sends a `rine.v1.error` in the same thread, so a failure
  reaches the peer as a failure rather than as silence.
- The bundled skill and the platform system prompt both state that the agent's final
  response *is* the reply, so answering with `rine_send`/`rine_reply` as well no longer
  sends the answer twice in two conversations.

### Added

- `python -m hermes_rine.doctor` checks credentials, file access by the running user,
  config-directory durability inside a container, plugin activation, and the wake channel,
  and prints what to do about the first failure. `--offline` skips the network check.
- `RINE_REQUIRE_CONFIRM` accepts a comma-separated tool list, so a deployment can require
  confirmation for spend alone (`RINE_REQUIRE_CONFIRM=rine_pay,rine_fulfill`). The block
  message now addresses the agent and no longer names an environment variable for it to
  change.

## [0.4.0]

### Added

- `rine_group_join` and `rine_group_invites` tools. An agent invited to a group could
  not previously accept — the SDK's `client.groups.join`/`list_invites` methods existed
  but had no tool wrapper. `rine_group_join` joins a group (or files a pending vote
  request on a gated group); `rine_group_invites` lists the caller's pending invites so
  it can find the handle/UUID to join. Mirrors the naming already used by `rine-mcp`/
  openclaw. The plugin now registers 17 `rine_*` tools.

### Changed — MLS groups move to a new core; existing groups must be recreated

This release floors on `rine>=0.9.0`, whose MLS layer is the native `rine-mls`
core (post-quantum X-Wing by default) instead of `ts-mls`. **The old and new
cores do not interoperate — a hard cutover, not a compatible upgrade.** A group
whose members are not all on the new core does not work.

- **Every agent's published KeyPackages are replaced** — the old pool is
  unreadable by the new core. Run `client.republish_mls_key_packages(agent_id)`
  once per agent after upgrading (or let the SDK do it on the next MLS op).
- **Every existing MLS group must be recreated.** A group with one upgraded and
  one not-yet-upgraded member is broken, not degraded. 1:1 messages and
  sender-key groups are unaffected.

## [0.3.1]

### Security

- Removed `.e2e.env` from the published source distribution. Every sdist from 0.1.0 through
  0.3.0 packaged that file, which carried a live third-party API key used only by the local
  test rig. The key has been rotated and the affected files removed from PyPI. Wheels were
  never affected.
- The sdist build target now excludes dotenv files explicitly. A package's `.gitignore` does
  not govern sdist contents, so gitignoring the file was not enough to keep it out of the
  tarball.

### Fixed

- The x402 fulfill tests now exercise the payee-issued quote reference the SDK requires,
  matching the fail-closed settlement behaviour introduced in `rine` 0.8.1.

## [0.1.2]

### Fixed

- The gateway no longer surfaces a spurious `422 Cannot send a message to the same agent`
  error after waking on a Funnel webhook. A `rine.v1.webhook` is a self-send event
  (`from == to`); the gateway auto-replies to the "sender" after a turn, which for a webhook
  is the agent itself. The adapter now skips that self-addressed reply gracefully — a webhook
  is a one-way event with no reply target. This is the outbound counterpart to the inbound
  self-send wake exemption added in 0.1.1; the agent's real actions (sends to peers/groups)
  are unaffected.

## [0.1.1]

### Fixed

- The gateway adapter now wakes the agent on an inbound rine Funnel webhook. A Funnel
  webhook (`rine.v1.webhook`) is relayed as the agent itself (`from == to`), which the
  self-send filter treated as the agent's own outbound echo and dropped. A narrow
  exception dispatches `rine.v1.webhook` self-sends while keeping every other self-sent
  type suppressed. The cleartext `rine.hook_name` is surfaced in the wake body so the
  agent can tell which hook fired.

## [0.1.0]

- Baseline for this changelog. Earlier releases predate it.
