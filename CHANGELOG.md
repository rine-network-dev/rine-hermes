# Changelog

All notable changes to the `hermes-rine` plugin are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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
