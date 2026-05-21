# FastAPI P2P Transfer Service

A serverless peer-to-peer transfer system that connects two devices using auto-generated pairing codes.

## Features
✅ **No Backend Storage** — In-memory only, no database  
✅ **Auto-Generated Codes** — 6-character pairing codes  
✅ **Direct P2P WebSocket** — Device-to-device communication  
✅ **Temporary File Staging** — Optional file transfer support  
✅ **Auto-Cleanup** — Expired pairings removed automatically  

## Getting Started

### 1. Create Python Virtual Environment
```bash
python -m venv .venv
.\.venv\Scripts\activate  # Windows
# source .venv/bin/activate  # macOS/Linux
```

### 2. Install Dependencies
```bash
pip install -r requirements.txt
```

### 3. Run the Server
```bash
uvicorn app.main:app --reload
```

Open `http://localhost:8000` to access the pairing dashboard.

## Environment Variables

| Name | Description | Default |
|------|-------------|---------|
| `UPLOADS_PATH` | Directory for temporary file staging | `backend/uploads` |
| `PAIRING_CODE_TTL` | Pairing code timeout in seconds | `3600` (1 hour) |
| `MAX_CONNECTIONS` | Max concurrent pairings allowed | `100` |
| `FRONTEND_URL` | Public frontend URL used in generated QR links | browser origin or backend URL fallback |
| `MAIL_RELAY_URL` | Vercel mail relay endpoint | `https://mail-services-silk.vercel.app/` |

**Note**: No MongoDB configuration needed!

Email delivery now uses a separate Vercel mail relay service instead of SMTP directly from this backend.

## Mail Relay Templates

### Admin Notification Template

Use this HTML in your mail relay admin notification template.

```html
<div style="margin:0;padding:0;background:#f5f7fb;font-family:system-ui,-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Arial,sans-serif;color:#1f2937;">
  <div style="max-width:640px;margin:0 auto;padding:24px;">
    <div style="background:#ffffff;border:1px solid #e5e7eb;border-radius:16px;overflow:hidden;box-shadow:0 10px 30px rgba(15,23,42,0.08);">
      <div style="padding:24px 28px;background:linear-gradient(135deg,#0f172a,#1d4ed8);color:#ffffff;">
        <div style="font-size:12px;letter-spacing:.12em;text-transform:uppercase;opacity:.8;">Nexdrop Admin Alert</div>
        <div style="margin-top:8px;font-size:24px;font-weight:700;line-height:1.2;">{{subject}}</div>
        <div style="margin-top:8px;font-size:14px;opacity:.85;">{{timestamp}}</div>
      </div>

      <div style="padding:28px;">
        <div style="font-size:16px;line-height:1.6;margin-bottom:20px;">
          A security-related admin action was recorded and sent to {{recipient_count}} recipient(s).
        </div>

        <table role="presentation" cellspacing="0" cellpadding="0" border="0" style="width:100%;border-collapse:collapse;">
          <tr>
            <td style="padding:12px 0;border-bottom:1px solid #e5e7eb;color:#6b7280;width:140px;vertical-align:top;">Action</td>
            <td style="padding:12px 0;border-bottom:1px solid #e5e7eb;font-weight:600;vertical-align:top;">{{action}}</td>
          </tr>
          <tr>
            <td style="padding:12px 0;border-bottom:1px solid #e5e7eb;color:#6b7280;vertical-align:top;">Actor</td>
            <td style="padding:12px 0;border-bottom:1px solid #e5e7eb;vertical-align:top;">{{actor}} ({{role}})</td>
          </tr>
          <tr>
            <td style="padding:12px 0;border-bottom:1px solid #e5e7eb;color:#6b7280;vertical-align:top;">Target</td>
            <td style="padding:12px 0;border-bottom:1px solid #e5e7eb;vertical-align:top;">{{target}}</td>
          </tr>
          <tr>
            <td style="padding:12px 0;color:#6b7280;vertical-align:top;">Status</td>
            <td style="padding:12px 0;vertical-align:top;">
              <span style="display:inline-block;padding:6px 10px;border-radius:999px;background:#eff6ff;color:#1d4ed8;font-size:12px;font-weight:700;letter-spacing:.04em;">{{status}}</span>
            </td>
          </tr>
        </table>

        <div style="margin-top:24px;padding:18px 20px;background:#f9fafb;border:1px solid #e5e7eb;border-radius:12px;">
          <div style="font-size:12px;font-weight:700;letter-spacing:.08em;text-transform:uppercase;color:#6b7280;margin-bottom:10px;">Details</div>
          <div style="white-space:pre-wrap;line-height:1.7;font-size:14px;color:#111827;">{{message}}</div>
        </div>
      </div>
    </div>
  </div>
</div>
```

Template variables expected from the backend:

- `to_email`
- `subject`
- `action`
- `actor`
- `role`
- `target`
- `status`
- `timestamp`
- `message`
- `recipient_count`

### Forgot Password Template

Use this HTML in your mail relay password reset template.

```html
<div style="margin:0;padding:0;background:#f5f7fb;font-family:system-ui,-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Arial,sans-serif;color:#1f2937;">
  <div style="max-width:640px;margin:0 auto;padding:24px;">
    <div style="background:#ffffff;border:1px solid #e5e7eb;border-radius:16px;overflow:hidden;box-shadow:0 10px 30px rgba(15,23,42,0.08);">
      <div style="padding:24px 28px;background:linear-gradient(135deg,#111827,#dc2626);color:#ffffff;">
        <div style="font-size:12px;letter-spacing:.12em;text-transform:uppercase;opacity:.8;">Nexdrop Password Reset</div>
        <div style="margin-top:8px;font-size:24px;font-weight:700;line-height:1.2;">{{subject}}</div>
      </div>

      <div style="padding:28px;">
        <div style="font-size:16px;line-height:1.6;margin-bottom:18px;">
          Hello {{username}}, use the verification code below to reset your password.
        </div>

        <div style="text-align:center;padding:20px 16px;background:#f9fafb;border:1px solid #e5e7eb;border-radius:12px;">
          <div style="font-size:12px;font-weight:700;letter-spacing:.08em;text-transform:uppercase;color:#6b7280;margin-bottom:10px;">One-time code</div>
          <div style="font-size:32px;font-weight:800;letter-spacing:0.2em;color:#111827;">{{otp}}</div>
        </div>

        <div style="margin-top:20px;line-height:1.7;font-size:14px;color:#4b5563;white-space:pre-wrap;">{{message}}</div>
      </div>
    </div>
  </div>
</div>
```

Template variables expected from the backend:

- `to_email`
- `subject`
- `username`
- `otp`
- `message`

## How It Works

1. **Device A generates** a 6-character code via `POST /api/pairing/initiate`
2. **Device A shares** the QR link or code with Device B securely
3. **Device B joins** via `POST /api/pairing/join/{code}`
4. Both devices connect via `WebSocket /ws/pairing/{pairing_id}/{device_id}`
5. Messages relay directly between devices (server only forwards)
6. **Pairing expires** after TTL (configurable, default 1 hour)

## API Reference

### Initiate Pairing (Device A)
```bash
POST /api/pairing/initiate
Content-Type: application/json

{
  "device": {
    "identifier": "my-laptop",
    "label": "Work Laptop"
  }
}

# Response:
{
  "id": "abc123xyz",
  "code": "ABC123",
  "status": "pending",
  "initiator": {...},
  "peer": null,
  "created_at": "2024-01-01T10:00:00",
  "expires_at": "2024-01-01T11:00:00"
}
```

### Join Pairing (Device B)
```bash
POST /api/pairing/join/{code}
Content-Type: application/json

{
  "device": {
    "identifier": "my-tablet",
    "label": "Tablet"
  }
}

# Response: Same as above, but status="connected" and peer is populated
```

### Check Pairing Status
```bash
GET /api/pairing/{code}
```

### WebSocket Connection
```javascript
const ws = new WebSocket('ws://localhost:8000/ws/pairing/{pairing_id}/{device_id}');

// Send text message
ws.send(JSON.stringify({
  type: 'text',
  content: 'Hello from Device A!'
}));

// Listen for messages from peer
ws.onmessage = (event) => {
  const msg = JSON.parse(event.data);
  console.log(`${msg.sender}: ${msg.content}`);
};
```

## File Transfer

Optional file staging endpoints:

```bash
# Upload file for peer
POST /api/pairing/{pairing_id}/files
# Body: multipart/form-data with file

# Download file from peer
GET /api/pairing/{pairing_id}/files/{filename}
```

## Architecture

### Classes
- **PairingCode** — Represents a single device pairing with auto-expiration
- **PairingManager** — Manages all active pairings in memory
- **ConnectionManager** — Routes WebSocket messages between paired devices

### Data Flow
```
Device A (WebSocket) ← Server → Device B (WebSocket)
        ↓                           ↓
  send_to_peer(pairing_id, msg) → forward to peer
```

All data flows through `send_to_peer()` which looks up the peer device and sends the message directly.

## Testing

```bash
# Test pairing flow
curl -X POST http://localhost:8000/api/pairing/initiate \
  -H "Content-Type: application/json" \
  -d '{"device": {"identifier": "test-device"}}'
```

## Troubleshooting

**Pairing code expired?**  
→ Codes expire after 1 hour. Generate a new one.

**Device not connected to WebSocket?**  
→ Verify pairing status is "connected" before connecting to `/ws/pairing/...`

**Messages not reaching peer?**  
→ Check both devices have active WebSocket connections.

## Next Steps
- [ ] Add message encryption
- [ ] Implement chunked file streaming over WebSocket
- [ ] Add bearer token authentication per pairing
- [ ] Rate limiting on code generation

