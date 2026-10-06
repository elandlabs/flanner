"""The Curb wire contract, checked against the client (docs/curb-wire-contract.md).

The control plane imports no client code for Curb; both sides check the
contract's test vectors instead. These tests recompute every vector from
`curb_wire`, so the page and the code cannot drift apart.
"""

import base64
import json
import re
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from flanner import curb_attribution, curb_sshsig, curb_wire, identity, refusals
from flanner.artifacts import canonical_bytes
from flanner.entitlements import CURB_ALERTS, CURB_ATTRIBUTION, CURB_FLEET, CURB_POLICY, Claims

DOC = Path(__file__).resolve().parent.parent / "docs" / "curb-wire-contract.md"
SIGNER = {
    "authority": "issuer",
    "policy": "policy_current",
    "report": "device",
    "registry": "issuer",
}


def vectors():
    tail = DOC.read_text(encoding="utf-8").split("## Test vectors", 1)[1]
    return json.loads(re.search(r"```json\n(.*?)\n```", tail, re.S).group(1))


def private(seed):
    return Ed25519PrivateKey.from_private_bytes(bytes.fromhex(seed))


def signer(key):
    return lambda payload: base64.b64encode(key.sign(payload)).decode("ascii")


def claims(*features):
    return Claims(
        "org",
        "user",
        "dev",
        "key",
        "2026-01-01T00:00:00Z",
        "2027-01-01T00:00:00Z",
        features=features,
    )


def test_every_key_derives_from_its_seed():
    keys = vectors()["keys"]
    for entry in keys.values():
        assert identity.public_key_b64(private(entry["seed"]).public_key()) == entry["public_key"]
    device = keys["device"]
    assert identity.device_id_for(private(device["seed"]).public_key()) == device["device_id"]


@pytest.mark.parametrize("name", ["authority", "policy", "report", "registry"])
def test_each_document_signs_to_exactly_its_vector(name):
    found = vectors()
    doc, key = found[name], found["keys"][SIGNER[name]]
    assert canonical_bytes(doc["fields"]).decode("utf-8") == doc["canonical"]
    assert curb_wire.doc_hash(doc["fields"]) == doc["hash"]
    assert curb_wire.encode(doc["fields"], signer(private(key["seed"]))) == doc["token"]
    keyring = {doc["fields"]["key_id"]: key["public_key"]}
    assert curb_wire.read(doc["token"], keyring, doc["fields"]["kind"]) == doc["fields"]
    assert curb_wire.fields_of(doc["token"]) == doc["fields"]


def test_a_document_does_not_read_as_another_kind_with_another_key_or_tampered():
    found = vectors()
    token = found["policy"]["token"]
    current = {"pol_2026": found["keys"]["policy_current"]["public_key"]}
    revoked = {"pol_2026": found["keys"]["policy_revoked"]["public_key"]}
    assert curb_wire.read(token, current, curb_wire.REPORT) is None
    assert curb_wire.read(token, revoked, curb_wire.POLICY) is None
    fields = dict(found["policy"]["fields"], version=8)
    forged = curb_wire.encode(fields, lambda _: token.split(".")[1])
    assert curb_wire.read(forged, current, curb_wire.POLICY) is None


def test_event_ids_and_headers_match_their_vectors():
    found = vectors()
    event = found["event_id"]
    assert (
        curb_wire.event_id(event["device_id"], event["finding"], event["sequence"]) == event["id"]
    )
    assert (
        curb_wire.event_id(event["device_id"], event["finding"], event["sequence"] + 1)
        != event["id"]
    )
    assert curb_wire.headers("0.16.0") == found["headers"]


def test_every_name_the_client_sends_is_in_the_contract():
    text = DOC.read_text(encoding="utf-8")
    for name in [*curb_wire.PATHS.values(), *curb_wire.CAPABILITIES, curb_wire.EVENT_HEADER]:
        assert f"`{name}`" in text
    for code in (
        refusals.CLIENT_TOO_OLD,
        refusals.STALE_SEQUENCE,
        refusals.KEY_OWNED,
        refusals.BAD_PROOF,
        refusals.REPLACES_UNKNOWN,
    ):
        assert f"`{code}`" in text
        assert code in refusals.KNOWN
    for feature in (CURB_POLICY, CURB_FLEET, CURB_ALERTS, CURB_ATTRIBUTION):
        assert f"`{feature}`" in text


def test_a_feature_needs_both_the_entitlement_and_the_server():
    offered = (curb_wire.POLICY_V1,)
    assert curb_wire.usable(CURB_POLICY, claims(CURB_POLICY), offered)
    assert not curb_wire.usable(CURB_POLICY, claims(), offered)  # not in the plan
    assert not curb_wire.usable(CURB_POLICY, claims(CURB_POLICY), ())  # an older control plane
    assert not curb_wire.usable(CURB_FLEET, claims(CURB_FLEET), offered)
    assert not curb_wire.usable(CURB_POLICY, None, offered)  # not signed in
    # And why not, for a person: the control plane's answer comes first,
    # because one from before Curb offers nothing whatever the plan says.
    assert curb_wire.unusable(CURB_POLICY, claims(CURB_POLICY), offered) == ""
    assert "does not offer" in curb_wire.unusable(CURB_POLICY, claims(), ())
    assert "plan" in curb_wire.unusable(CURB_POLICY, claims(), offered)
    assert "no current entitlement" in curb_wire.unusable(CURB_POLICY, None, offered)


def test_the_attribution_proof_signs_to_its_vector():
    found = vectors()
    proof, key = found["proof"], found["keys"]["attribution"]
    fields = proof["fields"]
    canonical = curb_attribution.proof_bytes(
        fields["device_id"], fields["public_key"], fields["nonce"], fields["replaces"]
    )
    assert canonical.decode("utf-8") == proof["canonical"]
    attribution = private(key["seed"])
    assert base64.b64encode(attribution.sign(canonical)).decode("ascii") == proof["signature"]
    assert curb_sshsig.fingerprint(attribution.public_key()) == key["fingerprint"]
    assert identity.public_key_b64(attribution.public_key()) == key["public_key"]
