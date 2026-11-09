# Auxiliar (use com `source`): faz login no Gateway e injeta o JWT em todo `curl` do script.
# Precisa de GW (URL do gateway) e ADMIN_USER/ADMIN_PASSWORD (do ambiente ou do arquivo .env).
# CURL_INSECURE=1 aceita certificado autoassinado (https://<ip-edge>): acrescenta -k a todo curl.
_HS_K=""; [ "${CURL_INSECURE:-}" = 1 ] && _HS_K="-k"
_HS_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
_env() { [ -n "${!1:-}" ] && { printf '%s' "${!1}"; return; }; grep -E "^$1=" "$_HS_ROOT/.env" 2>/dev/null | cut -d= -f2-; }
_hs_token() {
  [ -n "${TOKEN:-}" ] && return 0
  TOKEN=$(command curl ${_HS_K:+$_HS_K} -s -X POST "$GW/auth/login" -H 'Content-Type: application/json' \
    -d "{\"username\":\"$(_env ADMIN_USER)\",\"password\":\"$(_env ADMIN_PASSWORD)\"}" \
    | python3 -c 'import sys,json; print(json.load(sys.stdin).get("access_token",""))' 2>/dev/null)
}
curl() { _hs_token; command curl ${_HS_K:+$_HS_K} -H "Authorization: Bearer ${TOKEN:-}" "$@"; }
