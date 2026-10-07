"""SSHSIG signatures and OpenSSH key lines (curb_sshsig).

The interop with ssh-keygen itself is in tests/test_curb_attribution.py.
"""

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from flanner import curb_sshsig

KEY = Ed25519PrivateKey.from_private_bytes(bytes([41]) * 32)


def test_a_signature_verifies_only_for_its_message_and_namespace():
    armored = curb_sshsig.sign(KEY, b"message")
    assert curb_sshsig.verify(armored, b"message") == KEY.public_key()
    assert curb_sshsig.verify(armored, b"messagE") is None
    assert curb_sshsig.verify(armored, b"message", namespace="file") is None


def test_malformed_signatures_are_refused_quietly():
    armored = curb_sshsig.sign(KEY, b"m")
    for bad in ("", "not armored", armored.replace("A", "B", 3), armored[:-40] + curb_sshsig.END):
        assert curb_sshsig.verify(bad, b"m") is None


def test_key_lines_and_fingerprints_follow_openssh():
    line = curb_sshsig.openssh_line(KEY.public_key(), "agent@device")
    assert line.startswith("ssh-ed25519 AAAAC3NzaC1lZDI1NTE5") and line.endswith(" agent@device")
    assert curb_sshsig.from_openssh_line("key::" + line) == KEY.public_key()
    print_ = curb_sshsig.fingerprint(KEY.public_key())
    assert print_.startswith("SHA256:") and len(print_) == 50 and "=" not in print_
    for bad in ("ssh-rsa AAAA", "ssh-ed25519", "ssh-ed25519 !!!"):
        try:
            curb_sshsig.from_openssh_line(bad)
        except ValueError:
            continue
        raise AssertionError(bad)
