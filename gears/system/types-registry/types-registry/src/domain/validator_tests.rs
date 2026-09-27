use base64::Engine as _;
use base64::engine::general_purpose::URL_SAFE_NO_PAD;
use serde_json::json;

use super::*;

const FINGERPRINT: &[u8] = &[7; 32];

fn select(names: &[&str]) -> FieldSelection {
    FieldSelection::parse(names).expect("valid selection")
}

fn token(value: &serde_json::Value) -> String {
    URL_SAFE_NO_PAD.encode(serde_json::to_vec(value).expect("serializable"))
}

#[test]
fn equal_inputs_give_a_byte_identical_token() {
    let a = Validator::compute(3, Some(FINGERPRINT), FieldSelection::default());
    let b = Validator::compute(3, Some(FINGERPRINT), FieldSelection::default());
    assert_eq!(a.encode(), b.encode());
}

#[test]
fn a_revision_changes_the_validator() {
    let before = Validator::compute(3, Some(FINGERPRINT), FieldSelection::default());
    let after = Validator::compute(4, Some(FINGERPRINT), FieldSelection::default());
    assert_ne!(before, after);
}

#[test]
fn a_refreshed_fingerprint_changes_the_validator_at_the_same_revision() {
    let before = Validator::compute(3, Some(FINGERPRINT), FieldSelection::default());
    let after = Validator::compute(3, Some(&[8; 32]), FieldSelection::default());
    assert_ne!(before, after);
}

#[test]
fn an_instance_validator_has_no_fingerprint_and_still_changes_on_revision() {
    let before = Validator::compute(1, None, FieldSelection::default());
    let after = Validator::compute(2, None, FieldSelection::default());
    assert_ne!(before, after);
    assert_ne!(
        before,
        Validator::compute(1, Some(&[]), FieldSelection::default())
    );
}

#[test]
fn two_selections_of_one_entity_give_two_validators() {
    let narrow = Validator::compute(3, Some(FINGERPRINT), FieldSelection::default());
    let wide = Validator::compute(3, Some(FINGERPRINT), select(&["content", "origin"]));
    assert_ne!(narrow, wide);
}

#[test]
fn equal_normalized_selections_give_one_validator() {
    let compute = |selection| Validator::compute(3, Some(FINGERPRINT), selection);
    let explicit_default = select(&["gts_id", "gts_uuid", "kind", "origin", "lifecycle_status"]);
    assert_eq!(
        compute(FieldSelection::default()),
        compute(explicit_default)
    );
    assert_eq!(
        compute(select(&["content"])),
        compute(select(&[" Content ", "KIND", "lifecycle_status"])),
    );
}

#[test]
fn the_wire_form_is_base64url_of_a_versioned_json_object() {
    let encoded = Validator::compute(3, Some(FINGERPRINT), FieldSelection::default()).encode();
    assert_eq!(encoded.len(), 48, "DESIGN §3.3's managed length: {encoded}");
    let json: serde_json::Value =
        serde_json::from_slice(&URL_SAFE_NO_PAD.decode(&encoded).expect("base64url"))
            .expect("a JSON object");
    assert_eq!(json["v"], 1);
    let digest = URL_SAFE_NO_PAD
        .decode(json["d"].as_str().expect("digest string"))
        .expect("base64url digest");
    assert_eq!(digest.len(), 16, "a 128-bit digest");
}

#[test]
fn a_token_decodes_back_to_its_validator() {
    let validator = Validator::compute(3, None, select(&["provenance"]));
    assert_eq!(Validator::decode(&validator.encode()), Some(validator));
}

#[test]
fn decoding_compares_fields_not_the_encoded_spelling() {
    let validator = Validator::compute(3, Some(FINGERPRINT), FieldSelection::default());
    let wire: serde_json::Value = serde_json::from_slice(
        &URL_SAFE_NO_PAD
            .decode(validator.encode())
            .expect("base64url"),
    )
    .expect("json");
    let respelled = token(&json!({ "d": wire["d"], "v": 1 }));
    assert_ne!(respelled, validator.encode());
    assert_eq!(Validator::decode(&respelled), Some(validator));
}

#[test]
fn an_unknown_version_is_not_a_match() {
    let validator = Validator::compute(3, Some(FINGERPRINT), FieldSelection::default());
    let wire: serde_json::Value = serde_json::from_slice(
        &URL_SAFE_NO_PAD
            .decode(validator.encode())
            .expect("base64url"),
    )
    .expect("json");
    assert_eq!(
        Validator::decode(&token(&json!({ "v": 2, "d": wire["d"] }))),
        None
    );
}

#[test]
fn a_malformed_token_is_not_a_match() {
    let short = URL_SAFE_NO_PAD.encode([0_u8; 8]);
    for bad in [
        String::new(),
        "not base64!".to_owned(),
        URL_SAFE_NO_PAD.encode(b"not json"),
        token(&json!({ "v": 1, "d": short })),
        token(&json!({ "v": 1, "d": URL_SAFE_NO_PAD.encode([0_u8; 16]), "x": 0 })),
        token(&json!([1, "d"])),
    ] {
        assert_eq!(Validator::decode(&bad), None, "{bad:?}");
    }
}
