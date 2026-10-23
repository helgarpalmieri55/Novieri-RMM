# Used only while migrating BACK to Shamir (scripts/openbao-seal.sh disable):
# the old seal must stay readable, marked disabled, for `unseal -migrate`.
seal "static" {
  current_key_id = "itms-unseal-1"
  current_key    = "file:///bao/unseal/unseal.key"
  disabled       = "true"
}
