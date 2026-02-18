# Island CRUD Admin System

A standalone FastAPI-based admin panel for managing Animal Crossing island data with JWT authentication and SQLite storage.

## Features

- 🔐 **JWT Authentication** - Secure login with token-based authentication
- 🏝️ **Full CRUD Operations** - Create, Read, Update, Delete island records
- 💾 **SQLite Database** - Lightweight, file-based database storage
- 🌐 **REST API** - RESTful API endpoints for programmatic access
- 🖥️ **Web UI** - Clean, Bootstrap-based admin interface
- 🌱 **Auto-seeding** - Automatically populates database with island data on first run

## Quick Start

### Installation

1. Navigate to the admin directory:
```bash
cd admin
```

2. Install dependencies:
```bash
pip install -r requirements.txt
```

3. Create environment configuration:
```bash
cp .env.example .env
```

4. Edit `.env` with your credentials:
```env
ADMIN_USERNAME=admin
ADMIN_PASSWORD=your_secure_password
SECRET_KEY=your-secret-key-here
ACCESS_TOKEN_EXPIRE_MINUTES=60
```

> ⚠️ **Security Note**: Change the default credentials and generate a strong secret key for production use.

### Running the Server

Start the FastAPI server:
```bash
cd /path/to/chobot
python -m uvicorn admin.main:app --host 0.0.0.0 --port 8200
```

Or run directly from the admin directory:
```bash
uvicorn main:app --host 0.0.0.0 --port 8200 --reload
```

The admin panel will be available at:
- Web UI: http://localhost:8200/admin/
- API Docs: http://localhost:8200/docs
- Health Check: http://localhost:8200/admin/api/health

## Web UI

### Login Page
Access the admin panel at `/admin/login` and sign in with your credentials.

![Login Page](https://github.com/user-attachments/assets/163264d8-c1dc-4b5e-bcd7-31cfad47be1a)

### Islands List
View all islands with their details, edit or delete existing islands, and add new ones.

![Islands List](https://github.com/user-attachments/assets/3d13ff79-95c6-4078-92c5-eff24039b2e8)

### Island Form
Create new islands or edit existing ones with a user-friendly form.

![Create Island Form](https://github.com/user-attachments/assets/dc4db12a-2564-474a-9acd-438c50c09e7a)

## API Endpoints

### Authentication

#### POST `/admin/api/auth/login`
Authenticate and receive a JWT token.

**Request:**
```json
{
  "username": "admin",
  "password": "your_password"
}
```

**Response:**
```json
{
  "access_token": "eyJhbGc...",
  "token_type": "bearer"
}
```

### Island Management (Requires JWT Token)

All island endpoints require a valid JWT token in the Authorization header:
```
Authorization: Bearer <your_token>
```

#### GET `/admin/api/islands`
List all islands.

**Response:**
```json
[
  {
    "id": "alapaap",
    "name": "ALAPAAP",
    "type": "Treasure Island",
    "items": ["2.0 items", "seasonal items", "rare furniture", "gold tools"],
    "theme": "gold",
    "cat": "member",
    "description": "Member treasure island",
    "seasonal": "Year-Round",
    "map_url": null
  },
  ...
]
```

#### GET `/admin/api/islands/{island_id}`
Get a specific island by ID.

#### POST `/admin/api/islands`
Create a new island.

**Request:**
```json
{
  "id": "new-island",
  "name": "NEW ISLAND",
  "type": "Free Island",
  "theme": "pink",
  "cat": "public",
  "description": "A new island",
  "seasonal": "Year-Round",
  "items": ["item1", "item2"],
  "map_url": "https://example.com/map.png"
}
```

#### PUT `/admin/api/islands/{island_id}`
Update an existing island.

**Request:**
```json
{
  "name": "UPDATED NAME",
  "type": "Free Island",
  "theme": "teal",
  "cat": "public",
  "description": "Updated description",
  "seasonal": "Summer",
  "items": ["updated item 1", "updated item 2"],
  "map_url": "https://example.com/new-map.png"
}
```

#### DELETE `/admin/api/islands/{island_id}`
Delete an island.

**Response:** 204 No Content

### Health Check

#### GET `/admin/api/health`
Check API health status.

**Response:**
```json
{
  "status": "healthy",
  "service": "Island CRUD Admin API",
  "version": "1.0.0"
}
```

## Data Model

### Island Fields

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `id` | string | Yes | Unique slug identifier (e.g., "alapaap") |
| `name` | string | Yes | Display name (e.g., "ALAPAAP") |
| `type` | string | Yes | Island type (e.g., "Treasure Island") |
| `items` | array | Yes | List of available items |
| `theme` | enum | Yes | Color theme: `pink`, `teal`, `purple`, `gold` |
| `cat` | enum | Yes | Category: `public`, `member` |
| `description` | string | Yes | Short description |
| `seasonal` | string | Yes | Seasonal availability (e.g., "Year-Round") |
| `map_url` | string | No | Optional URL to island map image |

> **Note:** The fields `dodoCode`, `status`, and `visitors` are NOT managed by this admin system. They are handled separately by the frontend/main application.

## Database

- **Type:** SQLite
- **File:** `admin/islands.db` (auto-created)
- **Seeding:** Automatically seeds with 40+ islands on first run
- **Backup:** The database file can be backed up by copying `islands.db`

## File Structure

```
admin/
├── main.py              # FastAPI application
├── models.py            # SQLAlchemy models
├── schemas.py           # Pydantic schemas
├── database.py          # Database setup
├── auth.py              # JWT authentication
├── seed.py              # Seed data
├── requirements.txt     # Dependencies
├── .env.example         # Environment template
├── .env                 # Your credentials (gitignored)
├── islands.db           # SQLite database (gitignored)
├── README.md            # This file
└── templates/
    ├── login.html       # Login page
    ├── islands.html     # Islands list
    └── island_form.html # Create/Edit form
```

## Security

- **JWT Tokens:** All island management operations require valid JWT authentication
- **Password Storage:** Passwords should be strong and kept secure
- **Secret Key:** Use a strong, random secret key in production
- **Token Expiry:** Tokens expire after 60 minutes by default (configurable)

### Production Security Checklist

- [ ] Change default admin credentials
- [ ] Generate a strong, random SECRET_KEY
- [ ] Use HTTPS in production
- [ ] Restrict admin panel access by IP if possible
- [ ] Regular backups of `islands.db`
- [ ] Monitor access logs

## Development

### Running in Development Mode

```bash
uvicorn admin.main:app --reload --port 8200
```

### Testing the API

Example using curl:

```bash
# Login
TOKEN=$(curl -s -X POST http://localhost:8200/admin/api/auth/login \
  -H "Content-Type: application/json" \
  -d '{"username":"admin","password":"changeme"}' | jq -r '.access_token')

# List islands
curl -s http://localhost:8200/admin/api/islands \
  -H "Authorization: Bearer $TOKEN" | jq

# Create island
curl -s -X POST http://localhost:8200/admin/api/islands \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "id": "test-island",
    "name": "TEST ISLAND",
    "type": "Test Island",
    "theme": "pink",
    "cat": "public",
    "description": "Test island",
    "seasonal": "Year-Round",
    "items": ["test item 1", "test item 2"]
  }'
```

## Troubleshooting

### Database Issues

If the database becomes corrupted:
```bash
rm admin/islands.db
# Restart the server - it will recreate and reseed the database
```

### Authentication Issues

If you forget your admin password:
1. Edit `admin/.env` and change `ADMIN_PASSWORD`
2. Restart the server
3. Login with new credentials

### Port Already in Use

If port 8200 is already in use:
```bash
# Use a different port
uvicorn admin.main:app --port 8201
```

## License

This admin system is part of the Chobot project and follows the same MIT License.
