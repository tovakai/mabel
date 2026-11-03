# DEVICE_ROUTES

**Path:** `routes/device_routes.py`

## Purpose

Admin/single-user HTTP API for device management, SSH parse/test, remote directory browsing, and allowed-directory CRUD.

## Endpoints

- `GET/POST/PATCH/DELETE /api/devices`
- `POST /api/devices/parse-ssh`
- `POST /api/devices/{id}/test`
- `POST /api/devices/{id}/host-key`
- `GET /api/devices/{id}/browse`
- `GET/POST/PATCH/DELETE /api/allowed-directories`

All gated by `owner_is_admin_or_single_user`. Paths resolve on the selected device via local or SFTP adapters — never on the browser client.
