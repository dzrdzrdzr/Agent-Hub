# Security policy

## Supported versions

The latest commit on `main` is the supported development version. Until the project reaches a stable release, security fixes are not backported.

## Security model

Agent Hub can launch coding agents, shell commands, tests, builds, and training processes with the permissions of the current operating-system user. Treat it as a local automation runner, not a sandbox.

Before use:

- run it under a non-privileged account;
- keep valuable work in version control;
- configure `protected_paths` and `forbidden_commands`;
- keep `allow_network: false` unless network access is required and understood;
- review provider and executor configuration independently;
- never place credentials in prompts, logs, issues, or committed YAML files;
- bind IPC to loopback unless you have added authentication and transport security.

The default TCP protocol has no remote authentication and must not be exposed to an untrusted network.

## Reporting a vulnerability

Open a private GitHub security advisory when available. Otherwise contact the repository owner without publishing exploit details.

Include the affected commit, operating system, configuration, reproduction steps, and impact. Remove API keys, access tokens, private source code, hostnames, and user data.
