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

**Note**: No MongoDB configuration needed!

## How It Works

1. **Device A generates** a 6-character code via `POST /api/pairing/initiate`
2. **Device A shares** the code with Device B securely
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

