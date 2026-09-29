use super::*;

const GTS_ID: &str = "gts.cf.core.key.person.v1~";

#[test]
fn both_spellings_round_trip_through_their_stored_text() {
    let uuid = GtsId::try_new(GTS_ID).expect("valid").to_uuid();
    for key in [EntityKey::GtsId(GTS_ID.to_owned()), EntityKey::Uuid(uuid)] {
        assert_eq!(EntityKey::parse(&key.to_string()), key);
    }
}

#[test]
fn an_identifier_and_its_registry_reference_share_one_uuid() {
    let uuid = GtsId::try_new(GTS_ID).expect("valid").to_uuid();
    assert_eq!(EntityKey::GtsId(GTS_ID.to_owned()).gts_uuid(), Some(uuid));
    assert_eq!(EntityKey::Uuid(uuid).gts_uuid(), Some(uuid));
    assert_eq!(EntityKey::Uuid(uuid).gts_id(), None);
}

#[test]
fn a_registry_reference_is_stored_lowercase_and_hyphenated() {
    let uuid = Uuid::parse_str("0F5C8E3A-0000-5000-8000-000000000001").expect("valid");
    assert_eq!(
        EntityKey::Uuid(uuid).to_string(),
        "0f5c8e3a-0000-5000-8000-000000000001"
    );
}
