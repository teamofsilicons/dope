#!/usr/bin/env python3
"""Run a remote command using short-lived Lightsail credentials and AWS-verified host keys."""
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile


def main():
    instance = os.getenv("DOPE_LIGHTSAIL_INSTANCE", "silicon-dopes-prod")
    result = subprocess.run(
        ["aws", "--profile", "silicon-deployment", "--region", "us-east-1", "lightsail",
         "get-instance-access-details", "--instance-name", instance, "--protocol", "ssh", "--output", "json"],
        stdout=subprocess.PIPE, check=True,
    )
    details = json.loads(result.stdout)["accessDetails"]
    ip = details["ipAddress"]
    if not details.get("hostKeys"):
        sys.exit("Lightsail has not published the instance's SSH host keys yet.")
    with tempfile.TemporaryDirectory(prefix="dopes-ssh-") as directory:
        base = Path(directory)
        identity = base / "identity"
        cert = base / "identity-cert.pub"
        hosts = base / "known_hosts"
        identity.touch(mode=0o600)
        identity.write_text(details["privateKey"].rstrip() + "\n")
        cert.write_text(details["certKey"].rstrip() + "\n")
        entries = []
        for host in details["hostKeys"]:
            key, algorithm = host["publicKey"].strip(), host["algorithm"]
            if not key.startswith(algorithm + " "):
                key = algorithm + " " + key
            entries.append(ip + " " + key)
        hosts.write_text("\n".join(entries) + "\n")
        command = ["ssh", "-F", "/dev/null", "-o", "BatchMode=yes", "-o", "IdentitiesOnly=yes",
                   "-o", "ConnectTimeout=12", "-o", "StrictHostKeyChecking=yes",
                   "-o", f"UserKnownHostsFile={hosts}", "-o", f"CertificateFile={cert}",
                   "-i", str(identity), details["username"] + "@" + ip]
        if sys.argv[1:]:
            command.append(shlex.join(sys.argv[1:]))
        return subprocess.run(command).returncode


if __name__ == "__main__":
    sys.exit(main())
