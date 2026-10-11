# Authentication service

Endpoints:

| method | path | description |
|---|---|---|
| POST | `/register` | create a user (`201`, `409` if the username is taken) |
| POST | `/login` | exchange credentials for a bearer token (`401` on bad credentials) |
| GET | `/me` | return the authenticated user (requires `Authorization: Bearer <token>`) |

Passwords are stored as salted PBKDF2-HMAC-SHA256 hashes. Tokens are HMAC-signed and expire after one hour.
Set `AUTH_SECRET` in the environment to change the signing key.
