"""Repository-owned wrappers behind the generic SBP_CLI_RUNNER_COMMAND contract.

`protocol` is shared by every wrapper. A wrapper module (for example `codex`)
supplies only the call into its CLI; nothing under `sbp_gen` knows which
wrapper is configured.
"""
