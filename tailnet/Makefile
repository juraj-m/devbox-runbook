# Apply policy.hujson to the tailnet through the Tailscale API, so what runs
# is always the repo copy. Run from the laptop.
#
# Needs an API access token: admin console > Settings > Keys > Generate API
# access token (90 days is the maximum; renew it there). Either export
# TS_API_KEY, or store it as TS_API_KEY in a sops file in your secrets repo,
# $(SECRETS)/tailnet/api-key.prod.env (encrypted to your hardware keys only),
# decrypted on demand, one touch per target.
SHELL := /bin/bash
SECRETS ?= $(HOME)/_/secrets
# "-" is the tailnet of the token's owner.
TAILNET ?= -
API := https://api.tailscale.com/api/v2/tailnet/$(TAILNET)/acl
key = $${TS_API_KEY:-$$(sops decrypt --extract '["TS_API_KEY"]' $(SECRETS)/tailnet/api-key.prod.env)}

.PHONY: validate diff apply

# Parses the file and runs its `tests` block without changing anything.
# The endpoint answers 200 either way and reports problems in a "message"
# field, so the body decides.
validate:
	@k=$(key) && out=$$(curl -fsS -u "$$k:" -H 'Content-Type: application/hujson' \
		--data-binary @policy.hujson $(API)/validate) && \
	if printf '%s' "$$out" | grep -q '"message"'; then printf '%s\n' "$$out"; exit 1; fi && \
	echo "policy.hujson: valid, tests pass"

# What the tailnet runs now vs the repo. Empty diff means they match. -b:
# Tailscale re-indents the stored copy (aligned colons), which is not a change.
diff:
	@k=$(key) && diff -b <(curl -fsS -u "$$k:" -H 'Accept: application/hujson' $(API) | sed -e '$$a\') policy.hujson \
		&& echo "tailnet matches policy.hujson"

apply: validate
	@k=$(key) && curl -fsS -u "$$k:" -H 'Content-Type: application/hujson' \
		--data-binary @policy.hujson $(API) >/dev/null && echo "applied policy.hujson"
