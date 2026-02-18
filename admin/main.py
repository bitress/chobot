"""
FastAPI Island CRUD Admin Application
Main application with authentication and CRUD endpoints
"""

import os
from typing import List
from datetime import timedelta

from fastapi import FastAPI, Depends, HTTPException, status, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from admin.database import get_db, init_db, SessionLocal
from admin.models import Island
from admin.schemas import (
    IslandCreate, 
    IslandUpdate, 
    IslandResponse, 
    Token, 
    LoginRequest
)
from admin.auth import (
    authenticate_user, 
    create_access_token, 
    verify_token,
    ACCESS_TOKEN_EXPIRE_MINUTES
)
from admin.seed import seed_islands

# Initialize FastAPI app
app = FastAPI(
    title="Island CRUD Admin API",
    description="Admin backend for managing Animal Crossing island data",
    version="1.0.0"
)

# Setup Jinja2 templates
templates = Jinja2Templates(directory="admin/templates")


# ============================================================================
# STARTUP EVENT
# ============================================================================

@app.on_event("startup")
async def startup_event():
    """
    Initialize database and seed data on startup
    """
    print("[STARTUP] Initializing Island Admin System...")
    
    # Create tables
    init_db()
    print("[STARTUP] Database tables created/verified")
    
    # Seed data if needed
    db = SessionLocal()
    try:
        seed_islands(db)
    finally:
        db.close()
    
    print("[STARTUP] Island Admin System ready!")


# ============================================================================
# WEB UI ROUTES (HTML Templates)
# ============================================================================

@app.get("/admin/", response_class=HTMLResponse)
async def admin_home(request: Request):
    """Redirect to login page"""
    return RedirectResponse(url="/admin/login")


@app.get("/admin/login", response_class=HTMLResponse)
async def login_page(request: Request):
    """Login page"""
    return templates.TemplateResponse("login.html", {"request": request})


@app.get("/admin/islands", response_class=HTMLResponse)
async def islands_list_page(request: Request):
    """Islands listing page"""
    return templates.TemplateResponse("islands.html", {"request": request})


@app.get("/admin/islands/new", response_class=HTMLResponse)
async def create_island_page(request: Request):
    """Create new island form"""
    return templates.TemplateResponse("island_form.html", {
        "request": request,
        "mode": "create",
        "island": None
    })


@app.get("/admin/islands/{island_id}/edit", response_class=HTMLResponse)
async def edit_island_page(request: Request, island_id: str):
    """Edit island form"""
    return templates.TemplateResponse("island_form.html", {
        "request": request,
        "mode": "edit",
        "island_id": island_id
    })


# ============================================================================
# AUTHENTICATION API ROUTES
# ============================================================================

@app.post("/admin/api/auth/login", response_model=Token)
async def login(login_data: LoginRequest):
    """
    Authenticate user and return JWT token
    
    - **username**: Admin username
    - **password**: Admin password
    
    Returns JWT access token on success
    """
    if not authenticate_user(login_data.username, login_data.password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect username or password",
            headers={"WWW-Authenticate": "Bearer"},
        )
    
    # Create access token
    access_token_expires = timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    access_token = create_access_token(
        data={"sub": login_data.username},
        expires_delta=access_token_expires
    )
    
    return {
        "access_token": access_token,
        "token_type": "bearer"
    }


# ============================================================================
# ISLAND CRUD API ROUTES (All require JWT authentication)
# ============================================================================

@app.get("/admin/api/islands", response_model=List[IslandResponse])
async def list_islands(
    db: Session = Depends(get_db),
    _: dict = Depends(verify_token)
):
    """
    Get all islands
    
    Returns list of all islands in the database
    Requires valid JWT token
    """
    islands = db.query(Island).order_by(Island.name).all()
    return islands


@app.get("/admin/api/islands/{island_id}", response_model=IslandResponse)
async def get_island(
    island_id: str,
    db: Session = Depends(get_db),
    _: dict = Depends(verify_token)
):
    """
    Get a single island by ID
    
    - **island_id**: Unique island identifier (slug)
    
    Returns island data or 404 if not found
    Requires valid JWT token
    """
    island = db.query(Island).filter(Island.id == island_id).first()
    
    if not island:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Island '{island_id}' not found"
        )
    
    return island


@app.post("/admin/api/islands", response_model=IslandResponse, status_code=status.HTTP_201_CREATED)
async def create_island(
    island_data: IslandCreate,
    db: Session = Depends(get_db),
    _: dict = Depends(verify_token)
):
    """
    Create a new island
    
    - **id**: Unique slug identifier (lowercase, alphanumeric, hyphens, underscores)
    - **name**: Display name
    - **type**: Island type
    - **items**: List of available items
    - **theme**: Color theme (pink, teal, purple, gold)
    - **cat**: Category (public, member)
    - **description**: Short description
    - **seasonal**: Seasonal availability
    - **map_url**: Optional map image URL
    
    Returns created island data
    Requires valid JWT token
    """
    # Check if island with this ID already exists
    existing = db.query(Island).filter(Island.id == island_data.id).first()
    if existing:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Island with ID '{island_data.id}' already exists"
        )
    
    # Create new island
    island = Island(**island_data.dict())
    db.add(island)
    db.commit()
    db.refresh(island)
    
    return island


@app.put("/admin/api/islands/{island_id}", response_model=IslandResponse)
async def update_island(
    island_id: str,
    island_data: IslandUpdate,
    db: Session = Depends(get_db),
    _: dict = Depends(verify_token)
):
    """
    Update an existing island
    
    - **island_id**: ID of island to update
    - **name**: Display name
    - **type**: Island type
    - **items**: List of available items
    - **theme**: Color theme (pink, teal, purple, gold)
    - **cat**: Category (public, member)
    - **description**: Short description
    - **seasonal**: Seasonal availability
    - **map_url**: Optional map image URL
    
    Returns updated island data or 404 if not found
    Requires valid JWT token
    """
    island = db.query(Island).filter(Island.id == island_id).first()
    
    if not island:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Island '{island_id}' not found"
        )
    
    # Update island fields
    for key, value in island_data.dict().items():
        setattr(island, key, value)
    
    db.commit()
    db.refresh(island)
    
    return island


@app.delete("/admin/api/islands/{island_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_island(
    island_id: str,
    db: Session = Depends(get_db),
    _: dict = Depends(verify_token)
):
    """
    Delete an island
    
    - **island_id**: ID of island to delete
    
    Returns 204 No Content on success or 404 if not found
    Requires valid JWT token
    """
    island = db.query(Island).filter(Island.id == island_id).first()
    
    if not island:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Island '{island_id}' not found"
        )
    
    db.delete(island)
    db.commit()
    
    return None


# ============================================================================
# HEALTH CHECK
# ============================================================================

@app.get("/admin/api/health")
async def health_check():
    """Health check endpoint"""
    return {
        "status": "healthy",
        "service": "Island CRUD Admin API",
        "version": "1.0.0"
    }


# ============================================================================
# RUN SERVER
# ============================================================================

if __name__ == "__main__":
    import uvicorn
    
    port = int(os.getenv("PORT", "8200"))
    uvicorn.run(
        "admin.main:app",
        host="0.0.0.0",
        port=port,
        reload=True
    )
