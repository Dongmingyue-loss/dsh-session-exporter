# Security Policy

## Supported versions

| Version | Supported |
|---|---|
| 1.x | Yes |

## Reporting a vulnerability

Please report security issues through GitHub Security Advisories:

<https://github.com/Dongmingyue-loss/dsh-session-exporter/security/advisories/new>

Do not open a public issue for a vulnerability that could expose session data, credentials, or local files.

## Scope

This tool is a single-user local application:

- It binds only to `127.0.0.1`.
- API requests require a random token generated at startup.
- DSH session files are opened read-only and never modified.
- The normalization cache is written only to the exporter-owned cache directory.
- It does not send session content to any remote service and has no telemetry.

## Data handling

Exports can contain conversation text, tool input/output, file paths, and other sensitive information. Treat exported TXT/JSONL files and the local cache as sensitive data. Do not upload them to public services without review.

The tool never stores message bodies in logs. Error messages may include file paths but not conversation content.
