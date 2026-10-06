# Stage 2 security rollout

## Required runtime configuration

Before deploying this backend, set `SHOPEE_TOKEN_ENCRYPTION_KEYS` on the DigitalOcean backend component as an encrypted runtime variable. Generate a unique Fernet key in a trusted local shell with:

```sh
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

Do not send the value in chat, commit it, or reuse JWT/Shopee partner secrets. The database, including Neon backups, cannot decrypt credentials without this separate key. Retain the key in your secret manager and recovery plan.

Startup creates the nonce/replay tables and adds `session_version INTEGER NOT NULL DEFAULT 0` to Store buyer/admin and ERP users. It encrypts existing marketplace tokens in one transaction before auto-sync starts. With an absent/invalid key or corrupted ciphertext, token reads/writes fail closed and startup logs a migration failure; login and user-session validation remain available after version columns have migrated; operations loading shop credentials remain unavailable until the key is configured. Configure the key before deployment to avoid interrupting Shopee sync.

For key rotation, set `SHOPEE_TOKEN_ENCRYPTION_KEYS=new_key,old_key`, deploy/restart to rewrap stored tokens, and verify migration succeeds. Remove the old key only after all rows are rotated; keep the old key as needed for historical backup recovery. Do not roll back to the plaintext-token backend after migration without a reviewed recovery procedure.

When Biteship is active (`BITESHIP_API_KEY` set), also configure `BITESHIP_WEBHOOK_SECRET` and the corresponding Biteship dashboard header. `BITESHIP_WEBHOOK_KEY` retains its existing default `X-Webhook-Secret`. Missing secrets now reject callbacks; status remains verified against Biteship's tracking API.

For Shopee push, `SHOPEE_PUSH_URL` must exactly match the registered public callback URL, including trailing slash. The verifier accepts only HMAC-SHA256 of `url|raw_body`, using the explicit `SHOPEE_PUSH_KEY`, or `SHOPEE_PARTNER_KEY` when the push key is absent. Keys are used as text, as with Shopee API signing. Duplicate notifications receive 200 without a second background pull; timestamps older than four hours or over five minutes in the future are rejected. Verify a genuine provider push after deployment; tests only exercise fixture signatures.

## OAuth frontend/backend deployment

Deploy the companion `frontend-marketplace-erp` PR together with this backend. Shopee receives the existing redirect URL with `/<akun_id>/<nonce>` appended to its callback path. The backend validates the configured `SHOPEE_REDIRECT_URI` or an explicitly allowed CORS origin, requires the owner session that started authorization, and consumes the nonce once within ten minutes. The frontend forwards the nonce and avoids duplicate exchanges under React StrictMode.

Ensure the Shopee redirect allowlist accepts this callback path. Existing in-flight authorizations without a nonce are rejected; start a new authorization. Successful existing shop connections require no reconnect when their stored tokens migrate successfully.

After a password change, old Store/ERP JWTs are rejected. The initiating Store Admin and ERP responses issue a new cookie, preserving that browser's session. Buyer/admin tables, cookie names, audiences, and database routing remain separate.

## Protected files and flows

The iPaymu adapter, Buyer router, Buyer frontend, checkout/payment service functions and provider URLs/payloads are unchanged. Regression checks cover the existing iPaymu test suite. The shared middleware's requested generic 500 response applies to all tenants, without changing payment endpoints.
