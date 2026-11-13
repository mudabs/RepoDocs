# ADR-005: Use Trusted Python Built-ins and a Versioned External Analyzer Protocol

- Date: 2026-11-13
- Status: Accepted

## Context

RepoDocs needs fast iteration for initial analyzers while preserving extensibility and avoiding execution of repository code during scanning.

## Alternatives

- Run every analyzer as an in-process plugin.
- Run every analyzer in a subprocess.
- Use trusted built-in Python modules and a versioned subprocess protocol for external analyzers.

## Decision

Initial trusted analyzers are Python modules. External analyzers use a versioned, language-neutral subprocess protocol with bounded resources and serialized inputs and outputs.

## Consequences

The core plugin contract must define capabilities, IR contributions, errors, versions, and resource limits. Analyzer trust and repository trust remain separate concerns.
