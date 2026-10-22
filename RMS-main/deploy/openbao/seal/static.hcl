# Auto-unseal with a static key (OpenBao >= 2.2). Loaded only when
# openbao/auto-unseal.yml is in COMPOSE_FILE -- see scripts/openbao-seal.sh.
# The key is 32 random bytes in a file on the HOST, outside the repo and
# outside every Docker volume, bind-mounted read-only.
seal "static" {
  current_key_id = "itms-unseal-1"
  current_key    = "file:///bao/unseal/unseal.key"
}
