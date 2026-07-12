package trivy

default ignore = false

# These suppressions stop applying automatically at the start of 2026-08-15 UTC.
suppression_active {
    time.now_ns() < time.parse_rfc3339_ns("2026-08-15T00:00:00Z")
}

# CVE-2026-39831 affects FIDO/U2F SSH security-key verification. The pinned
# Transifex CLI uses this dependency only indirectly and talks to the Transifex
# HTTPS API in this pipeline; it does not expose SSH authentication to users.
ignore {
    suppression_active
    input.Type == "vulnerability"
    input.VulnerabilityID == "CVE-2026-39831"
    input.PkgName == "golang.org/x/crypto"
    input.InstalledVersion == "v0.0.0-20210322153248-0c34fe9e7dc2"
}

# Trivy matches CVE-2026-39822 by Go compiler version. None of the exact gh,
# yq, tx, or gosu releases in the image calls an affected os.Root API, and the
# API did not exist in Go 1.16. Restrict the suppression to the stdlib versions
# reported for those binaries so a finding in any other version remains visible.
affected_os_root_versions := {"v1.16.15", "v1.24.6", "v1.26.4"}

ignore {
    suppression_active
    input.Type == "vulnerability"
    input.VulnerabilityID == "CVE-2026-39822"
    input.PkgName == "stdlib"
    input.InstalledVersion == affected_os_root_versions[_]
}
